"""API regression checks using real local media; no external services are called."""

import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import BackgroundTasks, HTTPException, Request, UploadFile
from fastapi.testclient import TestClient

import app as service


@pytest.fixture(autouse=True)
def isolated_jobs(tmp_path, monkeypatch):
    output_root = tmp_path / "jobs"
    output_root.mkdir()
    monkeypatch.setattr(service, "OUTPUT_ROOT", output_root)
    monkeypatch.setattr(service, "MAX_UPLOAD_BYTES", 2 * 1024**3)
    monkeypatch.setattr(service, "ALLOWED_HOST_SUFFIXES", ["sharepoint.com"])

    def forbid_network(*args, **kwargs):
        raise AssertionError("Tests must not call external download URLs")

    monkeypatch.setattr(service.requests, "get", forbid_network)
    return output_root


@pytest.fixture
def client():
    with TestClient(service.app) as test_client:
        yield test_client


def run_ffmpeg(*args):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *map(str, args)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.fixture(scope="session")
def media_files(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg and ffprobe are required for real-media integration tests")
    directory = tmp_path_factory.mktemp("synthetic_media")
    wav = directory / "sample.wav"
    mp3 = directory / "sample.mp3"
    mp4 = directory / "sample.mp4"
    run_ffmpeg(
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=65",
        "-c:a", "pcm_s16le", wav,
    )
    run_ffmpeg("-i", wav, "-c:a", "libmp3lame", "-b:a", "64k", mp3)
    run_ffmpeg(
        "-f", "lavfi", "-i", "color=c=black:s=16x16:r=1:d=65",
        "-i", wav, "-c:v", "mpeg4", "-q:v", "10",
        "-c:a", "aac", "-b:a", "32k", "-shortest", mp4,
    )
    # This upload is larger than the multipart parser's in-memory spool threshold.
    assert wav.stat().st_size > 1024 * 1024
    return {"wav": wav, "mp3": mp3, "mp4": mp4}


def start_upload(client, path, *, segment_seconds="60", filename=None):
    fields = {} if segment_seconds is None else {"segment_seconds": segment_seconds}
    with path.open("rb") as content:
        return client.post(
            "/split-upload",
            files={"file": (filename or path.name, content, "application/octet-stream")},
            data=fields,
        )


def completed_status(client, response):
    assert response.status_code == 202, response.text
    started = response.json()
    assert set(started) == {"job_id", "status", "status_url", "retry_after_seconds"}
    assert started["status"] == "processing"
    status = client.get(started["status_url"])
    assert status.status_code == 200
    result = status.json()
    assert result["status"] == "done", result
    assert result["stage"] == "done"
    return result


def test_health_and_version(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/").json()["version"] == "2.1.0"


@pytest.mark.parametrize("format_name", ["wav", "mp3", "mp4"])
def test_real_file_upload_and_downloadable_audio_chunks(client, media_files, tmp_path, format_name):
    source = media_files[format_name]
    result = completed_status(client, start_upload(client, source))
    assert result["segment_seconds"] == 60
    assert result["count"] == 2
    assert result["uploaded_bytes"] == source.stat().st_size
    assert "downloaded_bytes" not in result
    durations = []
    for index, url in enumerate(result["chunks"]):
        download = client.get(url)
        assert download.status_code == 200
        assert download.headers["content-type"] == "audio/mpeg"
        downloaded_file = tmp_path / f"downloaded_{index}.mp3"
        downloaded_file.write_bytes(download.content)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_name,codec_type,sample_rate,channels:format=duration",
             "-of", "json", str(downloaded_file)],
            check=True, capture_output=True, text=True, timeout=10,
        )
        info = json.loads(probe.stdout)
        assert info["streams"] == [{
            "codec_name": "mp3", "codec_type": "audio",
            "sample_rate": "16000", "channels": 1,
        }]
        durations.append(float(info["format"]["duration"]))
    assert 59 <= durations[0] <= 61
    assert 4 <= durations[1] <= 6
    assert not (service.OUTPUT_ROOT / result["job_id"] / "source_media").exists()


def test_upload_defaults_to_fifteen_minutes(client, media_files):
    result = completed_status(client, start_upload(client, media_files["mp3"], segment_seconds=None))
    assert result["segment_seconds"] == 900
    assert result["count"] == 1


def test_url_input_remains_compatible(client, media_files, monkeypatch):
    source = media_files["mp4"]
    seen_urls = []

    def local_download(url, target):
        seen_urls.append(url)
        shutil.copyfile(source, target)
        return source.stat().st_size

    monkeypatch.setattr(service, "download_source", local_download)
    result = completed_status(client, client.post(
        "/split",
        json={"url": "https://tenant.sharepoint.com/source.mp4", "segment_seconds": 60},
    ))
    assert seen_urls == ["https://tenant.sharepoint.com/source.mp4"]
    assert result["count"] == 2
    assert result["downloaded_bytes"] == source.stat().st_size
    assert "uploaded_bytes" not in result


@pytest.mark.parametrize("url", ["https://example.com/file.mp3", "http://tenant.sharepoint.com/file.mp3"])
def test_url_input_still_rejects_disallowed_sources(client, isolated_jobs, url):
    response = client.post("/split", json={"url": url, "segment_seconds": 900})
    assert response.status_code == 400
    assert list(isolated_jobs.iterdir()) == []


def test_missing_file_is_rejected(client, isolated_jobs):
    response = client.post("/split-upload", data={"segment_seconds": "900"})
    assert response.status_code == 422
    assert list(isolated_jobs.iterdir()) == []


@pytest.mark.parametrize("seconds", ["59", "3601", "not-an-integer"])
def test_invalid_segment_seconds_is_rejected(client, isolated_jobs, seconds):
    response = client.post(
        "/split-upload", data={"segment_seconds": seconds},
        files={"file": ("input.mp3", b"test", "audio/mpeg")},
    )
    assert response.status_code == 422
    assert list(isolated_jobs.iterdir()) == []


def test_empty_file_is_rejected_and_cleaned_up(client, isolated_jobs):
    response = client.post("/split-upload", files={"file": ("empty.mp3", b"", "audio/mpeg")})
    assert response.status_code == 400
    assert "empty" in response.json()["detail"]
    assert list(isolated_jobs.iterdir()) == []


def test_oversized_upload_is_rejected_and_cleaned_up(client, isolated_jobs, monkeypatch):
    monkeypatch.setattr(service, "MAX_UPLOAD_BYTES", 4)
    response = client.post("/split-upload", files={"file": ("big.mp3", b"12345", "audio/mpeg")})
    assert response.status_code == 413
    assert "MAX_UPLOAD_BYTES" in response.json()["detail"]
    assert list(isolated_jobs.iterdir()) == []


def test_size_limit_also_counts_reads_when_upload_size_is_unknown(isolated_jobs, monkeypatch):
    # More than one copy chunk makes this exercise cleanup of an already written partial file.
    monkeypatch.setattr(service, "MAX_UPLOAD_BYTES", 1024 * 1024)
    content = io.BytesIO(b"x" * (1024 * 1024 + 1))
    upload = UploadFile(filename="unknown-size.mp3", file=content)
    request = Request({"type": "http", "method": "POST", "path": "/split-upload", "headers": []})
    tasks = BackgroundTasks()
    with pytest.raises(HTTPException) as error:
        service.split_uploaded_audio(tasks, request, upload, 900)
    assert error.value.status_code == 413
    assert content.closed
    assert tasks.tasks == []
    assert list(isolated_jobs.iterdir()) == []


def test_corrupted_media_records_error_and_removes_source(client, isolated_jobs):
    response = client.post(
        "/split-upload", files={"file": ("broken.mp3", b"this is not audio", "audio/mpeg")},
    )
    assert response.status_code == 202
    started = response.json()
    status = client.get(started["status_url"]).json()
    assert status["status"] == "error"
    assert "FFmpeg failed" in status["error"]
    assert not (isolated_jobs / started["job_id"] / "source_media").exists()


def test_submitted_filename_cannot_choose_storage_path(client, media_files, isolated_jobs):
    result = completed_status(client, start_upload(
        client, media_files["mp3"], filename="../../unexpected.mp3",
    ))
    assert result["count"] == 2
    assert not (isolated_jobs.parent / "unexpected.mp3").exists()
    assert {item.name for item in isolated_jobs.iterdir()} == {result["job_id"]}
