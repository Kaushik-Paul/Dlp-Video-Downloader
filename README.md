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

A private, self-hosted media downloader and streaming library for a Hugging Face Docker Space. Paste any URL supported by [yt-dlp](https://github.com/yt-dlp/yt-dlp), choose a format, and receive signed stream and download links.

The server stores completed media in a mounted Hugging Face Storage Bucket. Files therefore survive Space restarts, while the Space itself can continue using the free CPU Basic runtime.

## What it provides

- Best quality, 1080p, 720p, MP3, and M4A download modes
- A background job queue with one active download at a time
- A persistent media library recovered after Space restarts
- Password-protected job creation, library browsing, and deletion
- HMAC-signed media links with configurable expiry
- HTTP byte-range support for seeking in browsers, VLC, and mpv
- A self-contained HTML/CSS/JavaScript interface with no CDN dependencies

## Architecture

```text
Browser
  |  POST /api/jobs, GET /api/jobs/{id}
  v
FastAPI + yt-dlp + FFmpeg  ---- writes ---->  /data/media/{job_id}
  |                                             |
  |  signed GET/HEAD /media/{id}                v
  +------------------------------------>  HF Storage Bucket
                Range responses
```

The Space filesystem outside `/data` is ephemeral. The private bucket is mounted read/write at `/data`, and each completed item stores its media file plus `_meta.json` under `/data/media/{job_id}`.

## Repository layout

```text
├── README.md
├── Dockerfile
├── requirements.txt
├── main/
│   ├── backend/app.py
│   ├── frontend/
│   │   ├── index.html
│   │   ├── style.css
│   │   └── app.js
│   └── scripts/deploy_space.py
└── PLAN.md
```

## Run locally

Requirements: Python 3.12+, FFmpeg, and the packages in `requirements.txt`.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export APP_PASSWORD='choose-a-password'
export SIGNING_SECRET='use-a-different-long-random-secret'
export MEDIA_DIR='./data/media'

uvicorn main.backend.app:app --host 0.0.0.0 --port 7860
```

Open <http://localhost:7860>. The configured password is entered in the web UI; it is not stored by the server or written into downloaded metadata.

## First Hugging Face deployment

This project is a **Docker Space**, not a Gradio SDK application. Hugging Face Spaces is the hosting product; Docker is the SDK selected in the README front matter.

Install the current `hf` CLI and authenticate:

```bash
curl -LsSf https://hf.co/cli/install.sh | bash
hf auth login
```

Create a private bucket and a protected Docker Space. Protected visibility keeps the source repository private while leaving the running app reachable by signed URLs used in VLC/mpv.

```bash
hf buckets create kaushikpaul/yt-dlp-media --private --exist-ok

hf repos create kaushikpaul/Dlp-Video-Downloader \
  --type space \
  --sdk docker \
  --protected \
  --flavor cpu-basic \
  --volume hf://buckets/kaushikpaul/yt-dlp-media:/data \
  --exist-ok
```

Configure secrets and the link lifetime. Do not commit these values to this repository.

```bash
hf spaces secrets add kaushikpaul/Dlp-Video-Downloader \
  -s APP_PASSWORD='choose-a-strong-password' \
  -s SIGNING_SECRET='use-a-different-long-random-secret'

hf spaces variables add kaushikpaul/Dlp-Video-Downloader \
  -e LINK_TTL_HOURS=168
```

Deploy the current working tree:

```bash
python3 main/scripts/deploy_space.py
hf spaces wait kaushikpaul/Dlp-Video-Downloader --timeout 15m
```

The deployment script uploads an explicit allowlist of runtime files directly through the authenticated CLI. It does not require a Git commit or push and excludes `.env` files, caches, IDE settings, `PLAN.md`, and `AGENTS.md`.

For a different account or Space name:

```bash
python3 main/scripts/deploy_space.py --repo-id USER/SPACE_NAME
```

Use `--dry-run` to inspect the exact upload set. Use `--create` to create a protected Docker Space when a bucket mount is not needed or will be configured separately.

### Existing Space without the bucket mount

Attach the bucket with:

```bash
hf spaces volumes set kaushikpaul/Dlp-Video-Downloader \
  --volume hf://buckets/kaushikpaul/yt-dlp-media:/data
```

`hf spaces volumes set` replaces the complete volume list, so include every desired volume in that command.

## Configuration

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `APP_PASSWORD` | Yes | empty | Authorizes job creation, library access, and deletion. An empty value intentionally makes protected operations fail. |
| `SIGNING_SECRET` | Recommended | `APP_PASSWORD` | HMAC key for media URLs. Keep it stable or existing links stop working. |
| `LINK_TTL_HOURS` | No | `168` | Lifetime of newly generated stream/download links. |
| `MEDIA_DIR` | No | `/data/media` | Media root. Keep this under the mounted `/data` directory on Spaces. |
| `SPACE_HOST` | Automatic on HF | request host | Used to create absolute public media URLs. |

## API

| Endpoint | Purpose |
| --- | --- |
| `POST /api/jobs` | Create a job with `{url, mode, password}`. |
| `GET /api/jobs/{id}` | Poll queued/downloading/ready/error status. |
| `POST /api/library` | List persisted media with `{password}`. |
| `POST /api/jobs/{id}/delete` | Delete a persisted item with `{password}`. |
| `GET or HEAD /media/{id}?expires=...&sig=...&download=0|1` | Stream inline or download using a signed URL. |

Media URLs are bearer links: anyone holding an unexpired URL can read that item. Keep the Space protected, use a strong signing secret, and shorten `LINK_TTL_HOURS` if links may be shared accidentally.

## Validation

After backend changes:

```bash
python3 -m py_compile main/backend/app.py
python3 main/scripts/deploy_space.py --dry-run
```

After deployment, check the root page and range behavior:

```bash
curl -I https://kaushikpaul-dlp-video-downloader.hf.space/
curl -H 'Range: bytes=0-1023' -I 'SIGNED_STREAM_URL'
```

The second request should return `206 Partial Content`, `Accept-Ranges: bytes`, a `Content-Range` header, and `Content-Length: 1024`.

## Operational notes

- Downloads are intentionally serialized with a semaphore to avoid overloading CPU Basic hardware.
- Restarting the Space interrupts active jobs. Completed jobs remain in the bucket and are recovered from `_meta.json`.
- Deleting an item permanently removes it from the non-versioned bucket.
- Keep `SIGNING_SECRET` unchanged across deployments so existing links remain valid until expiry.
- Only download media you are authorized to access and follow the source site's terms and applicable law.

## Troubleshooting

**`APP_PASSWORD is not configured`**

Add the Space secret and restart the Space.

**Downloaded files disappear after a restart**

Confirm `hf spaces volumes ls kaushikpaul/Dlp-Video-Downloader` shows the private bucket mounted read/write at `/data`.

**YouTube returns `403` or asks to confirm you are not a bot**

The image already includes Deno and `yt-dlp[default]` for JavaScript challenges. Datacenter IPs may still require a yt-dlp PO Token provider; see the official [yt-dlp PO Token guide](https://github.com/yt-dlp/yt-dlp/wiki/Po-Token-Guide).

**A format downloads but will not preview in the browser**

VLC or mpv may support containers/codecs that the browser does not. Use the signed stream URL in one of those players, or choose another yt-dlp format.
