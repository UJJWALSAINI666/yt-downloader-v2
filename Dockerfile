# Dockerfile (fix)
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
  && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app

# Shell form so $PORT expands (fallback 8000 if not set)
CMD gunicorn app:app --bind 0.0.0.0:${PORT:-8000} --workers 1
