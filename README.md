# Smart Video Downloader (Flask + yt-dlp + FFmpeg)

## Deploy to Railway
1. Push these files to a new GitHub repo.
2. Create a new Railway project → "Deploy from GitHub".
3. Railway auto-detects Python. It will install packages from **requirements.txt** and system packages from **apt.txt** (FFmpeg).
4. Set **Start Command** to: `gunicorn app:app --workers=1 --threads=4 --timeout=600` (or keep Procfile).
5. (Optional) Set env vars:
   - `MAX_CONCURRENT=2`
   - `MAX_DURATION_SECONDS=0`  (0 = no limit)
   - `FFMPEG_PATH` if ffmpeg is not on PATH.

Open the app → paste a video URL → **Get Options** → choose **Download Video** or **Download Audio**.

Health check: `/health`
