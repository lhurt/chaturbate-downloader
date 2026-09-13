"""
FastAPI web server for the Chaturbate stream downloader.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from downloader import DownloadManager
from downloader.auto_download import AutoDownloadScheduler
from downloader.converter import _probe_duration, generate_contact_sheet
from downloader.extractor import DEFAULT_HEADERS, fetch_room_status
from downloader.hls import _abs_url, _select_best_variant
from downloader.http_client import proxy_kwargs
from downloader.redact import _redact_text_urls, _redact_url
from downloader.tracker import Tracker

# Configure logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
DOWNLOADS_DIR = (Path(__file__).parent / "downloads").resolve()
DOWNLOADS_DIR.mkdir(exist_ok=True)

# Global download manager
manager = DownloadManager(
    output_dir=DOWNLOADS_DIR, on_complete=lambda path: _schedule_contact_sheet(Path(path))
)
tracker = Tracker(DOWNLOADS_DIR / "tracked.db")
AUTO_DOWNLOAD_MAX_CONCURRENT = int(os.getenv("AUTO_DOWNLOAD_MAX_CONCURRENT", "4"))
auto_download_scheduler = AutoDownloadScheduler(
    manager, tracker, max_concurrent=AUTO_DOWNLOAD_MAX_CONCURRENT
)

POLL_INTERVAL_SECONDS = 60

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{1,50}$")
COMPLETED_STEM_RE = re.compile(
    r"^(?P<username>.+)_(?P<timestamp>\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})$"
)
TEMP_TRACK_STEM_RE = re.compile(
    r"^(?P<username>.+)_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_(?:video|audio)$"
)
CONTACT_SHEET_STEM_RE = re.compile(r"^(?P<source_stem>.+)_contactsheet$")

ALLOWED_ORIGIN_SET = {
    origin.strip().rstrip("/")
    for origin in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:8000,http://127.0.0.1:8000,http://[::1]:8000",
    ).split(",")
    if origin.strip()
}
ALLOWED_ORIGINS = sorted(ALLOWED_ORIGIN_SET)

CONTACT_SHEET_INTERVAL_SECONDS = int(os.getenv("CONTACT_SHEET_INTERVAL_SECONDS", "60"))
CONTACT_SHEET_TILE_WIDTH = int(os.getenv("CONTACT_SHEET_TILE_WIDTH", "160"))
CONTACT_SHEET_COLUMNS = int(os.getenv("CONTACT_SHEET_COLUMNS", "10"))
CONTACT_SHEET_AUTO_GENERATE = os.getenv(
    "CONTACT_SHEET_AUTO_GENERATE", "false"
).strip().lower() in ("1", "true", "yes", "on")

MODAL_WIDTH_PERCENT = max(20, min(100, int(os.getenv("MODAL_WIDTH_PERCENT", "80"))))


def _validate_username(username: str) -> str:
    """Validate and normalize a username."""
    username = username.strip().lower()
    if not USERNAME_RE.match(username):
        raise HTTPException(status_code=400, detail="Invalid username")
    return username


def _safe_downloads_path(filename: str) -> Path:
    """Resolve a filename inside DOWNLOADS_DIR, preventing path traversal."""
    file_path = (DOWNLOADS_DIR / filename).resolve()
    if not file_path.is_relative_to(DOWNLOADS_DIR):
        raise HTTPException(status_code=400, detail="Invalid filename")
    return file_path


def _is_completed_media_file(path: Path) -> bool:
    """Return True for user-facing completed media outputs only."""
    return (
        path.is_file()
        and path.suffix == ".mp4"
        and not path.stem.endswith(("_video", "_audio"))
    )


def _cleanup_orphaned_temp_files() -> list[str]:
    """Remove leftover `_video.mp4`/`_audio.mp4` temp tracks with no owning
    download. These are produced by HLSDownloader mid-download and are only
    ever renamed/removed by a normal finalize(); a crash, force-cancel after
    the stop watchdog, or ungraceful shutdown skips that step and leaves them
    behind. They're intentionally hidden from every read endpoint, so this is
    the only path that reclaims them. Safe to call any time: usernames with
    an active reservation or task are left untouched."""
    active = manager.active_usernames()
    removed = []
    for f in DOWNLOADS_DIR.iterdir():
        if not f.is_file() or f.suffix != ".mp4":
            continue
        match = TEMP_TRACK_STEM_RE.match(f.stem)
        if not match or match.group("username") in active:
            continue
        try:
            f.unlink()
            removed.append(f.name)
        except OSError as exc:
            logger.warning("Failed to remove orphaned temp file %s: %s", f.name, exc)
    if removed:
        logger.info("Removed %d orphaned temp file(s): %s", len(removed), removed)
    return removed


def _username_from_completed_stem(stem: str) -> str:
    """Extract username only when the final suffix is the exact timestamp format."""
    match = COMPLETED_STEM_RE.match(stem)
    if not match:
        return stem
    return match.group("username")


def _require_trusted_origin(request: Request) -> None:
    """Reject browser cross-site state-changing requests to localhost."""
    sec_fetch_site = request.headers.get("sec-fetch-site", "").lower()
    if sec_fetch_site == "cross-site":
        raise HTTPException(status_code=403, detail="Cross-site requests are not allowed")

    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") not in ALLOWED_ORIGIN_SET:
        raise HTTPException(status_code=403, detail="Origin is not allowed")


def _status_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(15.0),
        follow_redirects=True,
        headers={**DEFAULT_HEADERS, "Referer": "https://chaturbate.com/"},
        **proxy_kwargs(),
    )


async def _check_tracked_status(client: httpx.AsyncClient, username: str) -> Optional[str]:
    """Fetch and persist one username's live status, scheduling an auto-download
    if it just went public. Shared by the background poller and the manual
    refresh endpoints so both paths behave identically."""
    status = await fetch_room_status(client, username)
    if status is not None:
        await tracker.update_status(username, status)
        if status == "public":
            auto_download_scheduler.schedule(username)
    return status


async def _refresh_all_tracked_status() -> int:
    usernames = await tracker.list_usernames()
    if usernames:
        async with _status_client() as client:
            for username in usernames:
                try:
                    await _check_tracked_status(client, username)
                except Exception as exc:
                    logger.debug("status poll failed for %s: %s", username, exc)
    _prune_thumb_cache(set(usernames))
    return len(usernames)


async def _poll_tracked_status() -> None:
    while True:
        try:
            await _refresh_all_tracked_status()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("status poller iteration failed: %s", exc)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handle startup/shutdown."""
    logger.info("Starting Chaturbate Downloader")
    _cleanup_orphaned_temp_files()
    _cleanup_orphaned_contact_sheets()
    poll_task = asyncio.create_task(_poll_tracked_status())
    try:
        yield
    finally:
        poll_task.cancel()
        try:
            await poll_task
        except (asyncio.CancelledError, Exception):
            pass
        await auto_download_scheduler.stop()
        await manager.stop_all()
        tracker.close()
        logger.info("Shutting down")


app = FastAPI(title="Chaturbate Stream Downloader", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


# ─── Web UI ────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Serve the main web UI."""
    return templates.TemplateResponse(
        request, "index.html", {"modal_width_percent": MODAL_WIDTH_PERCENT}
    )


# ─── API Endpoints ─────────────────────────────────────────


@app.post("/api/download/start")
async def start_download(
    request: Request,
    username: str,
    output_format: str = "mp4",
    max_duration: Optional[int] = None,
):
    """Start downloading a stream."""
    _require_trusted_origin(request)
    username = _validate_username(username)
    if output_format != "mp4":
        raise HTTPException(status_code=400, detail="Format must be 'mp4'")
    if max_duration is not None and max_duration <= 0:
        raise HTTPException(status_code=400, detail="max_duration must be positive")
    # Convert minutes (from UI) to seconds for the backend
    duration_seconds = max_duration * 60 if max_duration else None
    result = await manager.start_download(
        username=username,
        max_duration=duration_seconds,
    )
    if "error" in result:
        raise HTTPException(status_code=409, detail=result["error"])
    await tracker.upsert_download(username)
    await tracker.update_status(username, "public")
    return result


@app.post("/api/download/stop/{username}")
async def stop_download(request: Request, username: str):
    """Stop a specific download."""
    _require_trusted_origin(request)
    username = _validate_username(username)
    result = await manager.stop_download(username)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.post("/api/download/stop-all")
async def stop_all(request: Request):
    """Stop all active downloads."""
    _require_trusted_origin(request)
    return await manager.stop_all()


@app.get("/api/download/status")
async def get_all_status():
    """Get status of all downloads."""
    return manager.get_status()


@app.get("/api/download/status/{username}")
async def get_status(username: str):
    """Get status of a specific download."""
    username = _validate_username(username)
    result = manager.get_download(username)
    if result is None:
        raise HTTPException(status_code=404, detail="Download not found")
    return result


@app.get("/api/download/file/{username}")
async def download_file(username: str):
    """Download the completed file."""
    username = _validate_username(username)

    # Try download status first
    status = manager.get_download(username)
    if status and status.get("output_path"):
        file_path = Path(status["output_path"]).resolve()
        if file_path.is_relative_to(DOWNLOADS_DIR) and _is_completed_media_file(file_path):
            return FileResponse(
                path=str(file_path),
                media_type="video/mp4",
                filename=file_path.name,
            )

    # Fallback: scan downloads directory for any file starting with username_
    for f in sorted(DOWNLOADS_DIR.iterdir(), reverse=True):
        if _is_completed_media_file(f) and f.name.startswith(f"{username}_"):
            return FileResponse(
                path=str(f),
                media_type="video/mp4",
                filename=f.name,
            )

    raise HTTPException(status_code=404, detail="File not found")


@app.get("/api/downloads/file/{filename}")
async def download_file_by_name(filename: str):
    """Download an exact completed filename."""
    file_path = _safe_downloads_path(filename)
    if not file_path.exists() or not _is_completed_media_file(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        path=str(file_path),
        media_type="video/mp4",
        filename=file_path.name,
    )


_CONTACT_SHEET_LOCK = asyncio.Lock()
_contact_sheet_pending: set[str] = set()


def _contact_sheet_path(file_path: Path) -> Path:
    return file_path.with_name(file_path.stem + "_contactsheet.jpg")


def _cleanup_orphaned_contact_sheets() -> list[str]:
    """Remove contact-sheet JPEGs whose source recording no longer exists.
    `delete_file` removes the sheet alongside the .mp4 when a user deletes
    through the API, but the .mp4 can also disappear by other means -- a
    manual `rm`, or an external tool watching `downloads/` (a NAS scanner,
    jDownloader, etc. -- see the bind mounts in compose.override.yaml) that
    has no reason to know about the sidecar .jpg. This reclaims those."""
    removed = []
    for f in DOWNLOADS_DIR.iterdir():
        if not f.is_file() or f.suffix != ".jpg":
            continue
        match = CONTACT_SHEET_STEM_RE.match(f.stem)
        if not match:
            continue
        source = f.with_name(match.group("source_stem") + ".mp4")
        if source.exists():
            continue
        try:
            f.unlink()
            removed.append(f.name)
        except OSError as exc:
            logger.warning("Failed to remove orphaned contact sheet %s: %s", f.name, exc)
    if removed:
        logger.info("Removed %d orphaned contact sheet(s): %s", len(removed), removed)
    return removed


async def _ensure_contact_sheet(file_path: Path) -> bool:
    """Generate the contact sheet for `file_path` if it doesn't exist yet.
    Safe to call concurrently: a global lock plus a double-checked existence
    test means only one generation ever runs at a time for a given file."""
    sheet_path = _contact_sheet_path(file_path)
    if sheet_path.exists():
        return True

    async with _CONTACT_SHEET_LOCK:
        if sheet_path.exists():
            return True
        return await asyncio.to_thread(
            generate_contact_sheet,
            str(file_path),
            str(sheet_path),
            CONTACT_SHEET_INTERVAL_SECONDS,
            CONTACT_SHEET_TILE_WIDTH,
            CONTACT_SHEET_COLUMNS,
        )


async def _auto_generate_contact_sheet(file_path: Path) -> None:
    key = file_path.name
    if key in _contact_sheet_pending:
        return
    _contact_sheet_pending.add(key)
    try:
        if not await _ensure_contact_sheet(file_path):
            logger.warning("Auto contact-sheet generation failed for %s", key)
    finally:
        _contact_sheet_pending.discard(key)


def _schedule_contact_sheet(file_path: Path) -> None:
    """Fire-and-forget an automatic contact-sheet generation, if enabled."""
    if not CONTACT_SHEET_AUTO_GENERATE:
        return
    asyncio.create_task(_auto_generate_contact_sheet(file_path))


@app.get("/api/downloads/contact-sheet/{filename}")
async def get_contact_sheet(filename: str):
    """Return a per-minute contact-sheet thumbnail for a completed recording,
    generating and caching it next to the source file on first request."""
    file_path = _safe_downloads_path(filename)
    if not file_path.exists() or not _is_completed_media_file(file_path):
        raise HTTPException(status_code=404, detail="File not found")

    if not await _ensure_contact_sheet(file_path):
        raise HTTPException(status_code=502, detail="Failed to generate contact sheet")

    return FileResponse(path=str(_contact_sheet_path(file_path)), media_type="image/jpeg")


@app.post("/api/downloads/contact-sheets/generate-all")
async def generate_all_contact_sheets(request: Request):
    """Manually (re)run contact-sheet generation for every completed file that
    doesn't have one yet -- the same backfill `CONTACT_SHEET_AUTO_GENERATE`
    does automatically, callable on demand regardless of that setting."""
    _require_trusted_origin(request)
    media_files = [f for f in DOWNLOADS_DIR.iterdir() if _is_completed_media_file(f)]
    missing = [f for f in media_files if not _contact_sheet_path(f).exists()]
    results = await asyncio.gather(*(_ensure_contact_sheet(f) for f in missing))
    generated = [f.name for f, ok in zip(missing, results) if ok]
    failed = [f.name for f, ok in zip(missing, results) if not ok]
    return {"generated": generated, "failed": failed, "already_had_one": len(media_files) - len(missing)}


_duration_cache: dict[str, Optional[float]] = {}


async def _get_duration_seconds(file_path: Path) -> Optional[float]:
    """Probe a completed file's duration, cached by filename (files are immutable once done)."""
    key = file_path.name
    if key not in _duration_cache:
        _duration_cache[key] = await asyncio.to_thread(_probe_duration, str(file_path))
    return _duration_cache[key]


def _prune_duration_cache(existing_names: set[str]) -> None:
    """Drop cache entries for files that no longer exist (e.g. removed
    outside the API), so the dict doesn't grow without bound over time."""
    for stale in [name for name in _duration_cache if name not in existing_names]:
        _duration_cache.pop(stale, None)


@app.get("/api/downloads/list")
async def list_downloaded_files():
    """List all downloaded files."""
    media_files = [f for f in DOWNLOADS_DIR.iterdir() if _is_completed_media_file(f)]
    _prune_duration_cache({f.name for f in media_files})
    durations = await asyncio.gather(*(_get_duration_seconds(f) for f in media_files))

    files = []
    for f, duration in zip(media_files, durations):
        stem = f.stem
        username = _username_from_completed_stem(stem)
        st = f.stat()
        has_contact_sheet = _contact_sheet_path(f).exists()
        if not has_contact_sheet:
            _schedule_contact_sheet(f)
        files.append(
            {
                "filename": f.name,
                "username": username,
                "size": st.st_size,
                "size_mb": round(st.st_size / (1024 * 1024), 2),
                "format": f.suffix.lstrip("."),
                "has_contact_sheet": has_contact_sheet,
                "duration_seconds": duration,
            }
        )
    return sorted(files, key=lambda x: x["filename"])


@app.delete("/api/downloads/{filename}")
async def delete_file(request: Request, filename: str):
    """Delete a downloaded file."""
    _require_trusted_origin(request)
    file_path = _safe_downloads_path(filename)
    if not file_path.exists() or not _is_completed_media_file(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    file_path.unlink()
    _contact_sheet_path(file_path).unlink(missing_ok=True)
    _duration_cache.pop(filename, None)
    return {"status": "deleted", "filename": filename}


@app.post("/api/downloads/cleanup-orphans")
async def cleanup_orphans(request: Request):
    """Manually remove leftover `_video.mp4`/`_audio.mp4` temp tracks from
    downloads that never finalized (crash, force-cancel, ungraceful
    shutdown), plus contact sheets whose source recording is gone (deleted
    outside the API, e.g. by an external tool watching `downloads/`). Also
    runs automatically once at server startup."""
    _require_trusted_origin(request)
    removed_temp_files = _cleanup_orphaned_temp_files()
    removed_contact_sheets = _cleanup_orphaned_contact_sheets()
    return {
        "removed_temp_files": removed_temp_files,
        "removed_contact_sheets": removed_contact_sheets,
    }


# ─── Tracked Streamers ────────────────────────────────────


_THUMB_URL = "https://thumb.live.mmcdn.com/riw/{username}.jpg"
_THUMB_TTL_SECONDS = 30
_thumb_cache: dict[str, tuple[float, bytes, str]] = {}
_thumb_lock = asyncio.Lock()


def _prune_thumb_cache(valid_usernames: set[str]) -> None:
    """Drop cached thumbnails for usernames no longer tracked, so the cache
    doesn't grow forever across the lifetime of a long-running server."""
    for stale in [name for name in _thumb_cache if name not in valid_usernames]:
        _thumb_cache.pop(stale, None)


@app.get("/api/tracked")
async def list_tracked():
    rows = await tracker.list_all()
    active = manager.get_status()
    if isinstance(active, dict) and isinstance(active.get("downloads"), list):
        active_users = {item.get("username") for item in active["downloads"] if item.get("active")}
    elif isinstance(active, list):
        active_users = {item.get("username") for item in active if item.get("active")}
    else:
        active_users = set()
    for row in rows:
        row["downloading"] = row["username"] in active_users
    return {"tracked": rows, "polled_every_seconds": POLL_INTERVAL_SECONDS}


@app.post("/api/tracked")
async def add_tracked(request: Request, username: str):
    """Track a streamer without downloading, e.g. to enable auto-record while offline."""
    _require_trusted_origin(request)
    username = _validate_username(username)
    added = await tracker.add(username)
    if not added:
        raise HTTPException(status_code=409, detail="Username already tracked")

    try:
        async with _status_client() as client:
            await _check_tracked_status(client, username)
    except Exception as exc:
        logger.debug("initial status fetch failed for %s: %s", username, exc)

    return {"status": "added", "username": username}


@app.delete("/api/tracked/{username}")
async def delete_tracked(request: Request, username: str):
    _require_trusted_origin(request)
    username = _validate_username(username)
    removed = await tracker.delete(username)
    if not removed:
        raise HTTPException(status_code=404, detail="Username not tracked")
    _thumb_cache.pop(username, None)
    return {"status": "deleted", "username": username}


@app.post("/api/tracked/{username}/refresh")
async def refresh_tracked(request: Request, username: str):
    """Manually re-check one streamer's live status right now instead of
    waiting for the next background poll (up to `POLL_INTERVAL_SECONDS`)."""
    _require_trusted_origin(request)
    username = _validate_username(username)
    if username not in set(await tracker.list_usernames()):
        raise HTTPException(status_code=404, detail="Username not tracked")
    async with _status_client() as client:
        status = await _check_tracked_status(client, username)
    return {"username": username, "status": status}


@app.post("/api/tracked/refresh-all")
async def refresh_all_tracked(request: Request):
    """Manually run the same status check the background poller performs
    every `POLL_INTERVAL_SECONDS`, immediately, for every tracked username."""
    _require_trusted_origin(request)
    count = await _refresh_all_tracked_status()
    return {"status": "refreshed", "count": count}


@app.patch("/api/tracked/{username}/auto-download")
async def set_auto_download(request: Request, username: str, enabled: bool):
    _require_trusted_origin(request)
    username = _validate_username(username)
    updated = await tracker.set_auto_download(username, enabled)
    if not updated:
        raise HTTPException(status_code=404, detail="Username not tracked")
    return {"username": username, "auto_download": enabled}


@app.get("/api/thumbnail/{username}")
async def get_thumbnail(username: str):
    username = _validate_username(username)
    cached = _thumb_cache.get(username)
    if cached and time.time() - cached[0] < _THUMB_TTL_SECONDS:
        return Response(content=cached[1], media_type=cached[2])

    async with _thumb_lock:
        cached = _thumb_cache.get(username)
        if cached and time.time() - cached[0] < _THUMB_TTL_SECONDS:
            return Response(content=cached[1], media_type=cached[2])

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0),
                follow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://chaturbate.com/",
                },
                **proxy_kwargs(),
            ) as client:
                resp = await client.get(_THUMB_URL.format(username=username))
        except Exception as exc:
            logger.debug("thumbnail fetch failed for %s: %s", username, exc)
            raise HTTPException(status_code=502, detail="Thumbnail unavailable")

        if resp.status_code != 200 or not resp.content:
            raise HTTPException(status_code=404, detail="Thumbnail not found")

        media_type = resp.headers.get("content-type", "image/jpeg")
        _thumb_cache[username] = (time.time(), resp.content, media_type)
        return Response(content=resp.content, media_type=media_type)


# ─── Debug Endpoints ──────────────────────────────────────


@contextmanager
def _capture_downloader_logs():
    """Temporarily attach a handler to the 'downloader' logger and yield its buffer."""
    import io

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    downloader_logger = logging.getLogger("downloader")
    downloader_logger.addHandler(handler)
    try:
        yield buf
    finally:
        downloader_logger.removeHandler(handler)


@app.get("/api/debug/extract/{username}")
async def debug_extract(username: str):
    """Debug: test HLS URL extraction for a room without downloading."""
    from downloader.extractor import extract_hls_url

    username = _validate_username(username)

    with _capture_downloader_logs() as log_capture:
        url = await extract_hls_url(username)
        logs = _redact_text_urls(log_capture.getvalue())

    return {
        "username": username,
        "hls_url": _redact_url(url),
        "found": url is not None,
        "logs": logs,
    }


@app.get("/api/debug/playlist/{username}")
async def debug_playlist(username: str):
    """Debug: fetch and parse the HLS playlist, show its full contents."""
    import m3u8

    username = _validate_username(username)

    with _capture_downloader_logs() as log_capture:
        from downloader.extractor import extract_hls_url, DEFAULT_HEADERS

        hls_url = await extract_hls_url(username)

        if not hls_url:
            return {
                "username": username,
                "error": "No HLS URL found",
                "logs": _redact_text_urls(log_capture.getvalue()),
            }

        async with httpx.AsyncClient(
            headers=DEFAULT_HEADERS,
            timeout=httpx.Timeout(20.0),
            follow_redirects=True,
            **proxy_kwargs(),
        ) as client:
            resp = await client.get(hls_url)
            master_body = resp.text
            master_status = resp.status_code
            master_playlist = m3u8.loads(master_body)
            is_variant = master_playlist.is_variant

            result = {
                "username": username,
                "hls_url": _redact_url(hls_url),
                "master_status": master_status,
                "master_is_variant": is_variant,
                "master_content": _redact_text_urls(master_body[:3000]),
                "master_content_length": len(master_body),
            }

            if is_variant and master_playlist.playlists:
                variants = []
                for p in master_playlist.playlists:
                    var_url = _abs_url(hls_url, p.uri)
                    variants.append(
                        {
                            "uri": _redact_url(var_url),
                            "bandwidth": p.stream_info.bandwidth,
                            "resolution": str(p.stream_info.resolution)
                            if p.stream_info.resolution
                            else None,
                        }
                    )
                result["variants"] = variants

                best = _select_best_variant(master_playlist.playlists)
                best_url = _abs_url(hls_url, best.uri)

                result["selected_variant_url"] = _redact_url(best_url)

                resp2 = await client.get(best_url)
                variant_body = resp2.text
                variant_playlist = m3u8.loads(variant_body)

                result["variant_status"] = resp2.status_code
                result["variant_content"] = _redact_text_urls(variant_body[:5000])
                result["variant_content_length"] = len(variant_body)
                result["variant_segment_count"] = len(variant_playlist.segments)
                result["variant_has_segment_map"] = bool(variant_playlist.segment_map)
                result["variant_is_variant"] = variant_playlist.is_variant
                result["variant_target_duration"] = variant_playlist.target_duration

                segs = []
                for s in variant_playlist.segments[:5]:
                    seg_url = _abs_url(best_url, s.uri) if s.uri else ""
                    segs.append({"uri": _redact_url(seg_url), "duration": s.duration})
                result["first_segments"] = segs

            elif master_playlist.segments:
                result["direct_segment_count"] = len(master_playlist.segments)
                segs = []
                for s in master_playlist.segments[:5]:
                    seg_url = _abs_url(hls_url, s.uri) if s.uri else ""
                    segs.append({"uri": _redact_url(seg_url), "duration": s.duration})
                result["first_segments"] = segs
            else:
                result["no_segments_found"] = True
                result["raw_parse_check"] = "#EXTINF" in master_body

        result["logs"] = _redact_text_urls(log_capture.getvalue())
        return result


def main():
    import uvicorn

    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
