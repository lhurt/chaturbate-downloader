"""
FastAPI web server for the Chaturbate stream downloader.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from downloader import DownloadManager
from downloader.auto_download import AutoDownloadScheduler
from downloader.cleanup import cleanup_orphaned_contact_sheets, cleanup_orphaned_temp_files
from downloader.converter import _probe_duration, generate_contact_sheet
from downloader.extractor import DEFAULT_HEADERS, fetch_room_status
from downloader.http_client import proxy_kwargs
# Kept importable as app._redact_text_urls/_redact_url for tests, even though
# app.py's own code no longer calls them (routers/debug.py imports its own
# copy directly from downloader.redact).
from downloader.redact import _redact_text_urls, _redact_url
from downloader.tracker import Tracker

# When run directly (`python app.py`), this module loads as "__main__", but
# routers/*.py do `import app` to reach app-owned state. Alias "app" to the
# already-executing module so that import resolves here instead of
# re-executing this file as a second, separate module (circular import).
sys.modules.setdefault("app", sys.modules[__name__])

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
STATUS_POLL_DELAY_SECONDS = float(os.getenv("STATUS_POLL_DELAY_SECONDS", "1.0"))

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{1,50}$")
COMPLETED_STEM_RE = re.compile(
    r"^(?P<username>.+)_(?P<timestamp>\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})$"
)

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
    """Sweep DOWNLOADS_DIR for orphaned temp tracks (see downloader.cleanup)."""
    return cleanup_orphaned_temp_files(DOWNLOADS_DIR, manager.active_usernames())


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
            for i, username in enumerate(usernames):
                if i:
                    await asyncio.sleep(STATUS_POLL_DELAY_SECONDS)
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


# ─── Shared download/contact-sheet/tracked state ───────────
# The route handlers themselves live in routers/*.py (included below);
# this section holds what they share: the manager/tracker singletons,
# config, caches, and helpers.


_CONTACT_SHEET_LOCK = asyncio.Lock()
_contact_sheet_pending: set[str] = set()


def _contact_sheet_path(file_path: Path) -> Path:
    return file_path.with_name(file_path.stem + "_contactsheet.jpg")


def _cleanup_orphaned_contact_sheets() -> list[str]:
    """Sweep DOWNLOADS_DIR for orphaned contact sheets (see downloader.cleanup)."""
    return cleanup_orphaned_contact_sheets(DOWNLOADS_DIR)


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


# ─── Routers ────────────────────────────────────────────────
# Imported last (not at module top) since routers/*.py each `import app` to
# reach the shared state/helpers above -- app.py isn't done initializing yet
# at that point, which is fine as long as they only touch `app.<name>` from
# inside a request handler, never at their own module level.

from routers.debug import router as debug_router
from routers.downloads import (
    cleanup_orphans,
    delete_file,
    download_file,
    download_file_by_name,
    generate_all_contact_sheets,
    get_all_status,
    get_contact_sheet,
    get_status,
    list_downloaded_files,
    router as downloads_router,
    start_download,
    stop_all,
    stop_download,
)
from routers.tracked import (
    add_tracked,
    delete_tracked,
    get_thumbnail,
    list_tracked,
    refresh_all_tracked,
    refresh_tracked,
    router as tracked_router,
    set_auto_download,
)

app.include_router(downloads_router)
app.include_router(tracked_router)
app.include_router(debug_router)


def main():
    import uvicorn

    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
