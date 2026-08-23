# AGENTS.md

Guidance for AI agents and developers working on this repository.

## What this project is

A personal **yt-dlp media server** designed to run as a **Hugging Face Docker Space**.
The user pastes a video URL (YouTube or any yt-dlp-supported site), picks a format
and a delivery mode, and gets either:

- **Instant source link** — yt-dlp extracts the provider's direct CDN URL. Nothing is
  downloaded or stored; the link is provider-controlled and may expire quickly.
- **Store for 30 days** — the media is downloaded into the mounted HF Storage Bucket
  (`/data`) and served through FastAPI with signed URLs and HTTP Range support so
  players can seek.
- **Upload local file** — the browser sends bounded parallel raw chunks, which are
  written directly to their final offsets in one open bucket file. Completed uploads
  use the same metadata, retention, library, and signed-link path as stored downloads.

## Repository layout

```text
├── README.md                  # HF Space config (YAML front matter) + user docs. KEEP front matter intact.
├── AGENTS.md                  # this file
├── PLAN.md                    # original architecture plan (reference only, may drift from code)
├── Dockerfile                 # python:3.12-slim + ffmpeg + Deno + BgUtils PO-token provider, UID 1000
├── requirements.txt           # fastapi, uvicorn[standard], yt-dlp[default,curl-cffi], bgutil provider
├── .dockerignore              # excludes dev files, PLAN.md, AGENTS.md from image context
├── LICENCE                    # MIT
└── main/
    ├── backend/
    │   └── app.py             # entire backend: API, yt-dlp workers, range streaming, static serving
    ├── frontend/
    │   ├── index.html         # single-page UI markup (no framework)
    │   ├── style.css          # dark gradient theme, CSS variables in :root
    │   └── app.js             # vanilla JS: job polling, library, clipboard, toasts
    └── scripts/
        └── deploy_space.py    # uploads an allowlist of files to the HF Space via the `hf` CLI
```

## Architecture in brief

```text
Browser UI (/static/*, /)
    → POST /api/jobs            (password; delivery: "instant" | "stored")
    → POST /api/uploads         (create parallel local-file upload)
    → PUT  /api/uploads/{id}/chunks/{index}
    → POST /api/uploads/{id}/complete|abort
    → POST /api/transfers/cancel (cancel all ingestion; site protection, no app password)
    → GET  /api/jobs/{id}       (polled until ready/error)
    → POST /api/library         (lists bucket contents, triggers retention cleanup)
    → POST /api/jobs/{id}/delete
    → GET/HEAD /media/{id}?expires&sig&download=0|1   (HMAC-signed, Range-enabled)
```

- Two worker paths in `app.py`: `run_instant_link()` (extraction only,
  `--dump-single-json`, no download) and `run_download()` (download + optional
  ffmpeg merge/conversion). Selected by the `delivery` field.
- One download runs at a time (`threading.Semaphore(1)`); others queue. This is
  deliberate for HF CPU Basic hardware. Do not raise the limit without reason.
  Instant extraction also runs in a thread but is capped by its own 120s timeout.
- In-memory `jobs` dict tracks live jobs; finished **stored** jobs persist as
  `/data/media/{job_id}/_meta.json` + media file and are recovered after restarts.
  **Instant** jobs exist only in memory, never appear in the library, and are lost
  on restart by design.
- Media URLs are HMAC-SHA256 signed (`SIGNING_SECRET`) and expire after the lesser
  of `LINK_TTL_HOURS` (default 720) and the item's fixed retention deadline.
- Retention: stored media is permanently deleted `MEDIA_RETENTION_DAYS` (default 30)
  days after download. Cleanup runs at startup and every 6 hours
  (`cleanup_loop` / `cleanup_expired_media`); the library endpoint also sweeps.
  Expired media returns HTTP `410`; signed links past expiry return `403`.
- Instant results can return separate video/audio provider URLs
  (`separate_streams: true` with `video_url` + `audio_url` fields).

## Environment variables

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `APP_PASSWORD` | yes (on HF) | `""` | Guards job create/delete and library. Empty = 500 error by design. |
| `SIGNING_SECRET` | recommended | falls back to `APP_PASSWORD` | HMAC key for media URLs. |
| `LINK_TTL_HOURS` | no | `720` | Max lifetime of new signed links (capped by retention deadline). Must be > 0. |
| `MEDIA_RETENTION_DAYS` | no | `30` | Stored media deleted this many days after download. Must be > 0. |
| `MEDIA_DIR` | no | `/data/media` | Download root (bucket mount on HF). |
| `UPLOAD_MAX_GIB` | no | `20` | Maximum local-file upload size. Must be > 0. |
| `UPLOAD_CHUNK_MIB` | no | `32` | Raw upload request size. Must be 5-128 MiB. |
| `UPLOAD_CONCURRENCY` | no | `6` | Parallel browser upload requests. Must be 1-12. |
| `DOWNLOAD_FRAGMENT_CONCURRENCY` | no | `4` | yt-dlp HLS/DASH fragment concurrency. Must be 1-8. |
| `SPACE_HOST` | auto on HF | — | Used to build absolute public URLs. |
| `YTDLP_PROXY` | no | `""` | HTTP/SOCKS proxy for yt-dlp (`http/https/socks4/socks4a/socks5/socks5h`). Validated at startup. Keep as an HF secret. |
| `YOUTUBE_COOKIES_B64` | no | `""` | Base64 Netscape cookie file; decoded to ephemeral `/tmp/youtube-cookies.txt` (mode 0600). |

Never hardcode secrets. On HF they come from Space Secrets; locally use `export`.

## Running locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# ffmpeg must be installed on the system (merging streams, MP3/M4A extraction)
export APP_PASSWORD=dev-password
export MEDIA_DIR=./data/media
uvicorn main.backend.app:app --host 0.0.0.0 --port 7860
```

Quick checks after backend changes:

```bash
python3 -m py_compile main/backend/app.py
python3 main/scripts/deploy_space.py --dry-run    # verifies deploy allowlist
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:7860/   # expect 200
```

There is no test suite yet; verify manually via the UI and `curl` against the API.

## Coding conventions

**Backend (`main/backend/app.py`)**
- Standard library first; the only third-party deps are FastAPI/uvicorn/pydantic/yt-dlp
  (plus the bgutil PO-token provider plugin). Do not add dependencies without
  updating `requirements.txt` and this file.
- yt-dlp is invoked as a **subprocess**, not the Python API. Build the argument list;
  never shell-concatenate user input (`subprocess.run` with a list, no `shell=True`).
- All yt-dlp network flags go through `yt_dlp_network_args()` (`--force-ipv4`,
  `--impersonate chrome`, `youtube:player_client=mweb`, optional proxy/cookies).
  Do not duplicate those flags at call sites.
- Sanitize errors with `yt_dlp_error()`: it redacts the configured proxy from
  stderr and maps "Sign in to confirm you're not a bot" to user-facing guidance.
  Never surface raw stderr to clients any other way.
- All mutating endpoints must call `verify_password()`, except
  `POST /api/transfers/cancel`: it intentionally relies on site-level protection and
  can only stop active/queued ingestion transfers.
- Job state changes go through `update_job()` / `get_job()` (lock-protected).
  Job IDs are `uuid4().hex` and must match `JOB_ID_PATTERN` (`^[0-9a-f]{32}$`).
- Anything written to disk belongs under `MEDIA_ROOT / job_id`; `_meta.json` is the
  source of truth for filename/size/mode/retention. Filenames are user-influenced
  (video titles), so always serve via the stored metadata path, never via
  URL-supplied paths; `load_metadata()` rejects anything inconsistent.
- Upload chunks are raw request bodies, not `multipart/form-data`. Keep writes bounded,
  offset-based, and idempotent so chunks can run in parallel and retry safely. Never
  create one persistent bucket file per chunk or buffer a complete upload in memory.
- Transfer subprocesses must be created through `run_transfer_process()` so global
  cancellation terminates the complete yt-dlp/FFmpeg process group. Never register
  `/media/...` responses as transfers; cancellation must not interrupt streaming.
- Keep the existing section-banner comment style (`# -----...-----`).

**Frontend (`main/frontend/`)**
- Vanilla HTML/CSS/JS only. No build step, no frameworks, no CDN dependencies.
  The Space must work fully self-contained.
- Theme via CSS custom properties in `:root` (`--a1`, `--a2` are the accent gradient).
- All API calls live in `app.js`; keep the `fetch` error handling pattern
  (`data.detail || "fallback"`) consistent.
- Local uploads deliberately use `XMLHttpRequest` for upload progress; chunk creation,
  retries, concurrency, cancellation, and finalization remain in `app.js`.
- New UI text must be plain ASCII-safe where possible; existing emoji usage is fine.

**Dockerfile**
- Must keep working as UID 1000 (`user`) with writable `/data` — HF requirement.
- Deno install is required by current yt-dlp for YouTube JS challenges. Do not remove.
- The BgUtils PO-token provider (Deno server under `/home/user/bgutil-ytdlp-pot-provider`)
  plus the matching Python plugin in `requirements.txt` generate YouTube PO tokens
  without account cookies. Keep both halves in sync (same version).
- If you add files under `main/`, no Dockerfile change is needed (`COPY main/ ./main/`).
  Root files must be part of the image context and pass `.dockerignore`.

**Deployment (`main/scripts/deploy_space.py`)**
- Deploys via the authenticated `hf` CLI, not git push. It uploads an explicit
  allowlist: root files (`.dockerignore`, `Dockerfile`, `LICENCE`, `README.md`,
  `requirements.txt`) plus everything under `main/`, excluding caches/env files.
  `AGENTS.md` and `PLAN.md` are never deployed. New root files must be added to
  `ROOT_FILES` or they will not ship.

## Common tasks

**Add a new download mode (e.g. `480p`)**
1. Add the mode string to `VALID_MODES` in `app.py`.
2. Add an `elif mode == "..."` branch in `run_download()` with the yt-dlp `-f` selector.
3. Add the format to `instant_format_selector()` if it makes sense for instant
   delivery (instant cannot convert; mp3/m4a are stored-only and are rejected in
   `create_job`).
4. Add a `<label class="pill">` radio in `index.html`.
No other changes needed; mode flows through metadata and badges automatically.

**Add a new API endpoint**
1. Add the route in `app.py` under the API section.
2. If it mutates state, require the password via `PasswordRequest` + `verify_password()`.
3. Add the caller in `app.js` and document it in `README.md`'s API table.

**Change the UI theme**
Edit only the `:root` variables and rules in `style.css`. Markup hooks are the
existing class names (`.card`, `.pill`, `.delivery-card`, `.btn-primary`,
`.btn-ghost`, `.btn-danger`, ...).

## Gotchas

- **YouTube bot checks**: the image ships Deno, yt-dlp's `curl-cffi` browser
  transport, and the BgUtils PO-token provider on the `mweb` client. Heavily shared
  datacenter IPs can still get 403s; the fallbacks are the `YTDLP_PROXY` secret
  (recommended) or `YOUTUBE_COOKIES_B64` (account-ban risk). See README.md
  Troubleshooting; do not try to "fix" this with code changes in `app.py`.
- **Instant links are bearer URLs to the provider**, not to this server. They live
  only in the in-memory `jobs` dict, are never written to the bucket, never appear
  in `/api/library`, and are not governed by `LINK_TTL_HOURS` /
  `MEDIA_RETENTION_DAYS`. Their lifetime is whatever the provider embedded in the
  URL (`source_expires` is a best-effort parse of `expire`/`expires`/`exp` params).
- **Ephemeral disk**: anything outside `/data` vanishes on Space restart. Never
  store media elsewhere; `/tmp/youtube-cookies.txt` is deliberately ephemeral.
- **Range requests**: `serve_media` implements them by hand (GET and HEAD). If you
  touch it, re-test seeking in VLC (`Range: bytes=N-`, suffix ranges, and 416 responses).
- **Retention vs link TTL**: `create_media_links` caps link expiry at the item's
  `retention_expires`. Don't issue links that outlive the media; expired media must
  return `410` and be removed.
- **Filename handling**: the output template is `%(title).180B [%(id)s].%(ext)s`;
  the stored filename in `_meta.json` is authoritative for serving.
- **`yt-dlp[default,curl-cffi]`** extra is intentional (challenge scripts +
  browser TLS transport). Keep it.
- **PLAN.md is not authoritative** — when it disagrees with the code, the code wins.
  Update README.md when behavior changes.
