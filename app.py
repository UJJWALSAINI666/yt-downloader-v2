import os, shutil, tempfile
from pathlib import Path
from typing import Dict, Any, List
from flask import Flask, render_template, request, jsonify, send_file, abort, after_this_request
from yt_dlp import YoutubeDL

app = Flask(__name__)

# ---------- Config & Limits ----------
MAX_DURATION_SECONDS = int(os.environ.get("MAX_DURATION_SECONDS", "0"))  # 0 = no limit
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "2"))
ENABLE_AUDIO_MP3 = True  # requires ffmpeg

# simple in-process concurrency guard
from threading import Semaphore
_sem = Semaphore(MAX_CONCURRENT)

# ---------- FFmpeg detection ----------
def ffmpeg_path() -> str | None:
    return os.environ.get("FFMPEG_PATH") or shutil.which("ffmpeg")

def base_ydl_opts(tmpdir: str) -> Dict[str, Any]:
    opts = {
        "outtmpl": str(Path(tmpdir) / "%(title)s-%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        "concurrent_fragment_downloads": 8,
        "retries": 10,
        "fragment_retries": 10,
        "http_chunk_size": 10485760,  # 10MB chunks
    }
    ffm = ffmpeg_path()
    if ffm:
        # yt-dlp expects directory for ffmpeg executables
        opts["ffmpeg_location"] = str(Path(ffm).parent)
    return opts

# ---------- Helpers ----------
def pick_download_file(tmpdir: str) -> Path:
    files = list(Path(tmpdir).glob("*"))
    if not files:
        raise FileNotFoundError("No output file produced.")
    return max(files, key=lambda p: p.stat().st_size)

def summarize_formats(info: Dict[str, Any]) -> Dict[str, Any]:
    title = info.get("title")
    duration = info.get("duration")
    if MAX_DURATION_SECONDS and duration and duration > MAX_DURATION_SECONDS:
        raise ValueError(f"Video too long ({duration}s > limit {MAX_DURATION_SECONDS}s).")

    vids, auds = [], []
    for f in info.get("formats", []):
        fmt = {
            "format_id": f.get("format_id"),
            "ext": f.get("ext"),
            "acodec": f.get("acodec"),
            "vcodec": f.get("vcodec"),
            "fps": f.get("fps"),
            "height": f.get("height"),
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "format_note": f.get("format_note"),
        }
        if fmt["vcodec"] and fmt["vcodec"] != "none":
            vids.append(fmt)
        if (fmt["vcodec"] in (None, "none")) and fmt["acodec"] and fmt["acodec"] != "none":
            auds.append(fmt)

    vids.sort(key=lambda x: (x.get("height") or 0, x.get("fps") or 0), reverse=True)
    auds.sort(key=lambda x: (x.get("filesize") or 0), reverse=True)

    return {
        "title": title,
        "duration": duration,
        "thumbnail": info.get("thumbnail"),
        "video_formats": vids,
        "audio_formats": auds,
        "webpage_url": info.get("webpage_url"),
        "uploader": info.get("uploader"),
    }

# ---------- Routes ----------
@app.get("/")
def index():
    return render_template("index.html", ffmpeg_ok=bool(ffmpeg_path()))

@app.post("/api/formats")
def api_formats():
    url = (request.form.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "URL required"}), 400
    with tempfile.TemporaryDirectory(prefix="probe_") as tmpdir:
        opts = base_ydl_opts(tmpdir)
        opts.update({"skip_download": True})
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            summary = summarize_formats(info)
            return jsonify({"ok": True, **summary})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400

@app.post("/api/download")
def api_download():
    url = (request.form.get("url") or "").strip()
    mode = (request.form.get("mode") or "video").strip()
    format_id = (request.form.get("format_id") or "").strip() or None
    if not url:
        return abort(400, "URL required")

    # ffmpeg requirement for conversions/merging
    if not ffmpeg_path():
        if mode == "audio" or "+" in (format_id or "") or mode == "video":
            return abort(503, "FFmpeg not available on server.")

    if not _sem.acquire(timeout=1):
        return abort(429, "Server busy. Try again in a moment.")

    tmpdir = tempfile.mkdtemp(prefix="ydl_")

    @after_this_request
    def cleanup(response):
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass
        _sem.release()
        return response

    try:
        opts = base_ydl_opts(tmpdir)

        if mode == "audio":
            if not ENABLE_AUDIO_MP3:
                return abort(503, "Audio conversion disabled.")
            opts.update({
                "format": format_id or "bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ],
            })
        else:
            chosen = f"{format_id}+bestaudio/best" if format_id else "bestvideo+bestaudio/best"
            opts.update({
                "format": chosen,
                "postprocessors": [
                    {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}
                ],
            })

        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)

        out_file = pick_download_file(tmpdir)
        base_title = info.get("title") or "video"
        download_name = f"{base_title}.mp3" if mode == "audio" else f"{base_title}{out_file.suffix}"

        return send_file(str(out_file), as_attachment=True, download_name=download_name)
    except Exception as e:
        return abort(500, f"Download failed: {e}")

@app.get("/health")
def health():
    return {
        "ok": True,
        "ffmpeg": ffmpeg_path() or "missing",
        "max_concurrent": MAX_CONCURRENT,
    }

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)