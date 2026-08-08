Yes. **This is a very good fit for a Hugging Face Docker Space**, especially now that Hugging Face lets you attach a Storage Bucket directly as a read/write filesystem volume. Your PRO plan currently includes **1 TB of private storage**, and Storage Buckets count under Hub storage limits. ([Hugging Face][1])

I would build it like this:

```text
                       Hugging Face Space
┌───────────────┐     ┌──────────────────────────┐
│ Browser / UI  │────▶│ FastAPI                  │
│ Paste URL     │     │                          │
└───────────────┘     │        yt-dlp            │
                      │           │              │
                      │           ▼              │
                      │    /data/media/...       │
                      └───────────┬──────────────┘
                                  │
                                  │ mounted
                                  ▼
                      ┌──────────────────────────┐
                      │ HF Storage Bucket        │
                      │ Private, persistent      │
                      │ up to your PRO allowance │
                      └──────────────────────────┘

After download:

https://your-space.hf.space/media/abc...?sig=...

                       │
             ┌─────────┼─────────┐
             ▼         ▼         ▼
          Browser     VLC       mpv
          download   stream     stream
```

The important part is that we **do not store videos only on the Space's normal disk**. Normal Space storage is ephemeral and disappears on restart. Hugging Face explicitly recommends attaching a Storage Bucket when you need persistent storage; the bucket appears inside the container as a normal filesystem directory and is read/write by default. ([Hugging Face][2])

## Why I recommend Docker instead of Gradio

You could make the UI in Gradio, but Docker gives us FastAPI underneath, which is much better for implementing:

```text
POST /api/jobs
GET  /api/jobs/{id}

GET  /media/{id}?download=1
GET  /media/{id}?download=0
```

The second media endpoint can implement **HTTP Range requests**, so VLC/mpv/browser players can seek through large videos rather than downloading the entire file first.

Docker Spaces can expose arbitrary web applications such as FastAPI, and the standard Space app port is configurable. ([Hugging Face][3])

Also, your current CPU Basic allocation is **2 vCPU / 16 GB RAM / 50 GB ephemeral disk**, with CPU Basic listed at no hourly cost. That's ample for yt-dlp downloading and FFmpeg muxing; actual video transcoding would be more CPU-intensive, so this version avoids unnecessary transcoding. ([Hugging Face][4])

---

# Build it

You only need three files:

```text
yt-dlp-space/
├── README.md
├── Dockerfile
├── requirements.txt
└── app.py
```

### `README.md`

```yaml
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

Personal yt-dlp download and streaming server.
```

---

### `requirements.txt`

```txt
fastapi
uvicorn[standard]
yt-dlp[default]
```

Using the `default` yt-dlp dependency group is intentional. Current yt-dlp requires external JavaScript challenge solving for YouTube; its documentation recommends a supported JS runtime such as Deno, and `yt-dlp[default]` includes its EJS challenge scripts. ([GitHub][5])

---

### `Dockerfile`

```dockerfile
FROM python:3.12-slim-bookworm

# ffmpeg:
#   - merges separate video/audio streams
#   - extracts MP3/M4A audio
#
# curl + unzip:
#   - required for installing Deno
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        unzip \
        ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Hugging Face Docker Spaces run as UID 1000.
RUN useradd -m -u 1000 user && \
    mkdir -p /data && \
    chmod 777 /data

USER user

ENV HOME=/home/user
ENV PATH=/home/user/.local/bin:/home/user/.deno/bin:$PATH

WORKDIR /home/user/app

# Install Deno.
# yt-dlp currently recommends Deno for YouTube's JS challenges.
RUN curl -fsSL https://deno.land/install.sh | sh

COPY --chown=user requirements.txt .

RUN pip install --user --no-cache-dir -r requirements.txt

COPY --chown=user app.py .

CMD ["python", "-m", "uvicorn", "app:app", \
     "--host", "0.0.0.0", \
     "--port", "7860"]
```

Hugging Face recommends running Docker Spaces as UID `1000`; it also specifically documents `/data` as a location that may need writable permissions. ([Hugging Face][6])

---

# `app.py`

This is the main application.

It provides:

```text
✓ URL input
✓ Best quality
✓ 1080p
✓ 720p
✓ MP3
✓ M4A
✓ Background downloading
✓ Persistent Storage Bucket
✓ Download link
✓ Stream link
✓ HTTP Range streaming
✓ VLC/mpv seeking
✓ Password protection
✓ Signed URLs
✓ Expiring media URLs
✓ Delete button
```

```python
import hashlib
import hmac
import json
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import time
import uuid

from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel


app = FastAPI(title="YT-DLP Media Server")


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

MEDIA_ROOT = Path(os.getenv("MEDIA_DIR", "/data/media"))

APP_PASSWORD = os.getenv("APP_PASSWORD", "")
SIGNING_SECRET = os.getenv("SIGNING_SECRET", APP_PASSWORD)

LINK_TTL_HOURS = int(os.getenv("LINK_TTL_HOURS", "168"))

SPACE_HOST = os.getenv("SPACE_HOST")

MEDIA_ROOT.mkdir(parents=True, exist_ok=True)


# Only run one yt-dlp process at once on CPU Basic.
download_semaphore = threading.Semaphore(1)

jobs = {}
jobs_lock = threading.Lock()


# ---------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------

class CreateJobRequest(BaseModel):
    url: str
    mode: str = "best"
    password: str


class PasswordRequest(BaseModel):
    password: str


# ---------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------

def verify_password(password: str):
    if not APP_PASSWORD:
        raise HTTPException(
            status_code=500,
            detail="APP_PASSWORD is not configured"
        )

    if not hmac.compare_digest(password, APP_PASSWORD):
        raise HTTPException(
            status_code=401,
            detail="Wrong password"
        )


def create_signature(job_id: str, expires: int) -> str:
    payload = f"{job_id}:{expires}".encode()

    return hmac.new(
        SIGNING_SECRET.encode(),
        payload,
        hashlib.sha256,
    ).hexdigest()


def verify_signature(job_id: str, expires: int, signature: str):
    if expires < int(time.time()):
        raise HTTPException(
            status_code=403,
            detail="Link expired"
        )

    expected = create_signature(job_id, expires)

    if not hmac.compare_digest(expected, signature):
        raise HTTPException(
            status_code=403,
            detail="Invalid signature"
        )


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def update_job(job_id: str, **values):
    with jobs_lock:
        if job_id not in jobs:
            jobs[job_id] = {}

        jobs[job_id].update(values)


def get_job(job_id: str):
    with jobs_lock:
        if job_id in jobs:
            return dict(jobs[job_id])

    return None


def job_directory(job_id: str) -> Path:
    return MEDIA_ROOT / job_id


def metadata_path(job_id: str) -> Path:
    return job_directory(job_id) / "_meta.json"


def load_metadata(job_id: str):
    meta_file = metadata_path(job_id)

    if not meta_file.exists():
        return None

    try:
        return json.loads(meta_file.read_text())
    except Exception:
        return None


def get_media_file(job_id: str) -> Path:
    meta = load_metadata(job_id)

    if not meta:
        raise HTTPException(
            status_code=404,
            detail="Media not found"
        )

    file_path = job_directory(job_id) / meta["filename"]

    if not file_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Media file no longer exists"
        )

    return file_path


def public_base_url(request: Request) -> str:
    # Hugging Face provides SPACE_HOST automatically.
    if SPACE_HOST:
        return f"https://{SPACE_HOST}"

    return str(request.base_url).rstrip("/")


def create_media_links(job_id: str, request: Request):
    expires = int(time.time()) + LINK_TTL_HOURS * 3600
    signature = create_signature(job_id, expires)

    base = public_base_url(request)

    common = (
        f"{base}/media/{job_id}"
        f"?expires={expires}"
        f"&sig={signature}"
    )

    return {
        "stream_url": common + "&download=0",
        "download_url": common + "&download=1",
        "expires": expires,
    }


# ---------------------------------------------------------------------
# yt-dlp
# ---------------------------------------------------------------------

def run_download(job_id: str, url: str, mode: str):
    directory = job_directory(job_id)

    try:
        with download_semaphore:
            update_job(
                job_id,
                status="downloading",
                message="Downloading media..."
            )

            directory.mkdir(
                parents=True,
                exist_ok=True
            )

            command = [
                "yt-dlp",
                "--no-playlist",
                "--no-progress",
                "-P",
                str(directory),
                "-o",
                "%(title).180B [%(id)s].%(ext)s",
            ]

            if mode == "best":
                # yt-dlp chooses the best available combination.
                pass

            elif mode == "1080":
                command += [
                    "-f",
                    (
                        "bv*[height<=1080]+ba/"
                        "b[height<=1080]"
                    ),
                ]

            elif mode == "720":
                command += [
                    "-f",
                    (
                        "bv*[height<=720]+ba/"
                        "b[height<=720]"
                    ),
                ]

            elif mode == "mp3":
                command += [
                    "-x",
                    "--audio-format",
                    "mp3",
                    "--audio-quality",
                    "0",
                ]

            elif mode == "m4a":
                command += [
                    "-x",
                    "--audio-format",
                    "m4a",
                ]

            else:
                raise ValueError(
                    f"Unsupported mode: {mode}"
                )

            command.append(url)

            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            if result.returncode != 0:
                error = result.stderr.strip()

                if len(error) > 4000:
                    error = error[-4000:]

                raise RuntimeError(error)

            # Find the actual completed output.
            candidates = []

            for path in directory.iterdir():
                if not path.is_file():
                    continue

                if path.name.startswith("_"):
                    continue

                if path.suffix in {
                    ".part",
                    ".ytdl",
                    ".json",
                }:
                    continue

                candidates.append(path)

            if not candidates:
                raise RuntimeError(
                    "yt-dlp completed but no media file was found"
                )

            # Normally there is only one final media file.
            # Pick the largest if yt-dlp left more than one.
            media_file = max(
                candidates,
                key=lambda p: p.stat().st_size
            )

            metadata = {
                "job_id": job_id,
                "filename": media_file.name,
                "size": media_file.stat().st_size,
                "source_url": url,
                "mode": mode,
                "created": int(time.time()),
            }

            metadata_path(job_id).write_text(
                json.dumps(
                    metadata,
                    indent=2,
                    ensure_ascii=False,
                )
            )

            update_job(
                job_id,
                status="ready",
                message="Ready",
                filename=media_file.name,
                size=media_file.stat().st_size,
            )

    except Exception as exc:
        update_job(
            job_id,
            status="error",
            message=str(exc),
        )


# ---------------------------------------------------------------------
# API
# ---------------------------------------------------------------------

@app.post("/api/jobs")
def create_job(body: CreateJobRequest):
    verify_password(body.password)

    parsed = urlparse(body.url)

    if parsed.scheme not in ("http", "https"):
        raise HTTPException(
            status_code=400,
            detail="Enter a valid http:// or https:// URL"
        )

    if body.mode not in {
        "best",
        "1080",
        "720",
        "mp3",
        "m4a",
    }:
        raise HTTPException(
            status_code=400,
            detail="Invalid download mode"
        )

    job_id = uuid.uuid4().hex

    update_job(
        job_id,
        status="queued",
        message="Queued",
        source_url=body.url,
        mode=body.mode,
        created=int(time.time()),
    )

    thread = threading.Thread(
        target=run_download,
        args=(
            job_id,
            body.url,
            body.mode,
        ),
        daemon=True,
    )

    thread.start()

    return {
        "job_id": job_id,
        "status": "queued",
    }


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str, request: Request):
    job = get_job(job_id)

    # If Space restarted, recover finished jobs from the bucket.
    if job is None:
        meta = load_metadata(job_id)

        if meta:
            links = create_media_links(
                job_id,
                request,
            )

            return {
                "job_id": job_id,
                "status": "ready",
                "filename": meta["filename"],
                "size": meta["size"],
                **links,
            }

        raise HTTPException(
            status_code=404,
            detail="Job not found"
        )

    result = {
        "job_id": job_id,
        **job,
    }

    if job.get("status") == "ready":
        result.update(
            create_media_links(
                job_id,
                request,
            )
        )

    return result


@app.post("/api/jobs/{job_id}/delete")
def delete_job(
    job_id: str,
    body: PasswordRequest,
):
    verify_password(body.password)

    directory = job_directory(job_id)

    if directory.exists():
        shutil.rmtree(directory)

    with jobs_lock:
        jobs.pop(job_id, None)

    return {
        "ok": True
    }


# ---------------------------------------------------------------------
# Range-enabled streaming
# ---------------------------------------------------------------------

def file_iterator(
    path: Path,
    start: int,
    end: int,
    chunk_size: int = 1024 * 1024,
):
    with path.open("rb") as file:
        file.seek(start)

        remaining = end - start + 1

        while remaining > 0:
            chunk = file.read(
                min(chunk_size, remaining)
            )

            if not chunk:
                break

            remaining -= len(chunk)

            yield chunk


@app.api_route(
    "/media/{job_id}",
    methods=["GET", "HEAD"],
    name="serve_media",
)
def serve_media(
    job_id: str,
    request: Request,
    expires: int,
    sig: str,
    download: int = 0,
):
    verify_signature(
        job_id,
        expires,
        sig,
    )

    path = get_media_file(job_id)

    file_size = path.stat().st_size

    mime_type, _ = mimetypes.guess_type(
        path.name
    )

    mime_type = (
        mime_type
        or "application/octet-stream"
    )

    disposition = (
        "attachment"
        if download
        else "inline"
    )

    encoded_filename = quote(path.name)

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": (
            f"{disposition}; "
            f"filename*=UTF-8''{encoded_filename}"
        ),
    }

    range_header = request.headers.get(
        "range"
    )

    if not range_header:
        headers["Content-Length"] = str(
            file_size
        )

        if request.method == "HEAD":
            return Response(
                status_code=200,
                media_type=mime_type,
                headers=headers,
            )

        return StreamingResponse(
            file_iterator(
                path,
                0,
                file_size - 1,
            ),
            status_code=200,
            media_type=mime_type,
            headers=headers,
        )

    match = re.match(
        r"bytes=(\d*)-(\d*)$",
        range_header,
    )

    if not match:
        return Response(
            status_code=416,
            headers={
                "Content-Range":
                    f"bytes */{file_size}"
            },
        )

    start_string = match.group(1)
    end_string = match.group(2)

    if start_string == "":
        # Example:
        # Range: bytes=-500
        length = int(end_string)

        start = max(
            file_size - length,
            0,
        )

        end = file_size - 1

    else:
        start = int(start_string)

        if end_string:
            end = int(end_string)
        else:
            end = file_size - 1

    if (
        start >= file_size
        or start > end
    ):
        return Response(
            status_code=416,
            headers={
                "Content-Range":
                    f"bytes */{file_size}"
            },
        )

    end = min(
        end,
        file_size - 1,
    )

    content_length = (
        end - start + 1
    )

    headers.update({
        "Content-Range":
            f"bytes {start}-{end}/{file_size}",
        "Content-Length":
            str(content_length),
    })

    if request.method == "HEAD":
        return Response(
            status_code=206,
            media_type=mime_type,
            headers=headers,
        )

    return StreamingResponse(
        file_iterator(
            path,
            start,
            end,
        ),
        status_code=206,
        media_type=mime_type,
        headers=headers,
    )


# ---------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------

HTML = r"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">

    <meta
        name="viewport"
        content="width=device-width, initial-scale=1"
    >

    <title>YT-DLP Media Server</title>

    <style>
        body {
            font-family:
                system-ui,
                -apple-system,
                sans-serif;

            max-width: 850px;
            margin: 50px auto;
            padding: 0 20px;

            background: #111;
            color: #eee;
        }

        .card {
            background: #1c1c1c;
            padding: 24px;
            border-radius: 14px;
            margin-bottom: 20px;
        }

        input,
        select,
        button {
            box-sizing: border-box;
            width: 100%;
            padding: 12px;
            margin-top: 8px;
            margin-bottom: 15px;

            border-radius: 8px;
            border: 1px solid #444;

            background: #252525;
            color: white;
        }

        button {
            cursor: pointer;
            font-weight: 600;
        }

        button:hover {
            background: #333;
        }

        a {
            color: #8ab4f8;
        }

        .linkbox {
            display: flex;
            gap: 8px;
        }

        .linkbox input {
            flex: 1;
        }

        .linkbox button {
            width: 100px;
        }

        #result {
            display: none;
        }

        #error {
            color: #ff8a80;
            white-space: pre-wrap;
        }

        video,
        audio {
            width: 100%;
            margin-top: 15px;
        }

        small {
            color: #aaa;
        }
    </style>
</head>

<body>

<h1>📥 YT-DLP Media Server</h1>

<div class="card">

    <label>App password</label>

    <input
        id="password"
        type="password"
        placeholder="Your Space password"
    >

    <label>Video / media URL</label>

    <input
        id="url"
        type="url"
        placeholder="https://www.youtube.com/watch?v=..."
    >

    <label>Format</label>

    <select id="mode">
        <option value="best">
            Best quality
        </option>

        <option value="1080">
            Maximum 1080p
        </option>

        <option value="720">
            Maximum 720p
        </option>

        <option value="mp3">
            MP3 audio
        </option>

        <option value="m4a">
            M4A audio
        </option>
    </select>

    <button onclick="createJob()">
        Generate Media Link
    </button>

    <div id="status"></div>
    <div id="error"></div>

</div>


<div
    id="result"
    class="card"
>

    <h2>Ready</h2>

    <div id="filename"></div>
    <div id="filesize"></div>

    <h3>Stream URL</h3>

    <div class="linkbox">
        <input
            id="streamUrl"
            readonly
        >

        <button
            onclick="copyField('streamUrl')"
        >
            Copy
        </button>
    </div>

    <small>
        Use with VLC, mpv, browser player, etc.
    </small>


    <h3>Download URL</h3>

    <div class="linkbox">
        <input
            id="downloadUrl"
            readonly
        >

        <button
            onclick="copyField('downloadUrl')"
        >
            Copy
        </button>
    </div>


    <video
        id="videoPlayer"
        controls
        style="display:none"
    ></video>

    <audio
        id="audioPlayer"
        controls
        style="display:none"
    ></audio>


    <button onclick="deleteMedia()">
        Delete stored file
    </button>

</div>


<script>

let currentJob = null;


function formatBytes(bytes) {

    if (!bytes) {
        return "";
    }

    const units = [
        "B",
        "KB",
        "MB",
        "GB",
        "TB"
    ];

    let i = 0;

    while (
        bytes >= 1024
        && i < units.length - 1
    ) {
        bytes /= 1024;
        i++;
    }

    return (
        bytes.toFixed(2)
        + " "
        + units[i]
    );
}


async function createJob() {

    const password =
        document.getElementById(
            "password"
        ).value;

    const url =
        document.getElementById(
            "url"
        ).value;

    const mode =
        document.getElementById(
            "mode"
        ).value;

    document.getElementById(
        "error"
    ).textContent = "";

    document.getElementById(
        "result"
    ).style.display = "none";

    document.getElementById(
        "status"
    ).textContent =
        "Creating job...";

    try {

        const response = await fetch(
            "/api/jobs",
            {
                method: "POST",

                headers: {
                    "Content-Type":
                        "application/json"
                },

                body: JSON.stringify({
                    password,
                    url,
                    mode
                })
            }
        );

        const data =
            await response.json();

        if (!response.ok) {
            throw new Error(
                data.detail
                || "Request failed"
            );
        }

        currentJob = data.job_id;

        pollJob();

    } catch (error) {

        document.getElementById(
            "status"
        ).textContent = "";

        document.getElementById(
            "error"
        ).textContent =
            error.message;
    }
}


async function pollJob() {

    if (!currentJob) {
        return;
    }

    try {

        const response = await fetch(
            "/api/jobs/" + currentJob
        );

        const data =
            await response.json();

        if (!response.ok) {
            throw new Error(
                data.detail
                || "Status request failed"
            );
        }

        document.getElementById(
            "status"
        ).textContent =
            data.message
            || data.status;

        if (
            data.status === "queued"
            || data.status === "downloading"
        ) {

            setTimeout(
                pollJob,
                2000
            );

            return;
        }

        if (
            data.status === "error"
        ) {

            throw new Error(
                data.message
            );
        }

        if (
            data.status === "ready"
        ) {

            showResult(data);
        }

    } catch (error) {

        document.getElementById(
            "error"
        ).textContent =
            error.message;
    }
}


function showResult(data) {

    document.getElementById(
        "result"
    ).style.display = "block";

    document.getElementById(
        "filename"
    ).textContent =
        data.filename;

    document.getElementById(
        "filesize"
    ).textContent =
        formatBytes(data.size);

    document.getElementById(
        "streamUrl"
    ).value =
        data.stream_url;

    document.getElementById(
        "downloadUrl"
    ).value =
        data.download_url;

    const filename =
        data.filename.toLowerCase();

    const video =
        document.getElementById(
            "videoPlayer"
        );

    const audio =
        document.getElementById(
            "audioPlayer"
        );

    video.style.display = "none";
    audio.style.display = "none";

    if (
        filename.endsWith(".mp4")
        || filename.endsWith(".webm")
        || filename.endsWith(".mkv")
    ) {

        video.src =
            data.stream_url;

        video.style.display =
            "block";

    } else if (
        filename.endsWith(".mp3")
        || filename.endsWith(".m4a")
        || filename.endsWith(".opus")
        || filename.endsWith(".ogg")
    ) {

        audio.src =
            data.stream_url;

        audio.style.display =
            "block";
    }
}


async function deleteMedia() {

    if (!currentJob) {
        return;
    }

    const password =
        document.getElementById(
            "password"
        ).value;

    const response = await fetch(
        "/api/jobs/"
        + currentJob
        + "/delete",
        {
            method: "POST",

            headers: {
                "Content-Type":
                    "application/json"
            },

            body: JSON.stringify({
                password
            })
        }
    );

    if (response.ok) {

        document.getElementById(
            "result"
        ).style.display =
            "none";

        document.getElementById(
            "status"
        ).textContent =
            "Deleted.";

        currentJob = null;
    }
}


function copyField(id) {

    const field =
        document.getElementById(id);

    navigator.clipboard.writeText(
        field.value
    );
}

</script>

</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def home():
    return HTML
```

---

# Configure Hugging Face

Do the following once:

1. **Create a private Storage Bucket**, for example `yt-dlp-media`. Hugging Face lets you create one from the Hub UI or with `hf buckets create my-bucket --private`. Private buckets are appropriate here because downloaded personal media should not simply become a public Hub dataset. ([Hugging Face][7])

2. Create a new Space and choose **Docker**. Your PRO subscription allows Gradio and Docker Spaces on compute. ([Hugging Face][1])

3. I recommend setting the Space visibility to **Protected**. With Protected visibility your source code remains private while the actual `.hf.space` application remains reachable, which is exactly what we need for VLC/download URLs. A fully Private Space restricts access to owner/collaborators and is therefore less convenient for standalone media clients. ([Hugging Face][4])

4. In the Space's storage/volume settings, attach `yt-dlp-media` as a **read/write volume** mounted at:

```text
/data
```

HF officially supports mounting Storage Buckets into Spaces at a chosen filesystem path. ([Hugging Face][2])

5. In **Settings → Secrets**, add:

```text
APP_PASSWORD
```

For example:

```text
some-long-random-password
```

And:

```text
SIGNING_SECRET
```

Use another long random value, for example:

```text
8952f1f7a3f9baf64f7811f95a...
```

Hugging Face recommends keeping credentials in Space Secrets rather than hard-coding them; Docker Spaces receive runtime secrets through environment variables. ([Hugging Face][4])

Optionally add this as a **Variable**:

```text
LINK_TTL_HOURS=168
```

`168` = 7 days.

You could use:

```text
24
```

for one day, or:

```text
720
```

for 30 days.

---

# What happens when you use it

Suppose you enter:

```text
https://www.youtube.com/watch?v=abc123
```

and select:

```text
Maximum 1080p
```

The Space runs something conceptually equivalent to:

```bash
yt-dlp \
  --no-playlist \
  -f "bv*[height<=1080]+ba/b[height<=1080]" \
  "https://www.youtube.com/watch?v=abc123"
```

yt-dlp will select separate video/audio when appropriate and can use FFmpeg to merge them. Its documented default format-selection behavior similarly prefers the best video/audio combination. ([GitHub][8])

The file ends up at something like:

```text
/data/media/
└── 8a6174378dbe4bc3a8d54e51f784245e/
    ├── Some Video [abc123].mp4
    └── _meta.json
```

Because `/data` is your mounted Bucket, the media survives a Space restart. ([Hugging Face][2])

The app then returns something like:

```text
Stream:

https://kaushikpaul-yt-dlp.hf.space/media/8a6174378dbe4bc3a8d54e51f784245e?expires=1787000000&sig=28a1...
```

and:

```text
Download:

https://kaushikpaul-yt-dlp.hf.space/media/8a6174378dbe4bc3a8d54e51f784245e?expires=1787000000&sig=28a1...&download=1
```

The signature means someone cannot just guess:

```text
/media/123
/media/124
/media/125
```

and access your files.

---

# Using the generated URL

Browser:

```text
https://.../media/abc?...&download=1
```

will download it.

VLC:

```text
Media
→ Open Network Stream
→ paste Stream URL
```

Or:

```bash
vlc "https://.../media/abc?...&download=0"
```

mpv:

```bash
mpv "https://.../media/abc?...&download=0"
```

curl:

```bash
curl -L \
  "https://.../media/abc?...&download=1" \
  -o video.mp4
```

Because our FastAPI endpoint handles:

```http
Range: bytes=5000000-
```

a player can request only portions of the file:

```text
VLC
 │
 │ bytes=0-1048575
 ▼
HF Space
 │
 ▼
Storage Bucket

VLC seeks to 35:10
 │
 │ bytes=983428192-
 ▼
HF Space
```

so **seeking doesn't require re-downloading the entire file**.

---

# What happens when the Space sleeps?

There are two separate pieces:

```text
Space compute
    ↓
can sleep

Storage Bucket
    ↓
persists
```

Hugging Face says free Space hardware sleeps after a period of inactivity, while attached Bucket data remains persistent across Space restarts. ([Hugging Face][4])

So you don't lose:

```text
video1.mp4
video2.mp4
audio.mp3
...
```

when the Space restarts.

Your signed URLs also continue to work after restart **as long as `SIGNING_SECRET` remains the same and the link hasn't expired**; that's a property of the signing scheme above rather than a Hugging Face feature.

---

# One current yt-dlp/YouTube complication

As of **August 2026**, YouTube has become considerably more aggressive about playback validation.

yt-dlp currently documents two mechanisms relevant here:

```text
JavaScript challenges
        +
PO Tokens on some YouTube clients
```

yt-dlp now requires an external JavaScript runtime for YouTube challenges; Deno is its recommended runtime, which is why the Dockerfile installs it. ([GitHub][5])

YouTube is also progressively enforcing **PO Tokens** for some playback clients. yt-dlp's current recommendation, when necessary, is to use a PO Token Provider plugin rather than manually generating tokens. ([GitHub][9])

So I would first deploy the version above and test normal public YouTube videos. If Hugging Face's datacenter IP starts producing errors such as:

```text
HTTP Error 403
```

or:

```text
Sign in to confirm you're not a bot
```

then we add a PO-token provider to this Docker image. That's a separate layer and isn't necessary for every URL.

---

## One thing I would improve next

The implementation above **downloads the media into your 1 TB bucket first and serves it through your Space**.

For your exact use case, I'd eventually make the UI more like:

```text
┌─────────────────────────────────────────┐
│ YT-DLP Media Server                     │
├─────────────────────────────────────────┤
│ URL                                     │
│ [ https://youtube.com/...             ] │
│                                         │
│ Quality                                 │
│ [ Best ▼ ]                              │
│                                         │
│ [ Generate ]                            │
├─────────────────────────────────────────┤
│ Some Movie                              │
│ 1080p • MP4 • 1.82 GB                   │
│                                         │
│ [▶ Play] [↓ Download] [Copy URL]        │
│                                         │
│ ███████████████████░░░  83%             │
└─────────────────────────────────────────┘
```

with a **media-library page showing everything already stored in your HF bucket**, so it starts behaving more like your own personal Drive/media server.

Hugging Face Storage Buckets are particularly suitable for this because they are mutable object storage rather than Git repos, include a CDN, and HF currently says egress/CDN are included subject to its storage bandwidth policy. ([Hugging Face][10])

So yes: **HF PRO + Docker Space + private Storage Bucket + yt-dlp + FastAPI is a practical architecture for exactly what you're trying to build.**

[1]: https://huggingface.co/pro "PRO Account - Hugging Face"
[2]: https://huggingface.co/docs/hub/main/spaces-storage "Disk usage on Spaces · Hugging Face"
[3]: https://huggingface.co/docs/hub/spaces-sdks-docker?utm_source=chatgpt.com "Docker Spaces · Hugging Face"
[4]: https://huggingface.co/docs/hub/main/spaces-overview "Spaces Overview · Hugging Face"
[5]: https://github.com/yt-dlp/yt-dlp/wiki/EJS "EJS · yt-dlp/yt-dlp Wiki · GitHub"
[6]: https://huggingface.co/docs/hub/spaces-sdks-docker "Docker Spaces · Hugging Face"
[7]: https://huggingface.co/docs/hub/storage-buckets "Storage Buckets · Hugging Face"
[8]: https://github.com/yt-dlp/yt-dlp/blob/master/README.md?utm_source=chatgpt.com "yt-dlp/README.md at master · yt-dlp/yt-dlp · GitHub"
[9]: https://github.com/yt-dlp/yt-dlp/wiki/Po-Token-Guide "PO Token Guide · yt-dlp/yt-dlp Wiki · GitHub"
[10]: https://huggingface.co/docs/hub/storage-buckets?utm_source=chatgpt.com "Storage Buckets · Hugging Face"
