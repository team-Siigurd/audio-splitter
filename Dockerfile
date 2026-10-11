FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ENV PYTHONUNBUFFERED=1
ENV OUTPUT_ROOT=/tmp/audio-splitter
ENV FILE_TTL_SECONDS=21600
ENV RETRY_AFTER_SECONDS=30
ENV ALLOWED_HOST_SUFFIXES=sharepoint.com

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000} --workers 1"]
