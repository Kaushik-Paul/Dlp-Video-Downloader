import base64
import binascii
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
from urllib.parse import parse_qs, quote, urlparse

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
YTDLP_PROXY = os.getenv("YTDLP_PROXY", "").strip()
YOUTUBE_COOKIES_B64 = os.getenv("YOUTUBE_COOKIES_B64", "").strip()
YOUTUBE_COOKIES_FILE = Path("/tmp/youtube-cookies.txt")

if LINK_TTL_HOURS <= 0:
    raise RuntimeError("LINK_TTL_HOURS must be greater than zero")
if MEDIA_RETENTION_DAYS <= 0:
    raise RuntimeError("MEDIA_RETENTION_DAYS must be greater than zero")
if YTDLP_PROXY and urlparse(YTDLP_PROXY).scheme not in {
    "http", "https", "socks4", "socks4a", "socks5", "socks5h"
}:
    raise RuntimeError("YTDLP_PROXY must be an HTTP or SOCKS proxy URL")

MEDIA_ROOT.mkdir(parents=True, exist_ok=True)

if YOUTUBE_COOKIES_B64:
    try:
        encoded_cookies = "".join(YOUTUBE_COOKIES_B64.split())
        decoded_cookies = base64.b64decode(encoded_cookies, validate=True)
        if not decoded_cookies or len(decoded_cookies) > 2 * 1024 * 1024:
            raise ValueError("cookie file is empty or too large")
        YOUTUBE_COOKIES_FILE.write_bytes(decoded_cookies)
        YOUTUBE_COOKIES_FILE.chmod(0o600)
    except (binascii.Error, OSError, ValueError) as exc:
        raise RuntimeError("YOUTUBE_COOKIES_B64 is not a valid cookie file") from exc

VALID_MODES = {"best", "1080", "720", "audio", "mp3", "m4a"}
VALID_DELIVERIES = {"stored", "instant"}
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
    delivery: str = "stored"
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
    # A custom-domain proxy can append its host to Hugging Face's host. Use the
    # final forwarded value instead of emitting an invalid comma-separated URL.
    configured_host = (
        SPACE_HOST
        or request.headers.get("x-forwarded-host", "")
        or request.headers.get("host", "")
    )
    hosts = [host.strip() for host in configured_host.split(",") if host.strip()]
    if hosts:
        host = hosts[-1]
        parsed_host = urlparse(f"//{host}")
        if (
            parsed_host.hostname
            and parsed_host.netloc == host
            and parsed_host.username is None
            and parsed_host.password is None
        ):
            forwarded_proto = request.headers.get("x-forwarded-proto", "https")
            scheme = forwarded_proto.split(",")[-1].strip().lower()
            if scheme not in {"http", "https"}:
                scheme = "https"
            return f"{scheme}://{host}"
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


def direct_link_expiration(urls: list[str]) -> int | None:
    """Return the earliest plausible Unix expiry embedded in provider URLs."""
    now = int(time.time())
    expirations = []
    for url in urls:
        try:
            query = parse_qs(urlparse(url).query)
        except ValueError:
            continue
        for key in ("expire", "expires", "exp"):
            for value in query.get(key, []):
                try:
                    expiration = int(value)
                except (TypeError, ValueError):
                    continue
                if expiration > 10_000_000_000:
                    expiration //= 1000
                if now < expiration < now + 10 * 365 * 86400:
                    expirations.append(expiration)
    return min(expirations) if expirations else None


def instant_format_selector(mode: str) -> str:
    if mode == "best":
        return "b[vcodec!=none][acodec!=none]"
    if mode == "1080":
        return "b[height<=1080][vcodec!=none][acodec!=none]"
    if mode == "720":
        return "b[height<=720][vcodec!=none][acodec!=none]"
    if mode == "audio":
        return "ba"
    raise ValueError("MP3 and M4A conversion requires 30-day storage")


def selected_direct_formats(info: dict) -> list[dict]:
    selected = info.get("requested_downloads")
    if isinstance(selected, list) and selected:
        formats = [item for item in selected if isinstance(item, dict) and item.get("url")]
        if formats:
            return formats

    requested = info.get("requested_formats")
    if isinstance(requested, list) and requested:
        formats = [item for item in requested if isinstance(item, dict) and item.get("url")]
        if formats:
            return formats

    return [info] if info.get("url") else []


def yt_dlp_network_args() -> list[str]:
    arguments = [
        "--force-ipv4",
        "--impersonate",
        "chrome",
        "--extractor-args",
        "youtube:player_client=mweb",
    ]
    if YTDLP_PROXY:
        arguments.extend(("--proxy", YTDLP_PROXY))
    if YOUTUBE_COOKIES_B64:
        arguments.extend(("--cookies", str(YOUTUBE_COOKIES_FILE)))
    return arguments


def yt_dlp_error(stderr: str, fallback: str) -> str:
    error = stderr.strip()
    if YTDLP_PROXY:
        error = error.replace(YTDLP_PROXY, "[configured proxy]")
    if "Sign in to confirm" in error and "not a bot" in error:
        return (
            "YouTube blocked this Space's shared datacenter IP even after the "
            "PO-token attempt. Configure the YTDLP_PROXY Space secret (recommended) "
            "or YOUTUBE_COOKIES_B64; see README.md."
        )
    if len(error) > 4000:
        error = error[-4000:]
    return error or fallback


def run_instant_link(job_id: str, url: str, mode: str):
    try:
        update_job(job_id, status="extracting", message="Extracting source links...")
        command = [
            "yt-dlp",
            "--no-playlist",
            *yt_dlp_network_args(),
            "--no-progress",
            "--no-warnings",
            "--skip-download",
            "--dump-single-json",
            "-f",
            instant_format_selector(mode),
            url,
        ]
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(
                yt_dlp_error(result.stderr, "yt-dlp could not extract a source link")
            )

        try:
            info = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("yt-dlp returned invalid link metadata") from exc

        formats = selected_direct_formats(info)
        if not formats:
            raise RuntimeError("The provider did not return a directly playable URL")

        combined_url = None
        video_url = None
        audio_url = None
        for selected in formats:
            selected_url = selected["url"]
            has_video = selected.get("vcodec") not in (None, "none")
            has_audio = selected.get("acodec") not in (None, "none")
            if has_video and has_audio and combined_url is None:
                combined_url = selected_url
            elif has_video and video_url is None:
                video_url = selected_url
            elif has_audio and audio_url is None:
                audio_url = selected_url

        primary_url = combined_url or video_url or audio_url or formats[0]["url"]
        urls = [item["url"] for item in formats]
        sizes = [
            item.get("filesize") or item.get("filesize_approx")
            for item in formats
        ]
        known_sizes = [size for size in sizes if isinstance(size, (int, float))]
        title = info.get("title") or "Source media"
        extension = info.get("ext") or formats[0].get("ext") or "media"
        filename = info.get("_filename") or f"{title}.{extension}"
        headers_required = any(bool(item.get("http_headers")) for item in formats)

        update_job(
            job_id,
            status="ready",
            message="Source links ready",
            delivery="instant",
            stored=False,
            filename=filename,
            size=int(sum(known_sizes)) if known_sizes else 0,
            stream_url=primary_url,
            download_url=primary_url,
            video_url=video_url,
            audio_url=audio_url,
            separate_streams=bool(video_url and audio_url and not combined_url),
            source_expires=direct_link_expiration(urls),
            provider_headers_required=headers_required,
        )
    except subprocess.TimeoutExpired:
        update_job(job_id, status="error", message="Source link extraction timed out")
    except Exception as exc:
        update_job(job_id, status="error", message=str(exc))


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
                *yt_dlp_network_args(),
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
            elif mode == "audio":
                command += ["-f", "ba"]
            elif mode == "mp3":
                command += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
            elif mode == "m4a":
                command += ["-x", "--audio-format", "m4a"]
            else:
                raise ValueError(f"Unsupported mode: {mode}")

            command.append(url)

            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

            if result.returncode != 0:
                raise RuntimeError(yt_dlp_error(result.stderr, "yt-dlp failed"))

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
    if body.delivery not in VALID_DELIVERIES:
        raise HTTPException(status_code=400, detail="Invalid delivery mode")
    if body.delivery == "instant" and body.mode in {"mp3", "m4a"}:
        raise HTTPException(
            status_code=400,
            detail="MP3 and M4A conversion requires 30-day storage",
        )

    job_id = uuid.uuid4().hex
    update_job(
        job_id,
        status="queued",
        message="Queued",
        source_url=body.url,
        mode=body.mode,
        delivery=body.delivery,
        created=int(time.time()),
    )

    worker = run_instant_link if body.delivery == "instant" else run_download
    thread = threading.Thread(target=worker, args=(job_id, body.url, body.mode), daemon=True)
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
    if job.get("status") == "ready" and job.get("delivery") != "instant":
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
