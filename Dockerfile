FROM python:3.11-slim

# Install ffmpeg
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Set workdir
WORKDIR /app

# Copy files
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app

# Expose port (Railway sets PORT env)
ENV PYTHONUNBUFFERED=1
CMD ["gunicorn","app:app","--bind","0.0.0.0:${PORT}","--workers","1"]
