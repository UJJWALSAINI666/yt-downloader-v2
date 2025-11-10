import os
import tempfile
import shutil
import threading
import uuid
import pathlib
import logging
import time
import queue
from flask import Flask, request, render_template, send_file, jsonify, abort, Response

try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    limiter = Limiter(get_remote_address, default_limits=["100 per day", "5 per minute"])
    HAS_LIMITER = True
except Exception:
    limiter = None
    HAS_LIMITER = False

from yt_dlp import YoutubeDL

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "change-this")

if HAS_LIMITER:
    limiter.init_app(app)

TEMP_ROOT = pathlib.Path(tempfile.gettempdir()) / "yt_jobs"
TEMP_ROOT.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("yt")

JOBS = {}
PROGRESS = {}
ACTIVE = set()

def safe_rm(p: pathlib.Path):
    try:
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
    except:
        pass

def limit(rule):
    def wrap(func):
        if HAS_LIMITER:
            return limiter.limit(rule)(func)
        return func
    return wrap

def single_download(func):
    def wrapper(*a, **kw):
        ip = request.remote_addr
        if ip in ACTIVE:
            return jsonify({"ok": False, "error": "Wait, your previous download is still running."}), 429
        ACTIVE.add(ip)
        try:
            return func(*a, **kw)
        finally:
            threading.Timer(2, lambda: ACTIVE.discard(ip)).start()
    return wrapper

def progress_hook(job_id):
    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            pct = round(done / total * 100, 2) if total else 0
            PROGRESS[job_id] = {"percent": pct}
        elif d.get("status") == "finished":
            PROGRESS[job_id] = {"text": "Merging / Processing..."}
    return hook

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/start", methods=["POST"])
@limit("3 per minute")
@single_download
def start():
    url = request.form.get("url", "").strip()
    ftype = request.form.get("type", "mp4")

    if not url:
        return jsonify({"ok": False, "error": "Enter a valid YouTube URL"}), 400

    job_id = uuid.uuid4().hex
    work = TEMP_ROOT / job_id
    work.mkdir(parents=True, exist_ok=True)
    PROGRESS[job_id] = {"percent": 0}

    JOBS[job_id] = {"status": "processing", "file": None, "type": ftype}

    def worker():
        try:
            out = str(work / "%(title).200s-%(id)s.%(ext)s")
            opts = {
                "outtmpl": out,
                "noplaylist": True,
                "quiet": True,
                "progress_hooks": [progress_hook(job_id)],
            }

            if ftype == "mp3":
                opts["format"] = "bestaudio/best"
                opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
            else:
                opts["format"] = "bestvideo[height<=2160]+bestaudio/best"

            with YoutubeDL(opts) as ydl:
                ydl.extract_info(url, download=True)

            file = max(work.iterdir(), key=lambda x: x.stat().st_size)
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["file"] = str(file)

            threading.Thread(target=lambda: (time.sleep(180), safe_rm(work)), daemon=True).start()

        except Exception as e:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = str(e)
            safe_rm(work)

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})

@app.route("/progress/<job_id>")
def progress(job_id):
    if job_id not in JOBS:
        return abort(404)

    def stream():
        while True:
            job = JOBS[job_id]
            data = {"status": job["status"], "progress": PROGRESS.get(job_id)}
            yield "data: " + jsonify(data).data.decode() + "\n\n"
            if job["status"] in ("done", "error"):
                break
            time.sleep(1)

    return Response(stream(), mimetype="text/event-stream")

@app.route("/file/<job_id>")
def file(job_id):
    job = JOBS.get(job_id)
    if not job or job["status"] != "done":
        return abort(404)
    p = pathlib.Path(job["file"])
    return send_file(str(p), as_attachment=True, download_name=p.name)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
