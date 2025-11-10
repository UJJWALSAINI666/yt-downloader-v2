# YouTube Downloader (Flask + yt-dlp + ffmpeg)

A minimal Flask app to download YouTube videos as MP4 (up to 4K if available) or MP3 using yt-dlp and ffmpeg.

## Quick local run
1. Create virtualenv: `python -m venv venv && source venv/bin/activate`
2. `pip install -r requirements.txt`
3. Make sure `ffmpeg` is on PATH (install via apt / brew / choco / pkg etc.).
4. Run: `python app.py` and open http://127.0.0.1:5000

## Deploy to Railway (via GitHub)
1. Push this folder to a new GitHub repo.
2. In Railway: New Project → Deploy from GitHub → select your repo.
3. Railway will build with the Dockerfile in this repo (which installs ffmpeg).

### Environment variables (optional)
- `FLASK_SECRET`: secret key for Flask sessions (set any random string).
- `COOKIES_TEXT`: paste cookies for accounts/age-restricted videos if you legally have access.

## Notes
- Comply with YouTube Terms of Service and copyright law.
- Files are stored temporarily and auto-cleaned shortly after each download.
- Consider rate-limiting, CAPTCHA, and quotas before public use.


## Features added
- Live progress bar via Server-Sent Events (`/progress/<job_id>`)
- Rate limiting: 3 starts/min & 1 concurrent per IP (using Flask-Limiter)
- Background worker per job and delayed cleanup
