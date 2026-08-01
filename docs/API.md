# Douyin Downloader — REST API

HTTP API for dispatching Douyin download jobs. It is a thin async layer: you
`POST` a URL, get back a `job_id` immediately, then poll for status. The actual
download runs in the background.

- **Source:** [`server/app.py`](../server/app.py) (FastAPI) + [`server/jobs.py`](../server/jobs.py) (job model)
- **API version prefix:** `/api/v1`
- **Auth:** none (intended to run on localhost / a trusted network — do **not** expose publicly without your own gateway)
- **Content type:** `application/json`

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `GET`  | `/api/v1/health` | Liveness check |
| `POST` | `/api/v1/resolve` | **Info-only** metadata resolve (no download) |
| `POST` | `/api/v1/download` | Submit a download job (async) |
| `GET`  | `/api/v1/jobs/{job_id}` | Poll one job (now includes `files[]`) |
| `GET`  | `/api/v1/jobs` | List all jobs |
| `GET`  | `/api/v1/jobs/{job_id}/files/{index}` | **Stream** a finished job's file bytes |

---

## Running the server

`fastapi` and `uvicorn` are **optional** dependencies. Install them first:

```bash
pip install fastapi uvicorn
```

Start the server via the CLI:

```bash
python run.py --serve --serve-host 127.0.0.1 --serve-port 8000
```

| Flag | Default | Meaning |
|------|---------|---------|
| `--serve` | — | Enable REST server mode |
| `--serve-host` | `127.0.0.1` | Listen address |
| `--serve-port` | `8000` | Listen port |

Base URL is then `http://<host>:<port>`. All examples below assume
`http://127.0.0.1:8000`.

Auto-generated interactive docs (provided by FastAPI):
`/docs` (Swagger UI), `/redoc`, `/openapi.json`.

---

## Endpoints

### 1. `GET /api/v1/health`

Liveness check.

**Response `200`**
```json
{ "status": "ok" }
```

```bash
curl http://127.0.0.1:8000/api/v1/health
```

---

### 2. `POST /api/v1/resolve` — info only, no download

Resolve a Douyin URL (including `v.douyin.com/...` short links) into video
metadata **without downloading any media**. Fast: it makes a single signed
metadata call (`/aweme/v1/web/aweme/detail/`) and reads fields straight off the
response — duration comes from metadata, **not** ffprobe.

**Request body**
```json
{ "url": "https://www.douyin.com/video/7123456789012345678" }
```

**Response `200`** — ⚠️ keys are **camelCase**
```json
{
  "awemeId": "7123456789012345678",
  "durationSeconds": 246.36,
  "coverUrl": "https://p3-pc-sign.douyinpic.com/....jpeg?...",
  "title": "视频标题"
}
```

| Field | Type | Notes |
|-------|------|-------|
| `awemeId` | string | The aweme id |
| `durationSeconds` | float | Seconds (from `video.duration` ms ÷ 1000); `0.0` if the item has no video track (e.g. image note) |
| `coverUrl` | string \| null | Thumbnail; `null` if unavailable |
| `title` | string \| null | Caption/`desc`; `null` if empty |

**Errors** — the NestJS client maps any `4xx` → `invalid_url`, and network
error / `5xx` / timeout → `unreachable`.

| Status | When | Body |
|--------|------|------|
| `400` | `url` empty / whitespace | `{"detail": "url is required"}` |
| `422` | URL unparseable, or not a single video/note | `{"detail": "unsupported URL: ..."}` |
| `404` | short link won't resolve, or metadata not found | `{"detail": "metadata not found for aweme ..."}` |
| `502` | upstream/network error while resolving | `{"detail": "upstream error: ..."}` |

```bash
curl -X POST http://127.0.0.1:8000/api/v1/resolve \
  -H "Content-Type: application/json" \
  -d '{"url":"https://v.douyin.com/abcd1234/"}'
```

> Resolving needs the same valid cookies as downloading — it hits Douyin's
> signed web API. Cookies are loaded server-side from config / `.cookies.json`.

---

### 3. `POST /api/v1/download`

Submit a Douyin URL. Returns immediately with a `pending` job; the download
runs in the background. Supported URL types include single videos, user
profiles, mixes/collections, music, and live — plus short links
(`v.douyin.com/...`), which are resolved server-side.

**Request body**
```json
{ "url": "https://www.douyin.com/video/7123456789012345678" }
```

**Response `200`** — `JobResponse`
```json
{
  "job_id": "def615fe2ae8",
  "status": "pending",
  "url": "https://www.douyin.com/video/7123456789012345678"
}
```

**Errors**
| Status | When | Body |
|--------|------|------|
| `400` | `url` present but empty | `{"detail": "url is required"}` |
| `422` | `url` field missing / wrong type | FastAPI validation error object |

```bash
curl -X POST http://127.0.0.1:8000/api/v1/download \
  -H "Content-Type: application/json" \
  -d '{"url":"https://v.douyin.com/abcd1234/"}'
```

> **Note:** a `200` here only means the job was *accepted*, not that the
> download succeeded. Poll the job (below) to learn the outcome.

---

### 4. `GET /api/v1/jobs/{job_id}`

Fetch one job's current state. Poll this until `status` is terminal
(`success` or `failed`). When `status == "success"`, `files[]` lists the files
the job wrote — fetch their bytes via endpoint 6.

**Response `200`** — full job object
```json
{
  "job_id": "def615fe2ae8",
  "url": "https://www.douyin.com/video/7123456789012345678",
  "status": "success",
  "created_at": "2026-07-11T21:42:23.365811Z",
  "started_at": "2026-07-11T21:42:23.365811Z",
  "finished_at": "2026-07-11T21:42:24.490507Z",
  "total": 1,
  "success": 1,
  "failed": 0,
  "skipped": 0,
  "error": null,
  "files": [
    { "index": 0, "bytes": 5242880, "filename": "2026-04-11_title_7123.mp4" }
  ]
}
```

`files` is always present (`[]` until the job succeeds). Each entry:

| Field | Type | Description |
|-------|------|-------------|
| `index` | int | Position in the array; pass to endpoint 6 to stream it |
| `bytes` | int | File size on disk |
| `filename` | string | Base filename (follows the config naming template) |

**Error**
| Status | When | Body |
|--------|------|------|
| `404` | unknown `job_id` | `{"detail": "job not found"}` |

```bash
curl http://127.0.0.1:8000/api/v1/jobs/def615fe2ae8
```

---

### 5. `GET /api/v1/jobs`

List all jobs currently held in memory.

**Response `200`**
```json
{
  "jobs": [
    { "job_id": "def615fe2ae8", "status": "success", "...": "..." }
  ]
}
```

Each array element has the same shape as the single-job response.

```bash
curl http://127.0.0.1:8000/api/v1/jobs
```

---

### 6. `GET /api/v1/jobs/{job_id}/files/{index}`

Stream the raw bytes of one file produced by a finished job. `index` is the
position in that job's `files[]` array (from endpoint 4).

- **`200`** — the file stream. `Content-Type` is guessed from the filename
  (e.g. `video/mp4`, `image/jpeg`, `audio/mpeg`), falling back to
  `application/octet-stream`. `Content-Length` is set; `Content-Disposition`
  carries the filename.
- **`404`** — unknown `job_id`, `index` out of range, or the file is no longer
  on disk.

```bash
# download job def615fe2ae8's first file to disk
curl -OJ http://127.0.0.1:8000/api/v1/jobs/def615fe2ae8/files/0
```

> Files live on the sidecar's disk under `<download path>/<job_id>/...` until
> the job is pruned from memory (see caveats). Fetch them before then, or copy
> them out on your side.

---

## Data model

### Job object

| Field | Type | Description |
|-------|------|-------------|
| `job_id` | string | 12-char hex id |
| `url` | string | The submitted URL |
| `status` | string | `pending` → `running` → `success` \| `failed` |
| `created_at` | string (ISO-8601 UTC, `Z`) | When submitted |
| `started_at` | string \| null | When execution began |
| `finished_at` | string \| null | When it reached a terminal state |
| `total` | int | Items discovered for this URL |
| `success` | int | Items downloaded successfully |
| `failed` | int | Items that failed |
| `skipped` | int | Items skipped (e.g. already present) |
| `error` | string \| null | Error message when `status == "failed"` |
| `files` | array | Files written by the job (`[]` until success); entries are `{index, bytes, filename}` — stream via endpoint 6 |

### Status values

| Status | Meaning |
|--------|---------|
| `pending` | Accepted, waiting for a worker slot |
| `running` | Download in progress |
| `success` | Finished with `failed == 0` |
| `failed` | Job raised an exception, **or** finished with `failed > 0`. Check `error` and the counts. |

---

## Recommended integration flow

```
POST /api/v1/download        ──▶  { job_id }
      │
      ▼
GET  /api/v1/jobs/{job_id}    ──▶  poll every ~1–2s
      │
      └── until status ∈ { success, failed }
```

Example (pseudo):
```python
# Optional: cheap metadata pre-check before committing to a download.
meta = POST("/api/v1/resolve", {"url": url})   # { awemeId, durationSeconds, ... }

job = POST("/api/v1/download", {"url": url})["job_id"]
while True:
    j = GET(f"/api/v1/jobs/{job}")
    if j["status"] in ("success", "failed"):
        break
    time.sleep(1.5)
# on success, pull the bytes:
for f in j["files"]:
    GET(f"/api/v1/jobs/{job}/files/{f['index']}")   # streamed file
```

---

## Operational notes / caveats

- **Per-job output dir.** Each download job writes into its own
  `<download path>/<job_id>/` subtree so its `files[]` can be attributed
  unambiguously (jobs run concurrently). The download manifest and partial
  `.tmp` files are excluded from `files[]`.
- **File lifetime.** `files[]` is populated on `success` and the bytes stay on
  disk, but the *job record* (and thus the `files/{index}` route) disappears
  when the job is pruned from memory (below). Fetch files before then.
- **In-memory only.** Jobs are **not persisted**; a server restart drops all
  job history. Don't treat `job_id` as durable.
- **Pruning.** Terminal jobs are evicted by TTL (default **24h**) and a
  capacity cap (default **500** jobs). In-flight jobs are never evicted.
  Configurable via the `server` block in config:
  ```yaml
  server:
    max_jobs: 500
    job_ttl_seconds: 86400
  ```
- **Concurrency.** Background execution is bounded by a semaphore
  (`thread` config value, default 2). Extra submissions queue as `pending`.
- **Cookies.** Downloads use cookies from config / `.cookies.json` next to the
  config file. Without valid cookies, many URLs will fail — surfaced as a
  `failed` job with an `error`, not an HTTP error.
- **No auth / no rate limit at the HTTP layer.** Put it behind your own
  gateway if it's reachable beyond localhost.
