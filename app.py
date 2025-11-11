import os
import shutil
import tempfile
from pathlib import Path
from typing import Dict, Any, Optional, List

from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    abort,
    Response,
)
from yt_dlp import YoutubeDL
from threading import Semaphore

app = Flask(__name__)

# ---------- Config ----------
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "2"))
_sem = Semaphore(MAX_CONCURRENT)


# ---------- Utilities ----------
def ffmpeg_path() -> Optional[str]:
    """Return ffmpeg binary path if available."""
    return shutil.which("ffmpeg") or os.environ.get("FFMPEG_PATH")


def _write_cookies_if_any(tmpdir: str) -> Optional[str]:
    """
    Accept cookies from:
      1) uploaded file <input name="cookies">
      2) textarea 'cookies_text'
      3) env var COOKIES_TEXT
    Returns path to cookie file or None.
    """
    # 1) file upload
    f = request.files.get("cookies")
    if f and f.filename:
        dst = Path(tmpdir) / "cookies.txt"
        f.save(dst)
        return str(dst)

    # 2) textarea
    raw = (request.form.get("cookies_text") or "").strip()
    if raw:
        dst = Path(tmpdir) / "cookies.txt"
        dst.write_text(raw, encoding="utf-8")
        return str(dst)

    # 3) env var
    env_raw = (os.environ.get("COOKIES_TEXT") or "").strip()
    if env_raw:
        dst = Path(tmpdir) / "cookies.txt"
        dst.write_text(env_raw, encoding="utf-8")
        return str(dst)

    return None


def _base_ydl_opts(tmpdir: str, cookies_file: Optional[str]) -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "outtmpl": str(Path(tmpdir) / "%(title)s-%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        "concurrent_fragment_downloads": 8,
        "retries": 10,
        "fragment_retries": 10,
        "http_chunk_size": 10 * 1024 * 1024,
        "headers": {"User-Agent": "Mozilla/5.0"},
    }
    if cookies_file:
        opts["cookiefile"] = cookies_file

    ffm = ffmpeg_path()
    if ffm:
        # yt-dlp expects a directory for ffmpeg binaries
        opts["ffmpeg_location"] = str(Path(ffm).parent)
    return opts


def _summarize_formats(info: Dict[str, Any]) -> Dict[str, Any]:
    vids: List[Dict[str, Any]] = []
    auds: List[Dict[str, Any]] = []
    for f in info.get("formats", []):
        d = {
            "format_id": f.get("format_id"),
            "ext": f.get("ext"),
            "acodec": f.get("acodec"),
            "vcodec": f.get("vcodec"),
            "fps": f.get("fps"),
            "height": f.get("height"),
            "filesize": f.get("filesize") or f.get("filesize_approx"),
        }
        if d["vcodec"] and d["vcodec"] != "none":
            vids.append(d)
        if (d["vcodec"] in (None, "none")) and d["acodec"] and d["acodec"] != "none":
            auds.append(d)

    vids.sort(key=lambda x: (x.get("height") or 0, x.get("fps") or 0), reverse=True)
    auds.sort(key=lambda x: (x.get("filesize") or 0), reverse=True)

    return {
        "title": info.get("title"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "uploader": info.get("uploader"),
        "video_formats": vids,
        "audio_formats": auds,
    }


def _guess_mimetype(suffix: str, audio_mode: bool) -> str:
    if audio_mode:
        # we produce mp3 in audio mode
        return "audio/mpeg"
    if suffix.lower() == ".mp4":
        return "video/mp4"
    if suffix.lower() == ".webm":
        return "video/webm"
    return "application/octet-stream"


def _stream_file_and_cleanup(file_path: Path, download_name: str, tmpdir: str, audio_mode: bool) -> Response:
    """
    Stream file in chunks (avoids Gunicorn sendfile path) and
    remove the tmpdir *after* the response is fully sent.
    """
    def generate():
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)  # 1 MB
                if not chunk:
                    break
                yield chunk

    resp = Response(generate(), mimetype=_guess_mimetype(file_path.suffix, audio_mode))
    resp.headers["Content-Disposition"] = f'attachment; filename="{download_name}"'
    try:
        resp.headers["Content-Length"] = str(file_path.stat().st_size)
    except Exception:
        pass

    # clean after the stream finishes
    def _cleanup():
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass

    resp.call_on_close(_cleanup)
    return resp


# ---------- Routes ----------
@app.get("/")
def index():
    # simple inline page so you can test
    return """
<!doctype html>
<title>Smart Video Downloader</title>
<h2>Smart Video Downloader</h2>
<p>FFmpeg: {ffmpeg}</p>
<form id="probe" method="post" action="/api/formats" enctype="multipart/form-data">
  <input name="url" placeholder="Paste URL" style="width:420px" required>
  <input type="file" name="cookies" accept=".txt">
  <button type="submit">Get Options</button>
</form>
<details><summary>Paste cookies.txt</summary>
  <form method="post" action="/api/formats" enctype="multipart/form-data">
    <input name="url" placeholder="Paste URL" style="width:420px" required>
    <textarea name="cookies_text" rows="4" cols="60"></textarea>
    <button type="submit">Get Options</button>
  </form>
</details>
<p>Use API from your own UI: <code>/api/formats</code> then <code>/api/download</code></p>
""".format(ffmpeg=("available" if ffmpeg_path() else "missing"))


@app.post("/api/formats")
def api_formats():
    url = (request.form.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "URL required"}), 400

    with tempfile.TemporaryDirectory(prefix="probe_") as tmpdir:
        cookies_file = _write_cookies_if_any(tmpdir)
        opts = _base_ydl_opts(tmpdir, cookies_file)
        opts["skip_download"] = True
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            return jsonify({"ok": True, **_summarize_formats(info)})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400


@app.post("/api/download")
def api_download():
    url = (request.form.get("url") or "").strip()
    mode = (request.form.get("mode") or "video").strip()  # 'video' or 'audio'
    format_id = (request.form.get("format_id") or "").strip() or None

    if not url:
        return abort(400, "URL required")

    if not ffmpeg_path():
        return abort(503, "FFmpeg not available on server.")

    if not _sem.acquire(timeout=1):
        return abort(429, "Server busy. Try again shortly.")

    tmpdir = tempfile.mkdtemp(prefix="ydl_")
    try:
        cookies_file = _write_cookies_if_any(tmpdir)
        opts = _base_ydl_opts(tmpdir, cookies_file)

        # Choose formats & postprocessors
        if mode == "audio":
            opts.update({
                "format": format_id or "bestaudio/best",
                "postprocessors": [
                    {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
                ],
            })
        else:
            chosen = f"{format_id}+bestaudio/best" if format_id else "bestvideo+bestaudio/best"
            opts.update({
                "format": chosen,
                "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
            })

        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)

        # pick largest output file
        out_files = list(Path(tmpdir).glob("*"))
        if not out_files:
            return abort(500, "No output produced.")
        out_file = max(out_files, key=lambda p: p.stat().st_size)

        base_title = info.get("title") or "video"
        download_name = f"{base_title}.mp3" if mode == "audio" else f"{base_title}{out_file.suffix}"

        # Stream (no sendfile) and cleanup when done
        resp = _stream_file_and_cleanup(out_file, download_name, tmpdir, audio_mode=(mode == "audio"))
        return resp

    except Exception as e:
        # if we error, still cleanup now
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass
        return abort(500, f"Download failed: {e}")
    finally:
        _sem.release()


@app.get("/health")
def health():
    return {"ok": True, "ffmpeg": ffmpeg_path() or "missing", "max_concurrent": MAX_CONCURRENT}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
