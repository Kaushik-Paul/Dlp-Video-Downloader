# AGENTS.md

Guidance for AI agents and developers working on this repository.

## What this project is

A personal **yt-dlp media server** designed to run as a **Hugging Face Docker Space**.
The user pastes a video URL (YouTube or any yt-dlp-supported site), picks a format,
and receives signed **stream** and **download** URLs that work in browsers, VLC, and mpv.

The server downloads media with yt-dlp into a mounted HF Storage Bucket (`/data`),
then serves it through FastAPI with HTTP Range support so players can seek.

## Repository layout

```text
├── README.md          # HF Space config (YAML front matter) + user docs. KEEP the front matter intact.
├── AGENTS.md          # this file
├── PLAN.md            # original architecture plan (reference only, may drift from code)
├── Dockerfile         # Space image: python:3.12-slim + ffmpeg + Deno, runs as UID 1000
├── requirements.txt   # fastapi, uvicorn[standard], yt-dlp[default] (intentionally unpinned)
└── main/
    ├── backend/
    │   └── app.py     # entire backend: API, yt-dlp worker, range streaming, static serving
    └── frontend/
        ├── index.html # single-page UI markup (no framework)
        ├── style.css  # dark gradient theme, CSS variables in :root
        └── app.js     # vanilla JS: job polling, library, clipboard, toasts
```

## Architecture in brief

```text
Browser UI (/static/*, /)
    → POST /api/jobs            (password-checked, starts background thread)
    → GET  /api/jobs/{id}       (polled every 2s until ready/error)
    → GET  /media/{id}?expires&sig&download=0|1   (HMAC-signed, Range-enabled)
    → POST /api/library         (lists bucket contents)
    → POST /api/jobs/{id}/delete
```

- One download runs at a time (`threading.Semaphore(1)`); others queue. This is
  deliberate for HF CPU Basic hardware. Do not raise the limit without reason.
- In-memory `jobs` dict tracks live jobs; finished jobs persist as
  `/data/media/{job_id}/_meta.json` + media file, and are recovered after restarts.
- Media URLs are HMAC-SHA256 signed (`SIGNING_SECRET`) and expire after
  `LINK_TTL_HOURS` (default 168). Signing survives restarts as long as the secret
  is unchanged.

## Environment variables

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `APP_PASSWORD` | yes (on HF) | `""` | Guards job create/delete and library. Empty = 500 error by design. |
| `SIGNING_SECRET` | recommended | falls back to `APP_PASSWORD` | HMAC key for media URLs |
| `LINK_TTL_HOURS` | no | `168` | Media URL lifetime |
| `MEDIA_DIR` | no | `/data/media` | Download root (bucket mount on HF) |
| `SPACE_HOST` | auto on HF | — | Used to build absolute public URLs |

Never hardcode secrets. On HF they come from Space Secrets; locally use `export`.

## Running locally

```bash
pip install -r requirements.txt
# ffmpeg must be installed on the system (merging streams, MP3/M4A extraction)
export APP_PASSWORD=dev-password
export MEDIA_DIR=./data/media
uvicorn main.backend.app:app --host 0.0.0.0 --port 7860
```

Quick checks after backend changes:

```bash
python3 -m py_compile main/backend/app.py
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:7860/   # expect 200
```

There is no test suite yet; verify manually via the UI and `curl` against the API.

## Coding conventions

**Backend (`main/backend/app.py`)**
- Standard library first; the only third-party deps are FastAPI/uvicorn/pydantic/yt-dlp.
  Do not add dependencies without updating `requirements.txt` and this file.
- yt-dlp is invoked as a **subprocess**, not the Python API. Build the argument list;
  never shell-concatenate user input (`subprocess.run` with a list, no `shell=True`).
- All mutating endpoints must call `verify_password()`.
- Job state changes go through `update_job()` / `get_job()` (lock-protected).
- Anything written to disk belongs under `MEDIA_ROOT / job_id`; `_meta.json` is the
  source of truth for filename/size/mode. Filenames are user-influenced (video titles),
  so always serve via the stored metadata path, never via URL-supplied paths.
- Keep the existing section-banner comment style (`# -----...-----`).

**Frontend (`main/frontend/`)**
- Vanilla HTML/CSS/JS only. No build step, no frameworks, no CDN dependencies.
  The Space must work fully self-contained.
- Theme via CSS custom properties in `:root` (`--a1`, `--a2` are the accent gradient).
- All API calls live in `app.js`; keep `fetch` error handling pattern
  (`data.detail || "fallback"`) consistent.
- New UI text must be plain ASCII-safe where possible; existing emoji usage is fine.

**Dockerfile**
- Must keep working as UID 1000 (`user`) with writable `/data` — HF requirement.
- Deno install is required by current yt-dlp for YouTube JS challenges. Do not remove.
- If you add files under `main/`, no Dockerfile change is needed (`COPY main/ ./main/`).

## Common tasks

**Add a new download mode (e.g. `480p`)**
1. Add the mode string to `VALID_MODES` in `app.py`.
2. Add an `elif mode == "..."` branch in `run_download()` with the yt-dlp `-f` selector.
3. Add a `<label class="pill">` radio in `index.html`.
No other changes needed; mode flows through metadata and badges automatically.

**Add a new API endpoint**
1. Add the route in `app.py` under the API section.
2. If it mutates state, require the password via `PasswordRequest` + `verify_password()`.
3. Add the caller in `app.js` and document it in `README.md`'s API table.

**Change the UI theme**
Edit only the `:root` variables and rules in `style.css`. Markup hooks are the
existing class names (`.card`, `.pill`, `.btn-primary`, `.btn-ghost`, `.btn-danger`, ...).

## Gotchas

- **YouTube bot checks**: datacenter IPs may hit `HTTP Error 403` /
  "Sign in to confirm you're not a bot". The fix is a PO Token provider plugin in the
  Docker image, not code changes here. See PLAN.md's final section.
- **Ephemeral disk**: anything outside `/data` vanishes on Space restart. Never store
  media elsewhere.
- **Range requests**: `serve_media` implements them by hand. If you touch it, re-test
  seeking in VLC (`Range: bytes=N-`, suffix ranges, and 416 responses).
- **Signature expiry**: old links die after `LINK_TTL_HOURS`; the UI regenerates fresh
  links on every poll/library load, so prefer re-fetching over caching URLs client-side.
- **`yt-dlp[default]`** extra is intentional (bundles EJS challenge scripts). Keep it.
- **PLAN.md is not authoritative** — when it disagrees with the code, the code wins.
  Update README.md when behavior changes.
