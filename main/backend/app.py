import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import math
import mimetypes
import os
import re
import signal
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
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
        terminate_transfer_processes()
        close_upload_sessions()


app = FastAPI(title="YT-DLP Media Server", lifespan=app_lifespan)


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------
MEDIA_ROOT = Path(os.getenv("MEDIA_DIR", "/data/media"))
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
SIGNING_SECRET = os.getenv("SIGNING_SECRET", APP_PASSWORD)
LINK_TTL_HOURS = int(os.getenv("LINK_TTL_HOURS", "720"))
MEDIA_RETENTION_DAYS = int(os.getenv("MEDIA_RETENTION_DAYS", "30"))
UPLOAD_MAX_GIB = int(os.getenv("UPLOAD_MAX_GIB", "20"))
UPLOAD_CHUNK_MIB = int(os.getenv("UPLOAD_CHUNK_MIB", "32"))
UPLOAD_CONCURRENCY = int(os.getenv("UPLOAD_CONCURRENCY", "6"))
DOWNLOAD_FRAGMENT_CONCURRENCY = int(os.getenv("DOWNLOAD_FRAGMENT_CONCURRENCY", "4"))
SPACE_HOST = os.getenv("SPACE_HOST")
YTDLP_PROXY = os.getenv("YTDLP_PROXY", "").strip()
YOUTUBE_COOKIES_B64 = os.getenv("YOUTUBE_COOKIES_B64", "").strip()
YOUTUBE_COOKIES_FILE = Path("/tmp/youtube-cookies.txt")

if LINK_TTL_HOURS <= 0:
    raise RuntimeError("LINK_TTL_HOURS must be greater than zero")
if MEDIA_RETENTION_DAYS <= 0:
    raise RuntimeError("MEDIA_RETENTION_DAYS must be greater than zero")
if UPLOAD_MAX_GIB <= 0:
    raise RuntimeError("UPLOAD_MAX_GIB must be greater than zero")
if not 5 <= UPLOAD_CHUNK_MIB <= 128:
    raise RuntimeError("UPLOAD_CHUNK_MIB must be between 5 and 128")
if not 1 <= UPLOAD_CONCURRENCY <= 12:
    raise RuntimeError("UPLOAD_CONCURRENCY must be between 1 and 12")
if not 1 <= DOWNLOAD_FRAGMENT_CONCURRENCY <= 8:
    raise RuntimeError("DOWNLOAD_FRAGMENT_CONCURRENCY must be between 1 and 8")
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
UPLOAD_SESSION_TTL_SECONDS = 6 * 3600
UPLOAD_MAX_BYTES = UPLOAD_MAX_GIB * 1024**3
UPLOAD_CHUNK_SIZE = UPLOAD_CHUNK_MIB * 1024**2
UPLOAD_WRITE_BUFFER_SIZE = 4 * 1024**2

# Only run one yt-dlp process at a time on CPU Basic hardware.
download_semaphore = threading.Semaphore(1)
jobs = {}
jobs_lock = threading.Lock()
transfer_processes: dict[str, subprocess.Popen] = {}
transfer_processes_lock = threading.Lock()
ACTIVE_TRANSFER_STATUSES = {"queued", "extracting", "downloading", "uploading"}


class TransferCancelled(Exception):
    pass


@dataclass
class UploadSession:
    job_id: str
    filename: str
    path: Path
    size: int
    chunk_size: int
    chunk_count: int
    file_descriptor: int
    created: int
    completed_chunks: set[int] = field(default_factory=set)
    active_chunks: set[int] = field(default_factory=set)
    received_bytes: int = 0
    last_activity: float = field(default_factory=time.monotonic)
    cancelled: bool = False
    finalizing: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


upload_sessions: dict[str, UploadSession] = {}
upload_sessions_lock = threading.Lock()


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


class CreateUploadRequest(BaseModel):
    filename: str
    size: int
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


def update_job_unless_cancelled(job_id: str, **values) -> bool:
    with jobs_lock:
        job = jobs.setdefault(job_id, {})
        if job.get("status") == "cancelled":
            return False
        job.update(values)
        return True


def get_job(job_id: str):
    with jobs_lock:
        if job_id in jobs:
            return dict(jobs[job_id])
    return None


def job_is_cancelled(job_id: str) -> bool:
    with jobs_lock:
        return jobs.get(job_id, {}).get("status") == "cancelled"


def signal_transfer_process(process: subprocess.Popen, process_signal: int):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, process_signal)
    except ProcessLookupError:
        pass
    except OSError:
        logger.exception("Could not signal transfer process %s", process.pid)


def terminate_transfer_processes(
    processes: list[subprocess.Popen] | None = None,
    timeout: float = 2.0,
) -> int:
    if processes is None:
        with transfer_processes_lock:
            processes = list(transfer_processes.values())
    running = [process for process in processes if process.poll() is None]
    for process in running:
        signal_transfer_process(process, signal.SIGTERM)

    deadline = time.monotonic() + timeout
    while running and time.monotonic() < deadline:
        running = [process for process in running if process.poll() is None]
        if running:
            time.sleep(0.05)
    for process in running:
        signal_transfer_process(process, signal.SIGKILL)
    return len(processes)


def run_transfer_process(
    job_id: str,
    command: list[str],
    *,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    if job_is_cancelled(job_id):
        raise TransferCancelled
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    with transfer_processes_lock:
        transfer_processes[job_id] = process
    if job_is_cancelled(job_id):
        signal_transfer_process(process, signal.SIGTERM)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        terminate_transfer_processes([process])
        process.communicate()
        raise
    finally:
        with transfer_processes_lock:
            if transfer_processes.get(job_id) is process:
                transfer_processes.pop(job_id, None)
    if job_is_cancelled(job_id):
        raise TransferCancelled
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def job_directory(job_id: str) -> Path:
    validate_job_id(job_id)
    return MEDIA_ROOT / job_id


def metadata_path(job_id: str) -> Path:
    return job_directory(job_id) / "_meta.json"


def upload_marker_path(job_id: str) -> Path:
    return job_directory(job_id) / "_upload"


def safe_upload_filename(filename: str) -> str:
    name = filename.strip()
    if (
        not name
        or name in {".", "..", "_meta.json"}
        or Path(name).name != name
        or "/" in name
        or "\\" in name
        or any(ord(character) < 32 for character in name)
        or len(name.encode("utf-8")) > 240
    ):
        raise HTTPException(status_code=400, detail="Invalid filename")
    return name


def get_upload_session(job_id: str) -> UploadSession:
    validate_job_id(job_id)
    with upload_sessions_lock:
        session = upload_sessions.get(job_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Upload session not found")
    return session


def close_file_descriptor(file_descriptor: int):
    if file_descriptor < 0:
        return
    try:
        os.close(file_descriptor)
    except OSError:
        pass


def close_upload_file(session: UploadSession):
    with session.lock:
        file_descriptor = session.file_descriptor
        session.file_descriptor = -1
    close_file_descriptor(file_descriptor)


def dispose_upload_session(session: UploadSession, *, remove_files: bool):
    with upload_sessions_lock:
        if upload_sessions.get(session.job_id) is session:
            upload_sessions.pop(session.job_id, None)
    close_upload_file(session)
    if remove_files:
        try:
            shutil.rmtree(job_directory(session.job_id))
        except FileNotFoundError:
            pass
        except OSError:
            logger.exception("Could not clean up upload session %s", session.job_id)


def cancel_upload_session(job_id: str) -> bool:
    with upload_sessions_lock:
        session = upload_sessions.get(job_id)
    if session is None:
        return False
    with session.lock:
        session.cancelled = True
        session.last_activity = time.monotonic()
        idle = not session.active_chunks and not session.finalizing
    if idle:
        dispose_upload_session(session, remove_files=True)
    return True


def close_upload_sessions():
    with upload_sessions_lock:
        sessions = list(upload_sessions.values())
        upload_sessions.clear()
    for session in sessions:
        with session.lock:
            session.cancelled = True
        close_upload_file(session)


def cleanup_stale_uploads() -> int:
    cutoff = time.monotonic() - UPLOAD_SESSION_TTL_SECONDS
    with upload_sessions_lock:
        sessions = list(upload_sessions.values())
    stale = []
    for session in sessions:
        with session.lock:
            if (
                session.last_activity <= cutoff
                and not session.active_chunks
                and not session.finalizing
            ):
                session.cancelled = True
                stale.append(session)
    for session in stale:
        dispose_upload_session(session, remove_files=True)
        with jobs_lock:
            jobs.pop(session.job_id, None)
    return len(stale)


def write_at(file_descriptor: int, data: bytes, offset: int):
    view = memoryview(data)
    written = 0
    while written < len(view):
        count = os.pwrite(file_descriptor, view[written:], offset + written)
        if count <= 0:
            raise OSError("Could not write upload chunk")
        written += count


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
    uploading = cancel_upload_session(job_id)
    directory = job_directory(job_id)
    if directory.exists() and not uploading:
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
        elif upload_marker_path(child.name).exists():
            # Upload sessions cannot resume after a process restart. The marker
            # lets startup cleanup remove their potentially huge partial files.
            with upload_sessions_lock:
                expired = child.name not in upload_sessions
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
            removed_uploads = cleanup_stale_uploads()
            if removed_uploads:
                logger.info("Removed %d stale upload session(s)", removed_uploads)
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
            forwarded_proto = request.headers.get("x-forwarded-proto")
            scheme = (
                forwarded_proto.split(",")[-1].strip().lower()
                if forwarded_proto
                else request.url.scheme.lower()
            )
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


def yt_dlp_network_args(*, download: bool = False) -> list[str]:
    arguments = [
        "--force-ipv4",
        "--impersonate",
        "chrome",
        "--extractor-args",
        "youtube:player_client=mweb",
    ]
    if download:
        arguments.extend(
            ("--concurrent-fragments", str(DOWNLOAD_FRAGMENT_CONCURRENCY))
        )
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
        if not update_job_unless_cancelled(
            job_id,
            status="extracting",
            message="Extracting source links...",
        ):
            raise TransferCancelled
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
        result = run_transfer_process(job_id, command, timeout=120)
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

        update_job_unless_cancelled(
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
    except TransferCancelled:
        pass
    except subprocess.TimeoutExpired:
        update_job_unless_cancelled(
            job_id,
            status="error",
            message="Source link extraction timed out",
        )
    except Exception as exc:
        update_job_unless_cancelled(job_id, status="error", message=str(exc))


# ---------------------------------------------------------------------
# yt-dlp worker
# ---------------------------------------------------------------------
def run_download(job_id: str, url: str, mode: str):
    directory = job_directory(job_id)
    try:
        if job_is_cancelled(job_id):
            raise TransferCancelled
        with download_semaphore:
            if not update_job_unless_cancelled(
                job_id,
                status="downloading",
                message="Downloading media...",
            ):
                raise TransferCancelled
            directory.mkdir(parents=True, exist_ok=True)

            command = [
                "yt-dlp",
                "--no-playlist",
                *yt_dlp_network_args(download=True),
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

            result = run_transfer_process(job_id, command)

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

            if not update_job_unless_cancelled(
                job_id,
                status="ready",
                message="Ready",
                filename=media_file.name,
                size=metadata["size"],
            ):
                raise TransferCancelled

    except TransferCancelled:
        if directory.exists():
            try:
                shutil.rmtree(directory)
            except OSError:
                logger.exception("Could not clean up cancelled media job %s", job_id)
    except Exception as exc:
        if directory.exists() and not metadata_path(job_id).exists():
            try:
                shutil.rmtree(directory)
            except OSError:
                logger.exception("Could not clean up failed media job %s", job_id)
        update_job_unless_cancelled(job_id, status="error", message=str(exc))


# ---------------------------------------------------------------------
# API
# ---------------------------------------------------------------------
@app.post("/api/transfers/cancel")
def cancel_all_transfers():
    with jobs_lock:
        cancelled_job_ids = [
            job_id
            for job_id, job in jobs.items()
            if job.get("status") in ACTIVE_TRANSFER_STATUSES
        ]
        for job_id in cancelled_job_ids:
            jobs[job_id].update(
                status="cancelled",
                message="Cancelled by user",
            )

    with upload_sessions_lock:
        upload_job_ids = list(upload_sessions)
    cancelled_uploads = sum(
        1 for job_id in upload_job_ids if cancel_upload_session(job_id)
    )

    with transfer_processes_lock:
        processes = list(transfer_processes.values())
    terminated_processes = terminate_transfer_processes(processes)
    return {
        "ok": True,
        "cancelled_jobs": len(cancelled_job_ids),
        "cancelled_uploads": cancelled_uploads,
        "terminated_processes": terminated_processes,
    }


@app.post("/api/uploads")
def create_upload(body: CreateUploadRequest):
    verify_password(body.password)
    filename = safe_upload_filename(body.filename)
    if body.size <= 0:
        raise HTTPException(status_code=400, detail="The file is empty")
    if body.size > UPLOAD_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Files are limited to {UPLOAD_MAX_GIB} GiB",
        )

    cleanup_stale_uploads()
    with upload_sessions_lock:
        if len(upload_sessions) >= 4:
            raise HTTPException(
                status_code=503,
                detail="Too many uploads are already in progress",
            )

    job_id = uuid.uuid4().hex
    directory = job_directory(job_id)
    path = directory / filename
    file_descriptor = None
    try:
        directory.mkdir(parents=True, exist_ok=False)
        file_descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as exc:
        if file_descriptor is not None:
            close_file_descriptor(file_descriptor)
        try:
            shutil.rmtree(directory)
        except OSError:
            pass
        logger.exception("Could not create upload session %s", job_id)
        raise HTTPException(status_code=500, detail="Could not create upload") from exc

    created = int(time.time())
    chunk_count = math.ceil(body.size / UPLOAD_CHUNK_SIZE)
    session = UploadSession(
        job_id=job_id,
        filename=filename,
        path=path,
        size=body.size,
        chunk_size=UPLOAD_CHUNK_SIZE,
        chunk_count=chunk_count,
        file_descriptor=file_descriptor,
        created=created,
    )
    try:
        with upload_sessions_lock:
            upload_marker_path(job_id).touch(exist_ok=False)
            upload_sessions[job_id] = session
    except OSError as exc:
        close_file_descriptor(file_descriptor)
        try:
            shutil.rmtree(directory)
        except OSError:
            pass
        logger.exception("Could not persist upload session %s", job_id)
        raise HTTPException(status_code=500, detail="Could not create upload") from exc
    update_job(
        job_id,
        status="uploading",
        message="Uploading file...",
        filename=filename,
        size=body.size,
        uploaded=0,
        mode="upload",
        delivery="stored",
        created=created,
    )
    return {
        "job_id": job_id,
        "status": "uploading",
        "chunk_size": UPLOAD_CHUNK_SIZE,
        "chunk_count": chunk_count,
        "concurrency": min(UPLOAD_CONCURRENCY, chunk_count),
    }


@app.put("/api/uploads/{job_id}/chunks/{chunk_index}")
async def upload_chunk(job_id: str, chunk_index: int, request: Request):
    verify_password(request.headers.get("x-app-password", ""))
    session = get_upload_session(job_id)
    if chunk_index < 0 or chunk_index >= session.chunk_count:
        raise HTTPException(status_code=400, detail="Invalid upload chunk")

    offset = chunk_index * session.chunk_size
    expected_size = min(session.chunk_size, session.size - offset)
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_size = int(content_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc
        if declared_size != expected_size:
            raise HTTPException(status_code=400, detail="Upload chunk has the wrong size")

    with session.lock:
        if session.cancelled or session.finalizing:
            raise HTTPException(status_code=409, detail="Upload was cancelled")
        if chunk_index in session.completed_chunks:
            return {"ok": True, "chunk": chunk_index, "already_uploaded": True}
        if chunk_index in session.active_chunks:
            raise HTTPException(status_code=409, detail="Upload chunk is already in progress")
        session.active_chunks.add(chunk_index)
        session.last_activity = time.monotonic()

    received = 0
    buffer = bytearray()
    completed = False
    try:
        async for block in request.stream():
            if not block:
                continue
            with session.lock:
                if session.cancelled:
                    raise HTTPException(status_code=409, detail="Upload was cancelled")
            if received + len(buffer) + len(block) > expected_size:
                raise HTTPException(status_code=400, detail="Upload chunk is too large")
            buffer.extend(block)
            if len(buffer) >= UPLOAD_WRITE_BUFFER_SIZE:
                payload = bytes(buffer)
                buffer.clear()
                await asyncio.to_thread(
                    write_at,
                    session.file_descriptor,
                    payload,
                    offset + received,
                )
                received += len(payload)
        if buffer:
            payload = bytes(buffer)
            await asyncio.to_thread(
                write_at,
                session.file_descriptor,
                payload,
                offset + received,
            )
            received += len(payload)
        if received != expected_size:
            raise HTTPException(status_code=400, detail="Upload chunk is incomplete")

        with session.lock:
            if session.cancelled:
                raise HTTPException(status_code=409, detail="Upload was cancelled")
            session.completed_chunks.add(chunk_index)
            session.received_bytes += expected_size
            session.last_activity = time.monotonic()
            uploaded = session.received_bytes
            completed = True
        update_job_unless_cancelled(
            job_id,
            uploaded=uploaded,
            message=f"Uploading file... {uploaded * 100 // session.size}%",
        )
        return {"ok": True, "chunk": chunk_index, "uploaded": uploaded}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Could not write chunk %d for upload %s", chunk_index, job_id)
        raise HTTPException(status_code=500, detail="Could not write upload chunk") from exc
    finally:
        with session.lock:
            session.active_chunks.discard(chunk_index)
            if not completed:
                session.last_activity = time.monotonic()
            dispose = (
                session.cancelled
                and not session.active_chunks
                and not session.finalizing
            )
        if dispose:
            dispose_upload_session(session, remove_files=True)


@app.post("/api/uploads/{job_id}/complete")
async def complete_upload(job_id: str, body: PasswordRequest, request: Request):
    verify_password(body.password)
    session = get_upload_session(job_id)
    with session.lock:
        if session.cancelled:
            raise HTTPException(status_code=409, detail="Upload was cancelled")
        if session.finalizing:
            raise HTTPException(status_code=409, detail="Upload is already finalizing")
        if session.active_chunks:
            raise HTTPException(status_code=409, detail="Upload chunks are still in progress")
        if len(session.completed_chunks) != session.chunk_count:
            missing = session.chunk_count - len(session.completed_chunks)
            raise HTTPException(status_code=409, detail=f"{missing} upload chunk(s) are missing")
        session.finalizing = True
        file_descriptor = session.file_descriptor

    try:
        await asyncio.to_thread(os.fsync, file_descriptor)
        with session.lock:
            if session.cancelled:
                raise TransferCancelled
        if session.path.stat().st_size != session.size:
            raise OSError("Uploaded file size does not match")

        created = int(time.time())
        metadata = {
            "job_id": job_id,
            "filename": session.filename,
            "size": session.size,
            "mode": "upload",
            "created": created,
            "retention_expires": created + MEDIA_RETENTION_DAYS * 86400,
        }
        metadata_path(job_id).write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        upload_marker_path(job_id).unlink(missing_ok=True)
        if not update_job_unless_cancelled(
            job_id,
            status="ready",
            message="Ready",
            filename=session.filename,
            size=session.size,
            uploaded=session.size,
            retention_expires=metadata["retention_expires"],
        ):
            raise TransferCancelled
    except TransferCancelled as exc:
        with session.lock:
            session.finalizing = False
        dispose_upload_session(session, remove_files=True)
        raise HTTPException(status_code=409, detail="Upload was cancelled") from exc
    except Exception as exc:
        logger.exception("Could not finalize upload %s", job_id)
        update_job_unless_cancelled(
            job_id,
            status="error",
            message="Could not finalize upload",
        )
        with session.lock:
            session.finalizing = False
        dispose_upload_session(session, remove_files=True)
        raise HTTPException(status_code=500, detail="Could not finalize upload") from exc

    with session.lock:
        session.finalizing = False
    dispose_upload_session(session, remove_files=False)
    job = get_job(job_id) or {}
    return {"job_id": job_id, **job, **create_media_links(job_id, request)}


@app.post("/api/uploads/{job_id}/abort")
def abort_upload(job_id: str, body: PasswordRequest):
    verify_password(body.password)
    validate_job_id(job_id)
    cancelled = cancel_upload_session(job_id)
    with jobs_lock:
        jobs.pop(job_id, None)
    return {"ok": True, "cancelled": cancelled}


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
def file_iterator(path: Path, start: int, end: int, chunk_size: int = 8 * 1024 * 1024):
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
        "Cache-Control": "private, no-transform",
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{encoded_filename}",
        "X-Content-Type-Options": "nosniff",
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
