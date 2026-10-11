# Audio Splitter for Render + Make

FastAPI service that downloads a OneDrive/SharePoint audio file, splits/transcodes it with FFmpeg, and exposes the resulting MP3 chunks over HTTP.

This version is asynchronous from Make's point of view: `POST /split` returns a `job_id` immediately, while the download and FFmpeg work continue in the background. Make can poll `GET /status/{job_id}` until the job is finished.

## Files

- `app.py` - API and background job logic
- `requirements.txt` - Python dependencies
- `Dockerfile` - Render/Docker image with FFmpeg
- `render.yaml` - optional Render Blueprint
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

`plan: starter` is only a suggested default. Change the plan in Render if necessary.

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

If you ever want to accept sources from another host, set e.g.:

```text
ALLOWED_HOST_SUFFIXES=sharepoint.com,example.com
```

Using `*` allows any HTTPS source and is less safe.

## Make scenario change

For files that need splitting, the Make branch should become:

```text
OneDrive Watch
  -> HTTP POST /split
  -> save job_id/status_url
  -> wait
  -> HTTP GET /status/{job_id}
  -> if processing: wait/check again
  -> if done: iterate chunks[]
  -> HTTP Download File
  -> OpenAI transcription
```

For small files, keep your direct OpenAI path.

Recommended router logic:

```text
< 25 MB  -> direct download -> OpenAI transcription
>= 25 MB -> /split job -> poll -> chunks -> OpenAI transcription
```

The large-file branch does not need Make to download the original 171 MB file before calling `/split`. Pass OneDrive's `@microsoft.graph.downloadUrl` directly to this service.

## Important Render limitation

This lightweight version stores job metadata and output chunks on the Render instance's local filesystem. A deploy, restart, crash, or replacement of the instance can therefore remove in-progress/completed jobs. For this Make workflow that is normally acceptable because a failed job can be submitted again.

If you later need guaranteed persistence across restarts, move job state/chunks to Redis/object storage or another persistent service.
