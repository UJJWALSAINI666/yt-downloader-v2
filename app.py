# app.py (SSE progress + rate limiting)
import os
import tempfile
import shutil
import threading
import uuid
import pathlib
import logging
import queue
import time
from functools import wraps
from flask import Flask, request, render_template, send_file, flash, redirect, url_for, Response, jsonify, abort
from yt_dlp import YoutubeDL

# Rate limiting via Flask-Limiter
try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    HAS_LIMITER = True
except Exception:
    HAS_LIMITER = False

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "change-me")

# temp root folder to store downloads (Railway has ephemeral storage; cleanup after send)
TEMP_ROOT = pathlib.Path(tempfile.gettempdir()) / "yt_downloader"
TEMP_ROOT.mkdir(parents=True, exist_ok=True)

# Basic logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# In-memory stores
JOBS = {}            # job_id -> dict(status, progress, filename(pathlib.Path)|None, error|None, created_at)
PROGRESS_QUEUES = {} # job_id -> Queue of progress events for SSE
ACTIVE_BY_IP = set() # simple concurrency limiter per IP (1 at a time)

# Limiter setup
if HAS_LIMITER:
    limiter = Limiter(get_remote_address, app=app, default_limits=["5 per minute", "100 per day"])

def single_concurrent(fn):
    """Allow only one active download per client IP at a time."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        ip = get_remote_address() if HAS_LIMITER else request.remote_addr
        key = f"{ip}"
        if key in ACTIVE_BY_IP:
            return jsonify({"ok": False, "error": "Another download is already in progress from your IP. Please wait for it to finish."}), 429
        try:
            ACTIVE_BY_IP.add(key)
            return fn(*args, **kwargs)
        finally:
            # release happens inside worker when done; here is a safety fallback with slight delay
            threading.Timer(2.0, lambda: ACTIVE_BY_IP.discard(key)).start()
    return wrapper

# Helper: safe cleanup
def safe_remove(path: pathlib.Path):
    try:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    except Exception as e:
        logger.warning(f"Cleanup failed for {path}: {e}")

def job_update(job_id, **fields):
    job = JOBS.get(job_id)
    if not job:
        return
    job.update(fields)
    # push to SSE
    q = PROGRESS_QUEUES.get(job_id)
    if q:
        try:
            q.put_nowait(job.copy())
        except queue.Full:
            pass

def progress_hook_factory(job_id):
    def hook(d):
        if d.get('status') == 'downloading':
            # Calculate percentage if available
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            downloaded = d.get('downloaded_bytes') or 0
            percent = 0.0
            if total:
                percent = round((downloaded / total) * 100, 2)
            speed = d.get('speed') or 0
            eta = d.get('eta') or 0
            job_update(job_id, status='downloading', progress={'percent': percent, 'eta': eta, 'speed': speed})
        elif d.get('status') == 'finished':
            job_update(job_id, status='postprocessing', progress={'text': 'Merging/processing...'})
    return hook

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")

# Conditional limiter decorator helper
def maybe_limit(rate):
    if HAS_LIMITER:
        return limiter.limit(rate)
    # no-op decorator when limiter missing
    def _noop(f):
        return f
    return _noop

@app.route("/start", methods=["POST"])
@maybe_limit("3 per minute")
@single_concurrent
def start():
    url = request.form.get("url", "").strip()
    out_type = request.form.get("type", "mp4")
    if not url:
        return jsonify({"ok": False, "error": "Please provide a video URL"}), 400

    job_id = uuid.uuid4().hex
    workdir = TEMP_ROOT / job_id
    workdir.mkdir(parents=True, exist_ok=True)

    JOBS[job_id] = {
        "status": "queued",
        "progress": {"percent": 0},
        "filename": None,
        "error": None,
        "created_at": time.time(),
        "type": out_type,
        "workdir": str(workdir),
        "ip": request.remote_addr,
    }
    PROGRESS_QUEUES[job_id] = queue.Queue(maxsize=100)

    def worker():
        ip_key = request.remote_addr
        try:
            out_template = str(workdir / "%(title).200s-%(id)s.%(ext)s")
            ydl_opts = {
                'outtmpl': out_template,
                'noplaylist': True,
                'quiet': True,
                'no_warnings': True,
                'progress_hooks': [progress_hook_factory(job_id)],
                'postprocessors': [],
            }
            cookies = os.environ.get("COOKIES_TEXT")
            if cookies:
                cookie_file = workdir / "cookies.txt"
                cookie_file.write_text(cookies)
                ydl_opts['cookiefile'] = str(cookie_file)

            if out_type == 'mp3':
                ydl_opts.update({
                    'format': 'bestaudio/best',
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': '192',
                    }],
                })
            else:
                ydl_opts.update({
                    'format': 'bestvideo[height<=2160]+bestaudio/best',
                    'retries': 3,
                })

            job_update(job_id, status='downloading')

            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                files = list(pathlib.Path(workdir).iterdir())
                if files:
                    downloaded_file = max(files, key=lambda p: p.stat().st_size)
                else:
                    downloaded_file = pathlib.Path(ydl.prepare_filename(info))
            if not downloaded_file.exists():
                raise RuntimeError("File not found after download")
            job_update(job_id, status='done', filename=str(downloaded_file))

            # schedule auto cleanup later
            def delayed_cleanup(p):
                time.sleep(180)  # keep for 3 minutes after completion
                safe_remove(p)
            threading.Thread(target=delayed_cleanup, args=(workdir,), daemon=True).start()

        except Exception as e:
            job_update(job_id, status='error', error=str(e))
            safe_remove(workdir)
        finally:
            # release concurrency lock
            ACTIVE_BY_IP.discard(ip_key)

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})

@app.route("/progress/<job_id>")
def progress_stream(job_id):
    if job_id not in JOBS:
        return abort(404)
    q = PROGRESS_QUEUES.get(job_id)
    if not q:
        PROGRESS_QUEUES[job_id] = queue.Queue(maxsize=100)
        q = PROGRESS_QUEUES[job_id]

    def event_stream():
        # send initial state
        yield f"data: {jsonify_sse(JOBS[job_id])}\n\n"
        while True:
            try:
                update = q.get(timeout=60)
                yield f"data: {jsonify_sse(update)}\n\n"
                if update.get('status') in ('done', 'error'):
                    break
            except queue.Empty:
                # keep-alive
                yield "data: {\"ping\": true}\n\n"
    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return Response(event_stream(), headers=headers)

def jsonify_sse(obj):
    import json as _json
    # drop non-serializable fields
    o = dict(obj)
    o.pop('workdir', None)
    return _json.dumps(o)

@app.route("/file/<job_id>")
def get_file(job_id):
    job = JOBS.get(job_id)
    if not job or job.get('status') != 'done':
        return abort(404)
    file_path = pathlib.Path(job['filename'])
    if not file_path.exists():
        return abort(404)
    mime = 'audio/mpeg' if job.get('type') == 'mp3' else 'video/mp4'
    return send_file(str(file_path), as_attachment=True, download_name=file_path.name, mimetype=mime)

# Fallback legacy route (simple, no progress UI)
@app.route("/download", methods=["POST"])
def legacy_download():
    url = request.form.get("url", "").strip()
    out_type = request.form.get("type", "mp4")
    if not url:
        flash("Please provide a video URL")
        return redirect(url_for("index"))

    job_id = uuid.uuid4().hex
    workdir = TEMP_ROOT / job_id
    workdir.mkdir(parents=True, exist_ok=True)
    out_template = str(workdir / "%(title).200s-%(id)s.%(ext)s")

    ydl_opts = {
        'outtmpl': out_template,
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'postprocessors': [],
    }
    cookies = os.environ.get("COOKIES_TEXT")
    if cookies:
        cookie_file = workdir / "cookies.txt"
        cookie_file.write_text(cookies)
        ydl_opts['cookiefile'] = str(cookie_file)

    try:
        if out_type == 'mp3':
            ydl_opts.update({
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
            })
        else:
            ydl_opts.update({
                'format': 'bestvideo[height<=2160]+bestaudio/best',
                'retries': 3,
            })

        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            files = list(pathlib.Path(workdir).iterdir())
            if files:
                downloaded_file = max(files, key=lambda p: p.stat().st_size)
            else:
                downloaded_file = pathlib.Path(ydl.prepare_filename(info))

        if not downloaded_file.exists():
            raise RuntimeError("Download failed or file missing")

        mime = 'audio/mpeg' if out_type == 'mp3' else 'video/mp4'
        return send_file(str(downloaded_file), as_attachment=True, download_name=downloaded_file.name, mimetype=mime)

    except Exception as e:
        logger.exception("Download error")
        flash(f"Download failed: {e}")
        safe_remove(workdir)
        return redirect(url_for('index'))
    finally:
        def delayed_cleanup(p):
            time.sleep(30)
            safe_remove(p)
        threading.Thread(target=delayed_cleanup, args=(workdir,), daemon=True).start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=True)
