# Audio Splitter for Render + Make — 2.1.0

FastAPI service that accepts either a OneDrive/SharePoint download URL or a direct file upload, splits/transcodes the media with FFmpeg, and exposes the resulting MP3 chunks over HTTP. MP3/WAV audio and MP4 video with an audio track can use either input method.

The existing `POST /split` JSON interface remains available. The new `POST /split-upload` accepts multipart file data from Make. Both return the same `job_id` and `status_url` response and run the FFmpeg work in the background. A file upload must finish and be saved before its `202 Accepted` response is returned; conversion does not have to finish first. Your existing size branches, fixed waits, status requests and chunk processing can remain in place.

## Files

- `app.py` - API and background job logic
- `requirements.txt` - Python dependencies
- `Dockerfile` - Render/Docker image with FFmpeg
- `render.yaml` - optional Render Blueprint
- `MAKE_UPLOAD_DA.md` - Danish Make configuration and deployment instructions
- `.gitignore`

## API

### Health

```http
GET /health
```

Response:

```json
{"status":"ok"}
```

### Start split job

```http
POST /split
Content-Type: application/json
```

Body:

```json
{
  "url": "https://YOUR-TENANT.sharepoint.com/...",
  "segment_seconds": 900
}
```

Immediate response (`202 Accepted`):

```json
{
  "job_id": "...",
  "status": "processing",
  "status_url": "https://YOUR-RENDER-SERVICE/status/...",
  "retry_after_seconds": 30
}
```

### Start a job by uploading the actual file

```http
POST /split-upload
Content-Type: multipart/form-data; boundary=...
```

| Form field | Type | Value |
| --- | --- | --- |
| `file` | File | Actual file bytes and a filename. Required. |
| `segment_seconds` | Text/integer | Optional; default `900`; allowed range `60`–`3600`. |

For example:

```bash
curl --request POST 'https://YOUR-RENDER-SERVICE/split-upload' \
  --form 'file=@podcast.mp3' \
  --form 'segment_seconds=900'
```

Let your HTTP client create the multipart boundary; do not set a bare `Content-Type` header manually. This endpoint accepts a multipart file, not a base64 string, a JSON `data` field, or an unframed binary request body.

The successful response is the same `202 Accepted` shape as `/split`. Empty files return `400`; files larger than `MAX_UPLOAD_BYTES` return `413`; a missing file or invalid segment length returns `422`. If FFmpeg cannot read a file after it has been accepted, the job's status becomes `error` and `/status/{job_id}` provides the reason.

The upload is copied to an application-owned job file before background processing starts. The request's temporary upload handle is not used by the background task. The submitted filename is never used as a filesystem path. FastAPI may spool multipart files to temporary disk before endpoint validation; the byte limit is an accepted-file limit, not an upstream request-body limit.

### Check job

```http
GET /status/{job_id}
```

While processing:

```json
{
  "job_id": "...",
  "status": "processing",
  "stage": "downloading",
  "segment_seconds": 900,
  "retry_after_seconds": 30
}
```

or:

```json
{
  "job_id": "...",
  "status": "processing",
  "stage": "splitting",
  "segment_seconds": 900,
  "retry_after_seconds": 30,
  "downloaded_bytes": 171201643
}
```

When finished:

```json
{
  "job_id": "...",
  "status": "done",
  "stage": "done",
  "segment_seconds": 900,
  "retry_after_seconds": 30,
  "count": 3,
  "chunks": [
    "https://YOUR-RENDER-SERVICE/files/JOB-ID/chunk_000.mp3",
    "https://YOUR-RENDER-SERVICE/files/JOB-ID/chunk_001.mp3",
    "https://YOUR-RENDER-SERVICE/files/JOB-ID/chunk_002.mp3"
  ]
}
```

On failure:

```json
{
  "job_id": "...",
  "status": "error",
  "stage": "error",
  "segment_seconds": 900,
  "retry_after_seconds": 30,
  "error": "..."
}
```

## GitHub setup

1. Create a new empty GitHub repository, e.g. `audio-splitter`.
2. Upload all files from this project to the repository root.
3. Commit them to the default branch.

## Render setup - easiest method

1. In Render, create a new **Web Service**.
2. Connect the new GitHub repository.
3. Render should detect the `Dockerfile`.
4. Runtime: Docker.
5. Health check path: `/health`.
6. Use **one instance / one Uvicorn worker**.
7. Deploy.

The `Dockerfile` starts:

```bash
uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000} --workers 1
```

Do not increase the Uvicorn worker count for this simple version.

## Optional Render Blueprint

The repository also contains `render.yaml`. You can create a Blueprint in Render from the repository instead of manually creating the Web Service.

## Environment variables

Defaults are already included in the Docker image:

| Variable | Default | Meaning |
|---|---:|---|
| `OUTPUT_ROOT` | `/tmp/audio-splitter` | Temporary job/chunk directory |
| `FILE_TTL_SECONDS` | `21600` | Delete completed jobs after 6 hours |
| `RETRY_AFTER_SECONDS` | `30` | Suggested Make polling interval |
| `ALLOWED_HOST_SUFFIXES` | `sharepoint.com` | Allowed source URL host suffix |
| `DOWNLOAD_CONNECT_TIMEOUT` | `30` | HTTP connect timeout in seconds |
| `DOWNLOAD_READ_TIMEOUT` | `900` | Source download read timeout |
| `MAX_DOWNLOAD_BYTES` | `2147483648` | Max input file size, default 2 GiB |
| `MAX_UPLOAD_BYTES` | Value of `MAX_DOWNLOAD_BYTES` | Max accepted uploaded file size. Optional separate override. |

If you ever want to accept sources from another host, set e.g.:

```text
ALLOWED_HOST_SUFFIXES=sharepoint.com,example.com
```

Using `*` allows any HTTPS source and is less safe.

## Make scenario change

For an existing URL-based branch, keep `POST /split` and its JSON body unchanged.

For a branch that should send Make's actual file data, change only the starting HTTP request:

1. Set the URL to `https://YOUR-RENDER-SERVICE/split-upload` and Method to `POST`.
2. Choose **Body content type → multipart/form-data (supports file uploads)**.
3. Add a body item: **Name** `file`, **Field type** `File`, **Data** from the source module's binary Data output, and **File name** from that same module.
4. Add a second item: **Name** `segment_seconds`, **Field type** `Text`, **Value** `900`.
5. Keep **Parse response** enabled and retain the existing timeout, fixed wait modules and subsequent GET request to the returned `status_url`.

For OneDrive module 18, use `{{18.data}}` and `{{18.filename}}`. For a CloudConvert result, choose both Data and Filename from the current CloudConvert module instead. Do not combine one module's data with a different module's filename.

No router thresholds, wait durations, loops or automatic retries are introduced by this update. The existing `/status/{job_id}` and `/files/{job_id}/{filename}` interfaces remain available. Upload jobs additionally expose `uploaded_bytes` in their status; URL jobs retain `downloaded_bytes`.

## Updating the existing Render deployment

Replace `app.py` and `requirements.txt` in the source repository connected to your existing Render service, then deploy that commit. `python-multipart==0.0.32` is required for the new file-upload endpoint, so a normal Docker rebuild/deploy is required. Merely restarting an older image does not install the change. The Dockerfile and Render Blueprint do not need changes.

After deployment, `GET /` reports version `2.1.0`, and `/docs` includes `POST /split-upload`. Existing `/split` callers can continue using the old input format. Switch a Make branch to the upload endpoint only after this version is deployed.

## Local verification

Install the development dependencies with `pip install -r requirements-dev.txt`, ensure `ffmpeg` and `ffprobe` are available, and run `pytest`. Integration tests generate synthetic audio/video locally; they do not call Make, Render, OneDrive or OpenAI.

## Important Render limitation

This lightweight version stores job metadata and output chunks on the Render instance's local filesystem. A deploy, restart, crash, or replacement of the instance can therefore remove in-progress/completed jobs. For this Make workflow that is normally acceptable because a failed job can be submitted again.

If you later need guaranteed persistence across restarts, move job state/chunks to Redis/object storage or another persistent service.
