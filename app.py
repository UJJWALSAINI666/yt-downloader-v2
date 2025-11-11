
from flask import Flask, request, send_file, render_template
import tempfile, shutil, uuid, pathlib, time
from yt_dlp import YoutubeDL
import os

app = Flask(__name__)
app.secret_key = "secret"

TEMP_ROOT = pathlib.Path(tempfile.gettempdir()) / "yt_simple"
TEMP_ROOT.mkdir(exist_ok=True)

@app.route("/")
def index():
    return render_template("index.html")

@app.post("/download")
def download():
    url = request.form.get("url","").strip()
    out_type = request.form.get("type","mp4")
    job = TEMP_ROOT / uuid.uuid4().hex
    job.mkdir()
    out = str(job / "%(title).200s-%(id)s.%(ext)s")

    opts = {"outtmpl": out, "quiet": True}
    if out_type == "mp3":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{"key":"FFmpegExtractAudio","preferredcodec":"mp3"}]
    else:
        opts["format"] = "bestvideo[height<=2160]+bestaudio/best"

    with YoutubeDL(opts) as y:
        info = y.extract_info(url, download=True)
        file = pathlib.Path(y.prepare_filename(info))
    def cleanup(p):
        time.sleep(60)
        shutil.rmtree(p, ignore_errors=True)
    import threading; threading.Thread(target=cleanup, args=(job,), daemon=True).start()
    return send_file(str(file), as_attachment=True)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)))
