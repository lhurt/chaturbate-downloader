"""Download lifecycle, completed-file listing, and contact-sheet endpoints.

Handlers reference shared state (manager, tracker, DOWNLOADS_DIR, caches,
config, helpers) as `app.<name>` rather than importing names directly, since
`app.py` includes this router while it is still finishing its own module
initialization (a circular import) and because tests monkeypatch that shared
state on the `app` module directly (e.g. `monkeypatch.setattr(webapp,
"manager", FakeManager())`) -- looking it up through the `app` module object
at call time, instead of binding a local copy at import time, is what makes
those patches take effect here too.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

import app

router = APIRouter(tags=["downloads"])


@router.post("/api/download/start")
async def start_download(
    request: Request,
    username: str,
    output_format: str = "mp4",
    max_duration: Optional[int] = None,
):
    """Start downloading a stream."""
    app._require_trusted_origin(request)
    username = app._validate_username(username)
    if output_format != "mp4":
        raise HTTPException(status_code=400, detail="Format must be 'mp4'")
    if max_duration is not None and max_duration <= 0:
        raise HTTPException(status_code=400, detail="max_duration must be positive")
    # Convert minutes (from UI) to seconds for the backend
    duration_seconds = max_duration * 60 if max_duration else None
    result = await app.manager.start_download(
        username=username,
        max_duration=duration_seconds,
    )
    if "error" in result:
        raise HTTPException(status_code=409, detail=result["error"])
    await app.tracker.upsert_download(username)
    await app.tracker.update_status(username, "public")
    return result


@router.post("/api/download/stop/{username}")
async def stop_download(request: Request, username: str):
    """Stop a specific download."""
    app._require_trusted_origin(request)
    username = app._validate_username(username)
    result = await app.manager.stop_download(username)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.post("/api/download/stop-all")
async def stop_all(request: Request):
    """Stop all active downloads."""
    app._require_trusted_origin(request)
    return await app.manager.stop_all()


@router.get("/api/download/status")
async def get_all_status():
    """Get status of all downloads."""
    return app.manager.get_status()


@router.get("/api/download/status/{username}")
async def get_status(username: str):
    """Get status of a specific download."""
    username = app._validate_username(username)
    result = app.manager.get_download(username)
    if result is None:
        raise HTTPException(status_code=404, detail="Download not found")
    return result


@router.get("/api/download/file/{username}")
async def download_file(username: str):
    """Download the completed file."""
    username = app._validate_username(username)

    # Try download status first
    status = app.manager.get_download(username)
    if status and status.get("output_path"):
        file_path = Path(status["output_path"]).resolve()
        if file_path.is_relative_to(app.DOWNLOADS_DIR) and app._is_completed_media_file(file_path):
            return FileResponse(
                path=str(file_path),
                media_type="video/mp4",
                filename=file_path.name,
            )

    # Fallback: scan downloads directory for any file starting with username_
    for f in sorted(app.DOWNLOADS_DIR.iterdir(), reverse=True):
        if app._is_completed_media_file(f) and f.name.startswith(f"{username}_"):
            return FileResponse(
                path=str(f),
                media_type="video/mp4",
                filename=f.name,
            )

    raise HTTPException(status_code=404, detail="File not found")


@router.get("/api/downloads/file/{filename}")
async def download_file_by_name(filename: str):
    """Download an exact completed filename."""
    file_path = app._safe_downloads_path(filename)
    if not file_path.exists() or not app._is_completed_media_file(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        path=str(file_path),
        media_type="video/mp4",
        filename=file_path.name,
    )


@router.get("/api/downloads/contact-sheet/{filename}")
async def get_contact_sheet(filename: str):
    """Return a per-minute contact-sheet thumbnail for a completed recording,
    generating and caching it next to the source file on first request."""
    file_path = app._safe_downloads_path(filename)
    if not file_path.exists() or not app._is_completed_media_file(file_path):
        raise HTTPException(status_code=404, detail="File not found")

    if not await app._ensure_contact_sheet(file_path):
        raise HTTPException(status_code=502, detail="Failed to generate contact sheet")

    return FileResponse(path=str(app._contact_sheet_path(file_path)), media_type="image/jpeg")


@router.post("/api/downloads/contact-sheets/generate-all")
async def generate_all_contact_sheets(request: Request):
    """Manually (re)run contact-sheet generation for every completed file that
    doesn't have one yet -- the same backfill `CONTACT_SHEET_AUTO_GENERATE`
    does automatically, callable on demand regardless of that setting."""
    app._require_trusted_origin(request)
    media_files = [f for f in app.DOWNLOADS_DIR.iterdir() if app._is_completed_media_file(f)]
    missing = [f for f in media_files if not app._contact_sheet_path(f).exists()]
    results = await asyncio.gather(*(app._ensure_contact_sheet(f) for f in missing))
    generated = [f.name for f, ok in zip(missing, results) if ok]
    failed = [f.name for f, ok in zip(missing, results) if not ok]
    return {"generated": generated, "failed": failed, "already_had_one": len(media_files) - len(missing)}


@router.get("/api/downloads/list")
async def list_downloaded_files():
    """List all downloaded files."""
    media_files = [f for f in app.DOWNLOADS_DIR.iterdir() if app._is_completed_media_file(f)]
    app._prune_duration_cache({f.name for f in media_files})
    durations = await asyncio.gather(*(app._get_duration_seconds(f) for f in media_files))

    files = []
    for f, duration in zip(media_files, durations):
        stem = f.stem
        username = app._username_from_completed_stem(stem)
        st = f.stat()
        has_contact_sheet = app._contact_sheet_path(f).exists()
        if not has_contact_sheet:
            app._schedule_contact_sheet(f)
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


@router.delete("/api/downloads/{filename}")
async def delete_file(request: Request, filename: str):
    """Delete a downloaded file."""
    app._require_trusted_origin(request)
    file_path = app._safe_downloads_path(filename)
    if not file_path.exists() or not app._is_completed_media_file(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    file_path.unlink()
    app._contact_sheet_path(file_path).unlink(missing_ok=True)
    app._duration_cache.pop(filename, None)
    return {"status": "deleted", "filename": filename}


@router.post("/api/downloads/cleanup-orphans")
async def cleanup_orphans(request: Request):
    """Manually remove leftover `_video.mp4`/`_audio.mp4` temp tracks from
    downloads that never finalized (crash, force-cancel, ungraceful
    shutdown), plus contact sheets whose source recording is gone (deleted
    outside the API, e.g. by an external tool watching `downloads/`). Also
    runs automatically once at server startup."""
    app._require_trusted_origin(request)
    removed_temp_files = app._cleanup_orphaned_temp_files()
    removed_contact_sheets = app._cleanup_orphaned_contact_sheets()
    return {
        "removed_temp_files": removed_temp_files,
        "removed_contact_sheets": removed_contact_sheets,
    }
