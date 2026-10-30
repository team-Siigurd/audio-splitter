import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import requests
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, HttpUrl

app = FastAPI(title="Audio Splitter", version="2.1.0")

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
OUTPUT_ROOT = Path(os.getenv("OUTPUT_ROOT", "/tmp/audio-splitter"))
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

FILE_TTL_SECONDS = int(os.getenv("FILE_TTL_SECONDS", str(6 * 60 * 60)))
RETRY_AFTER_SECONDS = int(os.getenv("RETRY_AFTER_SECONDS", "30"))
DOWNLOAD_CONNECT_TIMEOUT = int(os.getenv("DOWNLOAD_CONNECT_TIMEOUT", "30"))
DOWNLOAD_READ_TIMEOUT = int(os.getenv("DOWNLOAD_READ_TIMEOUT", "900"))
MAX_DOWNLOAD_BYTES = int(os.getenv("MAX_DOWNLOAD_BYTES", str(2 * 1024 * 1024 * 1024)))
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(MAX_DOWNLOAD_BYTES)))

# Intended source is OneDrive/SharePoint. Override with a comma-separated list,
# e.g. "sharepoint.com,example.com". Set to "*" to allow any HTTPS host.
ALLOWED_HOST_SUFFIXES = [
    item.strip().lower()
    for item in os.getenv("ALLOWED_HOST_SUFFIXES", "sharepoint.com").split(",")
    if item.strip()
]

jobs_lock = threading.Lock()


class SplitRequest(BaseModel):
    url: HttpUrl
    segment_seconds: int = Field(default=900, ge=60, le=3600)


def _job_dir(job_id: str) -> Path:
    return OUTPUT_ROOT / job_id


def _job_file(job_id: str) -> Path:
    return _job_dir(job_id) / "job.json"


def _write_job(job_id: str, data: dict) -> None:
    job_dir = _job_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    tmp = job_dir / "job.json.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_job_file(job_id))


def get_job(job_id: str):
    path = _job_file(job_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def set_job(job_id: str, **updates) -> None:
    with jobs_lock:
        job = get_job(job_id) or {"job_id": job_id}
        job.update(updates)
        job["updated_at"] = time.time()
        _write_job(job_id, job)


def validate_source_url(source_url: str) -> None:
    parsed = urlparse(source_url)
    if parsed.scheme.lower() != "https":
        raise ValueError("Only HTTPS source URLs are allowed.")

    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("Source URL has no hostname.")

    if "*" in ALLOWED_HOST_SUFFIXES:
        return

    if not any(host == suffix or host.endswith("." + suffix) for suffix in ALLOWED_HOST_SUFFIXES):
        raise ValueError(
            f"Source host '{host}' is not allowed. Allowed suffixes: {', '.join(ALLOWED_HOST_SUFFIXES)}"
        )


def cleanup_old_jobs() -> None:
    now = time.time()
    for directory in OUTPUT_ROOT.iterdir():
        if not directory.is_dir():
            continue
        try:
            age = now - directory.stat().st_mtime
            job = get_job(directory.name)
            if job and job.get("status") in {"queued", "processing"}:
                continue
            if age > FILE_TTL_SECONDS:
                shutil.rmtree(directory, ignore_errors=True)
        except FileNotFoundError:
            pass


def download_source(source_url: str, target: Path) -> int:
    downloaded = 0
    with requests.get(
        source_url,
        stream=True,
        allow_redirects=True,
        timeout=(DOWNLOAD_CONNECT_TIMEOUT, DOWNLOAD_READ_TIMEOUT),
    ) as response:
        response.raise_for_status()

        content_length = response.headers.get("content-length")
        if content_length and int(content_length) > MAX_DOWNLOAD_BYTES:
            raise RuntimeError(
                f"Source file is too large ({content_length} bytes). "
                f"MAX_DOWNLOAD_BYTES is {MAX_DOWNLOAD_BYTES}."
            )

        with target.open("wb") as output:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                downloaded += len(chunk)
                if downloaded > MAX_DOWNLOAD_BYTES:
                    raise RuntimeError(
                        f"Source file exceeded MAX_DOWNLOAD_BYTES ({MAX_DOWNLOAD_BYTES})."
                    )
                output.write(chunk)

    return downloaded


def process_audio_job(job_id: str, source_url: str | None, segment_seconds: int) -> None:
    job_dir = _job_dir(job_id)
    source_file = job_dir / "source_media"

    try:
        job_dir.mkdir(parents=True, exist_ok=True)

        if source_url is not None:
            set_job(job_id, status="processing", stage="downloading")
            downloaded = download_source(source_url, source_file)
            set_job(job_id, downloaded_bytes=downloaded)

        # Uploads have already been copied to source_media before the task starts.
        # Both input methods use the same conversion and chunking settings.
        set_job(job_id, status="processing", stage="splitting")

        output_pattern = job_dir / "chunk_%03d.mp3"

        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source_file),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "32k",
            "-f",
            "segment",
            "-segment_time",
            str(segment_seconds),
            "-reset_timestamps",
            "1",
            str(output_pattern),
        ]

        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg failed: {result.stderr[-4000:]}")

        chunks = sorted(path.name for path in job_dir.glob("chunk_*.mp3"))
        if not chunks:
            raise RuntimeError("FFmpeg completed, but no chunks were created.")

        try:
            source_file.unlink()
        except FileNotFoundError:
            pass

        set_job(
            job_id,
            status="done",
            stage="done",
            count=len(chunks),
            chunks=chunks,
            finished_at=time.time(),
        )

    except Exception as exc:
        try:
            source_file.unlink()
        except FileNotFoundError:
            pass
        set_job(
            job_id,
            status="error",
            stage="error",
            error=str(exc),
            finished_at=time.time(),
        )


@app.get("/")
def root():
    return {
        "service": "audio-splitter",
        "version": "2.1.0",
        "health": "/health",
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/split", status_code=202)
def split_audio(payload: SplitRequest, background_tasks: BackgroundTasks, request: Request):
    cleanup_old_jobs()

    source_url = str(payload.url)
    try:
        validate_source_url(source_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job_id = str(uuid.uuid4())
    now = time.time()
    _write_job(
        job_id,
        {
            "job_id": job_id,
            "status": "queued",
            "stage": "queued",
            "segment_seconds": payload.segment_seconds,
            "created_at": now,
            "updated_at": now,
        },
    )

    background_tasks.add_task(
        process_audio_job,
        job_id,
        source_url,
        payload.segment_seconds,
    )

    base_url = str(request.base_url).rstrip("/")
    return {
        "job_id": job_id,
        "status": "processing",
        "status_url": f"{base_url}/status/{job_id}",
        "retry_after_seconds": RETRY_AFTER_SECONDS,
    }


@app.post("/split-upload", status_code=202)
def split_uploaded_audio(
    background_tasks: BackgroundTasks,
    request: Request,
    file: UploadFile = File(...),
    segment_seconds: int = Form(default=900, ge=60, le=3600),
):
    """Accept a multipart file and queue the same processing as URL input.

    FastAPI spools the multipart upload before this function is called. Copy it
    in bounded chunks to an app-owned path: UploadFile is closed when the request
    ends and must not be passed into a background task.
    """
    cleanup_old_jobs()
    job_id = str(uuid.uuid4())
    job_dir = _job_dir(job_id)
    source_file = job_dir / "source_media"
    uploaded = 0

    try:
        if file.size is not None and file.size > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Uploaded file exceeds MAX_UPLOAD_BYTES ({MAX_UPLOAD_BYTES}).",
            )

        job_dir.mkdir(parents=True, exist_ok=True)
        with source_file.open("wb") as output:
            while chunk := file.file.read(1024 * 1024):
                uploaded += len(chunk)
                if uploaded > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Uploaded file exceeds MAX_UPLOAD_BYTES ({MAX_UPLOAD_BYTES}).",
                    )
                output.write(chunk)

        if uploaded == 0:
            raise HTTPException(status_code=400, detail="Uploaded file is empty.")

        now = time.time()
        _write_job(
            job_id,
            {
                "job_id": job_id,
                "status": "queued",
                "stage": "queued",
                "segment_seconds": segment_seconds,
                "uploaded_bytes": uploaded,
                "created_at": now,
                "updated_at": now,
            },
        )
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    finally:
        file.file.close()

    background_tasks.add_task(process_audio_job, job_id, None, segment_seconds)

    base_url = str(request.base_url).rstrip("/")
    return {
        "job_id": job_id,
        "status": "processing",
        "status_url": f"{base_url}/status/{job_id}",
        "retry_after_seconds": RETRY_AFTER_SECONDS,
    }


@app.get("/status/{job_id}")
def job_status(job_id: str, request: Request):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    response = {
        "job_id": job_id,
        "status": job.get("status"),
        "stage": job.get("stage"),
        "segment_seconds": job.get("segment_seconds"),
        "retry_after_seconds": RETRY_AFTER_SECONDS,
    }

    if "downloaded_bytes" in job:
        response["downloaded_bytes"] = job["downloaded_bytes"]
    if "uploaded_bytes" in job:
        response["uploaded_bytes"] = job["uploaded_bytes"]

    if job.get("status") == "done":
        base_url = str(request.base_url).rstrip("/")
        response.update(
            {
                "count": job.get("count", 0),
                "chunks": [
                    f"{base_url}/files/{job_id}/{filename}"
                    for filename in job.get("chunks", [])
                ],
            }
        )
    elif job.get("status") == "error":
        response["error"] = job.get("error", "Unknown error")

    return response


@app.get("/files/{job_id}/{filename}")
def get_file(job_id: str, filename: str):
    job_dir = _job_dir(job_id).resolve()
    file_path = (job_dir / filename).resolve()

    if file_path.parent != job_dir:
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not filename.startswith("chunk_") or not filename.endswith(".mp3"):
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        path=str(file_path),
        media_type="audio/mpeg",
        filename=filename,
    )
