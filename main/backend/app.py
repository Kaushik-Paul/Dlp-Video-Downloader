import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


logger = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def app_lifespan(_app: FastAPI):
    cleanup_stop = threading.Event()
    cleanup_thread = threading.Thread(
        target=cleanup_loop,
        args=(cleanup_stop,),
        daemon=True,
        name="media-retention-cleanup",
    )
    cleanup_thread.start()
    try:
        yield
    finally:
        cleanup_stop.set()
        cleanup_thread.join(timeout=2)


app = FastAPI(title="YT-DLP Media Server", lifespan=app_lifespan)


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------
MEDIA_ROOT = Path(os.getenv("MEDIA_DIR", "/data/media"))
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
SIGNING_SECRET = os.getenv("SIGNING_SECRET", APP_PASSWORD)
LINK_TTL_HOURS = int(os.getenv("LINK_TTL_HOURS", "720"))
MEDIA_RETENTION_DAYS = int(os.getenv("MEDIA_RETENTION_DAYS", "30"))
SPACE_HOST = os.getenv("SPACE_HOST")

if LINK_TTL_HOURS <= 0:
    raise RuntimeError("LINK_TTL_HOURS must be greater than zero")
if MEDIA_RETENTION_DAYS <= 0:
    raise RuntimeError("MEDIA_RETENTION_DAYS must be greater than zero")

MEDIA_ROOT.mkdir(parents=True, exist_ok=True)

VALID_MODES = {"best", "1080", "720", "mp3", "m4a"}
JOB_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
CLEANUP_INTERVAL_SECONDS = 6 * 3600

# Only run one yt-dlp process at a time on CPU Basic hardware.
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
        raise HTTPException(status_code=500, detail="APP_PASSWORD is not configured")
    if not hmac.compare_digest(password, APP_PASSWORD):
        raise HTTPException(status_code=401, detail="Wrong password")


def create_signature(job_id: str, expires: int) -> str:
    payload = f"{job_id}:{expires}".encode()
    return hmac.new(SIGNING_SECRET.encode(), payload, hashlib.sha256).hexdigest()


def verify_signature(job_id: str, expires: int, signature: str):
    if expires < int(time.time()):
        raise HTTPException(status_code=403, detail="Link expired")
    expected = create_signature(job_id, expires)
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=403, detail="Invalid signature")


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def validate_job_id(job_id: str):
    if not JOB_ID_PATTERN.fullmatch(job_id):
        raise HTTPException(status_code=404, detail="Job not found")


def update_job(job_id: str, **values):
    with jobs_lock:
        jobs.setdefault(job_id, {}).update(values)


def get_job(job_id: str):
    with jobs_lock:
        if job_id in jobs:
            return dict(jobs[job_id])
    return None


def job_directory(job_id: str) -> Path:
    validate_job_id(job_id)
    return MEDIA_ROOT / job_id


def metadata_path(job_id: str) -> Path:
    return job_directory(job_id) / "_meta.json"


def load_metadata(job_id: str):
    meta_file = metadata_path(job_id)
    if not meta_file.exists():
        return None
    try:
        metadata = json.loads(meta_file.read_text(encoding="utf-8"))
        filename = metadata["filename"]
        if (
            not isinstance(filename, str)
            or not filename
            or Path(filename).name != filename
            or metadata.get("job_id") != job_id
        ):
            return None
        return metadata
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def media_expiration(job_id: str, metadata: dict) -> int:
    configured_expiration = metadata.get("retention_expires")
    if isinstance(configured_expiration, int) and configured_expiration > 0:
        return configured_expiration

    created = metadata.get("created")
    if not isinstance(created, int) or created <= 0:
        try:
            created = int(metadata_path(job_id).stat().st_mtime)
        except OSError:
            created = int(time.time())
    return created + MEDIA_RETENTION_DAYS * 86400


def is_media_expired(job_id: str, metadata: dict, now: int | None = None) -> bool:
    current_time = int(time.time()) if now is None else now
    return media_expiration(job_id, metadata) <= current_time


def remove_stored_job(job_id: str):
    directory = job_directory(job_id)
    if directory.exists():
        shutil.rmtree(directory)
    with jobs_lock:
        jobs.pop(job_id, None)


def cleanup_expired_media(now: int | None = None) -> int:
    current_time = int(time.time()) if now is None else now
    removed = 0
    if not MEDIA_ROOT.exists():
        return removed

    for child in MEDIA_ROOT.iterdir():
        if not child.is_dir() or not JOB_ID_PATTERN.fullmatch(child.name):
            continue
        metadata = load_metadata(child.name)
        if metadata:
            expired = is_media_expired(child.name, metadata, current_time)
        else:
            try:
                expired = child.stat().st_mtime <= current_time - MEDIA_RETENTION_DAYS * 86400
            except OSError:
                continue
        if not expired:
            continue
        try:
            remove_stored_job(child.name)
            removed += 1
        except OSError:
            logger.exception("Could not remove expired media job %s", child.name)

    if removed:
        logger.info("Removed %d expired media job(s)", removed)
    return removed


def cleanup_loop(stop_event: threading.Event):
    while not stop_event.is_set():
        try:
            cleanup_expired_media()
        except Exception:
            logger.exception("Media retention cleanup failed")
        if stop_event.wait(CLEANUP_INTERVAL_SECONDS):
            break


def active_metadata(job_id: str) -> dict:
    metadata = load_metadata(job_id)
    if not metadata:
        raise HTTPException(status_code=404, detail="Media not found")
    if is_media_expired(job_id, metadata):
        try:
            remove_stored_job(job_id)
        except OSError:
            logger.exception("Could not remove expired media job %s", job_id)
        raise HTTPException(
            status_code=410,
            detail=f"Media expired after {MEDIA_RETENTION_DAYS} days",
        )
    return metadata


def get_media_file(job_id: str) -> Path:
    meta = active_metadata(job_id)
    file_path = job_directory(job_id) / meta["filename"]
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Media file no longer exists")
    return file_path


def public_base_url(request: Request) -> str:
    # Hugging Face provides SPACE_HOST automatically.
    if SPACE_HOST:
        return f"https://{SPACE_HOST}"
    return str(request.base_url).rstrip("/")


def create_media_links(job_id: str, request: Request):
    metadata = active_metadata(job_id)
    retention_expires = media_expiration(job_id, metadata)
    expires = min(int(time.time()) + LINK_TTL_HOURS * 3600, retention_expires)
    signature = create_signature(job_id, expires)
    base = public_base_url(request)
    common = f"{base}/media/{job_id}?expires={expires}&sig={signature}"
    return {
        "stream_url": common + "&download=0",
        "download_url": common + "&download=1",
        "expires": expires,
        "retention_expires": retention_expires,
    }


# ---------------------------------------------------------------------
# yt-dlp worker
# ---------------------------------------------------------------------
def run_download(job_id: str, url: str, mode: str):
    directory = job_directory(job_id)
    try:
        with download_semaphore:
            update_job(job_id, status="downloading", message="Downloading media...")
            directory.mkdir(parents=True, exist_ok=True)

            command = [
                "yt-dlp",
                "--no-playlist",
                "--no-progress",
                "-P", str(directory),
                "-o", "%(title).180B [%(id)s].%(ext)s",
            ]

            if mode == "best":
                pass  # yt-dlp picks the best available combination.
            elif mode == "1080":
                command += ["-f", "bv*[height<=1080]+ba/b[height<=1080]"]
            elif mode == "720":
                command += ["-f", "bv*[height<=720]+ba/b[height<=720]"]
            elif mode == "mp3":
                command += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
            elif mode == "m4a":
                command += ["-x", "--audio-format", "m4a"]
            else:
                raise ValueError(f"Unsupported mode: {mode}")

            command.append(url)

            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

            if result.returncode != 0:
                error = result.stderr.strip()
                if len(error) > 4000:
                    error = error[-4000:]
                raise RuntimeError(error or "yt-dlp failed")

            candidates = [
                p for p in directory.iterdir()
                if p.is_file()
                and not p.name.startswith("_")
                and p.suffix not in {".part", ".ytdl", ".json"}
            ]

            if not candidates:
                raise RuntimeError("yt-dlp completed but no media file was found")

            media_file = max(candidates, key=lambda p: p.stat().st_size)

            created = int(time.time())
            metadata = {
                "job_id": job_id,
                "filename": media_file.name,
                "size": media_file.stat().st_size,
                "source_url": url,
                "mode": mode,
                "created": created,
                "retention_expires": created + MEDIA_RETENTION_DAYS * 86400,
            }
            metadata_path(job_id).write_text(
                json.dumps(metadata, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            update_job(job_id, status="ready", message="Ready", filename=media_file.name, size=metadata["size"])

    except Exception as exc:
        if directory.exists() and not metadata_path(job_id).exists():
            try:
                shutil.rmtree(directory)
            except OSError:
                logger.exception("Could not clean up failed media job %s", job_id)
        update_job(job_id, status="error", message=str(exc))


# ---------------------------------------------------------------------
# API
# ---------------------------------------------------------------------
@app.post("/api/jobs")
def create_job(body: CreateJobRequest):
    verify_password(body.password)

    parsed = urlparse(body.url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Enter a valid http:// or https:// URL")
    if body.mode not in VALID_MODES:
        raise HTTPException(status_code=400, detail="Invalid download mode")

    job_id = uuid.uuid4().hex
    update_job(job_id, status="queued", message="Queued", source_url=body.url, mode=body.mode, created=int(time.time()))

    thread = threading.Thread(target=run_download, args=(job_id, body.url, body.mode), daemon=True)
    thread.start()

    return {"job_id": job_id, "status": "queued"}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str, request: Request):
    validate_job_id(job_id)
    job = get_job(job_id)

    # If the Space restarted, recover finished jobs from the bucket.
    if job is None:
        meta = load_metadata(job_id)
        if meta:
            if is_media_expired(job_id, meta):
                remove_stored_job(job_id)
                raise HTTPException(
                    status_code=410,
                    detail=f"Media expired after {MEDIA_RETENTION_DAYS} days",
                )
            return {
                "job_id": job_id,
                "status": "ready",
                "filename": meta["filename"],
                "size": meta["size"],
                "mode": meta.get("mode", ""),
                **create_media_links(job_id, request),
            }
        raise HTTPException(status_code=404, detail="Job not found")

    result = {"job_id": job_id, **job}
    if job.get("status") == "ready":
        result.update(create_media_links(job_id, request))
    return result


@app.post("/api/jobs/{job_id}/delete")
def delete_job(job_id: str, body: PasswordRequest):
    verify_password(body.password)
    validate_job_id(job_id)
    remove_stored_job(job_id)
    return {"ok": True}


@app.post("/api/library")
def library(body: PasswordRequest, request: Request):
    verify_password(body.password)
    cleanup_expired_media()
    items = []
    if MEDIA_ROOT.exists():
        for child in MEDIA_ROOT.iterdir():
            if not child.is_dir() or not JOB_ID_PATTERN.fullmatch(child.name):
                continue
            meta = load_metadata(child.name)
            if not meta:
                continue
            if not (child / meta["filename"]).exists():
                continue
            try:
                links = create_media_links(child.name, request)
            except HTTPException as exc:
                if exc.status_code == 410:
                    continue
                raise
            items.append({
                "job_id": child.name,
                "filename": meta["filename"],
                "size": meta.get("size", 0),
                "mode": meta.get("mode", ""),
                "created": meta.get("created", 0),
                "retention_expires": media_expiration(child.name, meta),
                **links,
            })
    items.sort(key=lambda i: i.get("created", 0), reverse=True)
    return {"items": items}


# ---------------------------------------------------------------------
# Range-enabled streaming
# ---------------------------------------------------------------------
def file_iterator(path: Path, start: int, end: int, chunk_size: int = 1024 * 1024):
    with path.open("rb") as file:
        file.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            chunk = file.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


@app.api_route("/media/{job_id}", methods=["GET", "HEAD"], name="serve_media")
def serve_media(job_id: str, request: Request, expires: int, sig: str, download: int = 0):
    verify_signature(job_id, expires, sig)
    path = get_media_file(job_id)
    file_size = path.stat().st_size
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    disposition = "attachment" if download else "inline"
    encoded_filename = quote(path.name)
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{encoded_filename}",
    }

    range_header = request.headers.get("range")
    if not range_header:
        headers["Content-Length"] = str(file_size)
        if request.method == "HEAD":
            return Response(status_code=200, media_type=mime_type, headers=headers)
        return StreamingResponse(file_iterator(path, 0, file_size - 1), status_code=200, media_type=mime_type, headers=headers)

    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
    if not match or not any(match.groups()):
        return Response(status_code=416, headers={"Content-Range": f"bytes */{file_size}"})

    start_string, end_string = match.group(1), match.group(2)
    if start_string == "":
        start = max(file_size - int(end_string), 0)
        end = file_size - 1
    else:
        start = int(start_string)
        end = int(end_string) if end_string else file_size - 1

    if start >= file_size or start > end:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{file_size}"})

    end = min(end, file_size - 1)
    headers.update({
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Content-Length": str(end - start + 1),
    })

    if request.method == "HEAD":
        return Response(status_code=206, media_type=mime_type, headers=headers)
    return StreamingResponse(file_iterator(path, start, end), status_code=206, media_type=mime_type, headers=headers)


# ---------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/", response_class=FileResponse)
def home():
    return FRONTEND_DIR / "index.html"
