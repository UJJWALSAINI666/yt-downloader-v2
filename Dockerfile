# Railway-ready image with FFmpeg preinstalled
FROM python:3.11-slim

# Install FFmpeg
RUN apt-get update &&         apt-get install -y --no-install-recommends ffmpeg &&         rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY . .

ENV PYTHONUNBUFFERED=1
ENV PORT=8080
EXPOSE 8080

# Gunicorn bound to $PORT for Railway
CMD ["bash", "-lc", "exec gunicorn app:app --workers=1 --threads=4 --timeout=600 --bind 0.0.0.0:${PORT}"]
