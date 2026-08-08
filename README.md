---
title: YT-DLP Media Server
emoji: 📥
colorFrom: blue
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
---

# YT-DLP Media Server

Personal [yt-dlp](https://github.com/yt-dlp/yt-dlp) download and streaming server, built for Hugging Face Docker Spaces.

Paste a video URL (YouTube and hundreds of other sites), pick a format, and get back:

- a **Stream URL** for VLC / mpv / browser players (HTTP Range support, so seeking works without re-downloading)
- a **Download URL** for saving the file directly

## Features

- Best quality, 1080p, 720p, MP3 and M4A modes
- Background downloading with live status polling
- Persistent storage via a mounted HF Storage Bucket (`/data`)
- Signed, expiring media URLs (HMAC-SHA256)
- Password protection for creating/deleting jobs and browsing the library
- Media library showing everything already stored in the bucket
- Inline video/audio preview player
- Survives Space restarts: finished jobs are recovered from bucket metadata

## Repository layout

```text
├── README.md          # this file (HF Space config lives in the YAML header above)
├── Dockerfile         # Docker Space image (Python + ffmpeg + Deno)
├── requirements.txt   # fastapi, uvicorn, yt-dlp
└── main/
    ├── backend/
    │   └── app.py     # FastAPI app: API + range streaming + static serving
    └── frontend/
        ├── index.html # Web UI markup
        ├── style.css  # Dark gradient theme
        └── app.js     # Job polling, library, copy/delete actions
```

## Hugging Face setup (one time)

1. Create a **private Storage Bucket** (e.g. `yt-dlp-media`):
   `hf buckets create yt-dlp-media --private`
2. Create a new **Docker Space** and push this repo to it.
3. Attach the bucket as a **read/write volume** mounted at `/data` in the Space's storage settings.
4. In **Settings → Secrets**, add:
   - `APP_PASSWORD` — required, guards job creation/deletion and the library
   - `SIGNING_SECRET` — recommended, signs media URLs (falls back to `APP_PASSWORD`)
5. Optionally add a **Variable** `LINK_TTL_HOURS` (default `168` = 7 days).

Recommended Space visibility: **Protected** (code stays private, media URLs remain reachable by VLC/mpv).

## API

| Endpoint | Description |
| --- | --- |
| `POST /api/jobs` | Create a download job `{url, mode, password}` |
| `GET /api/jobs/{id}` | Poll job status; returns signed URLs when ready |
| `POST /api/jobs/{id}/delete` | Delete stored media `{password}` |
| `POST /api/library` | List all stored media `{password}` |
| `GET /media/{id}?expires=...&sig=...&download=0\|1` | Stream (range-enabled) or download |

## Local development

```bash
pip install -r requirements.txt
# ffmpeg is required for merging streams / audio extraction
export APP_PASSWORD=dev-password
export MEDIA_DIR=./data/media
uvicorn main.backend.app:app --host 0.0.0.0 --port 7860
```

Then open http://localhost:7860

## Note on YouTube

yt-dlp requires an external JavaScript runtime for YouTube's JS challenges; the Docker image installs **Deno** (yt-dlp's recommended runtime). If YouTube starts returning `HTTP Error 403` / "Sign in to confirm you're not a bot" from the datacenter IP, a PO Token provider plugin may be needed — see the [yt-dlp PO Token Guide](https://github.com/yt-dlp/yt-dlp/wiki/Po-Token-Guide).
