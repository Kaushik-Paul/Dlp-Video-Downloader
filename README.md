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

A private, self-hosted media link generator, downloader, and streaming library for a Hugging Face Docker Space. Paste any URL supported by [yt-dlp](https://github.com/yt-dlp/yt-dlp), then either extract an instant provider link without storing media or download it for a reliable 30-day signed link.

The server stores completed media in a mounted Hugging Face Storage Bucket. Files therefore survive Space restarts, while the Space itself can continue using the free CPU Basic runtime.

## What it provides

- Best quality, 1080p, 720p, source-audio, MP3, and M4A modes
- Instant source links that do not download or use bucket storage
- A background job queue with one active download at a time
- A persistent media library recovered after Space restarts
- Password-protected job creation, library browsing, and deletion
- HMAC-signed media links with configurable expiry
- Automatic deletion of bucket media 30 days after download
- Browser-compatible TLS requests and automatic YouTube PO-token generation
- HTTP byte-range support for seeking in browsers, VLC, and mpv
- A self-contained HTML/CSS/JavaScript interface with no CDN dependencies

## Architecture

```text
Browser
  |  POST /api/jobs {delivery: instant|stored}, GET /api/jobs/{id}
  v
FastAPI + yt-dlp + FFmpeg  ---- stored mode ---->  /data/media/{job_id}
  |
  +---- instant mode ----> Provider CDN URL (not stored)
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
export LINK_TTL_HOURS=720
export MEDIA_RETENTION_DAYS=30

uvicorn main.backend.app:app --host 0.0.0.0 --port 7860
```

Open <http://localhost:7860>. The configured password is entered in the web UI; it is not stored by the server or written into downloaded metadata.

## Instant links vs 30-day storage

**Instant source link** asks yt-dlp for the provider's best single-file CDN URL containing both video and audio. It does not download, proxy, or store the media. It is fast and uses no bucket capacity, but the provider controls its lifetime and may offer a lower resolution than its separate video/audio streams. The link may expire within minutes or hours or require provider-specific headers or cookies. MP3/M4A conversion is unavailable because conversion requires downloading the media.

**Store for 30 days** downloads and, when needed, merges or converts the media. The resulting signed URL supports browser/VLC/mpv seeking and remains renewable until the fixed 30-day retention deadline.

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
  -e LINK_TTL_HOURS=720 \
  -e MEDIA_RETENTION_DAYS=30
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
| `LINK_TTL_HOURS` | No | `720` | Maximum lifetime of newly generated stream/download links. Links never outlive their stored media. |
| `MEDIA_RETENTION_DAYS` | No | `30` | Permanently delete completed media this many days after download. |
| `MEDIA_DIR` | No | `/data/media` | Media root. Keep this under the mounted `/data` directory on Spaces. |
| `SPACE_HOST` | Automatic on HF | request host | Used to create absolute public media URLs. |
| `YTDLP_PROXY` | No | empty | Authenticated HTTP/SOCKS proxy URL used by yt-dlp. Configure as a Space Secret, not a variable. |
| `YOUTUBE_COOKIES_B64` | No | empty | Base64-encoded Netscape YouTube cookie file. Decoded to ephemeral `/tmp` with mode `0600`. |

## API

| Endpoint | Purpose |
| --- | --- |
| `POST /api/jobs` | Create a job with `{url, mode, delivery, password}`; `delivery` is `instant` or `stored`. |
| `GET /api/jobs/{id}` | Poll queued/downloading/ready/error status. |
| `POST /api/library` | List persisted media with `{password}`. |
| `POST /api/jobs/{id}/delete` | Delete a persisted item with `{password}`. |
| `GET or HEAD /media/{id}?expires=...&sig=...&download=0|1` | Stream inline or download using a signed URL. |

Media URLs are bearer links: anyone holding an unexpired URL can read that item. Keep the Space protected, use a strong signing secret, and shorten `LINK_TTL_HOURS` if links may be shared accidentally.

Instant results return provider bearer URLs directly. They are held only in memory until the Space restarts, are never added to the library, and are not governed by `LINK_TTL_HOURS` or `MEDIA_RETENTION_DAYS`.

Every completed file has a fixed 30-day retention deadline. New links are capped at that deadline, expired media returns HTTP `410`, and cleanup runs when the app starts and every six hours while it is awake. If the Space is asleep at the deadline, physical bucket deletion happens when the Space next wakes, but the signed link itself is already expired.

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
- Restarting the Space interrupts active jobs. Completed jobs remain in the bucket for 30 days and are recovered from `_meta.json`.
- Expired media is permanently deleted on startup and every six hours while the Space is running.
- Deleting an item permanently removes it from the non-versioned bucket.
- Keep `SIGNING_SECRET` unchanged across deployments so existing links remain valid until expiry.
- Only download media you are authorized to access and follow the source site's terms and applicable law.

## Troubleshooting

**`APP_PASSWORD is not configured`**

Add the Space secret and restart the Space.

**Downloaded files disappear after a restart**

Confirm `hf spaces volumes ls kaushikpaul/Dlp-Video-Downloader` shows the private bucket mounted read/write at `/data`.

**YouTube returns `403` or asks to confirm you are not a bot**

The image includes Deno, yt-dlp's browser TLS transport, and the BgUtils PO-token provider configured for the recommended `mweb` client. This avoids storing YouTube account cookies. YouTube can still block heavily shared datacenter IPs, and the provider itself notes that a token cannot guarantee bypassing every bot check. See the official [yt-dlp PO Token guide](https://github.com/yt-dlp/yt-dlp/wiki/Po-Token-Guide).

The safest workaround is a reputable proxy whose address is not blocked by YouTube:

```bash
hf spaces secrets add kaushikpaul/Dlp-Video-Downloader \
  -s YTDLP_PROXY='http://USER:PASSWORD@HOST:PORT'
```

If a proxy is unavailable, export a fresh Netscape-format cookie file from a separate/throwaway YouTube account and configure it without committing or uploading the file:

```bash
hf spaces secrets add kaushikpaul/Dlp-Video-Downloader \
  -s YOUTUBE_COOKIES_B64="$(base64 -w0 youtube-cookies.txt)"
```

Cookie-based extraction carries a risk of YouTube temporarily or permanently banning the account. The official [yt-dlp YouTube extractor guide](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies) recommends an isolated session and warns against using a primary account.

**YouTube fails with `SSL: UNEXPECTED_EOF_WHILE_READING`**

The server uses yt-dlp's supported `curl-cffi` browser transport and forces IPv4 because some Space network routes terminate Python/OpenSSL connections to YouTube unexpectedly. Disabling certificate verification is intentionally avoided: the failure occurs during transport and is not caused by an untrusted certificate.

**A format downloads but will not preview in the browser**

VLC or mpv may support containers/codecs that the browser does not. Use the signed stream URL in one of those players, or choose another yt-dlp format.

---

## 📄 License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
