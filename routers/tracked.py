"""Tracked-streamer dashboard endpoints (list/add/delete/refresh/auto-download)
plus the room-thumbnail proxy.

See routers/downloads.py's module docstring for why these reference shared
state as `app.<name>` instead of importing it directly.
"""

from __future__ import annotations

import time

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from downloader.http_client import proxy_kwargs

import app

router = APIRouter(tags=["tracked"])


@router.get("/api/tracked")
async def list_tracked():
    rows = await app.tracker.list_all()
    active = app.manager.get_status()
    if isinstance(active, dict) and isinstance(active.get("downloads"), list):
        active_users = {item.get("username") for item in active["downloads"] if item.get("active")}
    elif isinstance(active, list):
        active_users = {item.get("username") for item in active if item.get("active")}
    else:
        active_users = set()
    for row in rows:
        row["downloading"] = row["username"] in active_users
    return {"tracked": rows, "polled_every_seconds": app.POLL_INTERVAL_SECONDS}


@router.post("/api/tracked")
async def add_tracked(request: Request, username: str):
    """Track a streamer without downloading, e.g. to enable auto-record while offline."""
    app._require_trusted_origin(request)
    username = app._validate_username(username)
    added = await app.tracker.add(username)
    if not added:
        raise HTTPException(status_code=409, detail="Username already tracked")

    try:
        async with app._status_client() as client:
            await app._check_tracked_status(client, username)
    except Exception as exc:
        app.logger.debug("initial status fetch failed for %s: %s", username, exc)

    return {"status": "added", "username": username}


@router.delete("/api/tracked/{username}")
async def delete_tracked(request: Request, username: str):
    app._require_trusted_origin(request)
    username = app._validate_username(username)
    removed = await app.tracker.delete(username)
    if not removed:
        raise HTTPException(status_code=404, detail="Username not tracked")
    app._thumb_cache.pop(username, None)
    return {"status": "deleted", "username": username}


@router.post("/api/tracked/{username}/refresh")
async def refresh_tracked(request: Request, username: str):
    """Manually re-check one streamer's live status right now instead of
    waiting for the next background poll (up to `POLL_INTERVAL_SECONDS`)."""
    app._require_trusted_origin(request)
    username = app._validate_username(username)
    if username not in set(await app.tracker.list_usernames()):
        raise HTTPException(status_code=404, detail="Username not tracked")
    async with app._status_client() as client:
        status = await app._check_tracked_status(client, username)
    return {"username": username, "status": status}


@router.post("/api/tracked/refresh-all")
async def refresh_all_tracked(request: Request):
    """Manually run the same status check the background poller performs
    every `POLL_INTERVAL_SECONDS`, immediately, for every tracked username."""
    app._require_trusted_origin(request)
    count = await app._refresh_all_tracked_status()
    return {"status": "refreshed", "count": count}


@router.patch("/api/tracked/{username}/auto-download")
async def set_auto_download(request: Request, username: str, enabled: bool):
    app._require_trusted_origin(request)
    username = app._validate_username(username)
    updated = await app.tracker.set_auto_download(username, enabled)
    if not updated:
        raise HTTPException(status_code=404, detail="Username not tracked")
    return {"username": username, "auto_download": enabled}


@router.get("/api/thumbnail/{username}")
async def get_thumbnail(username: str):
    username = app._validate_username(username)
    cached = app._thumb_cache.get(username)
    if cached and time.time() - cached[0] < app._THUMB_TTL_SECONDS:
        return Response(content=cached[1], media_type=cached[2])

    async with app._thumb_lock:
        cached = app._thumb_cache.get(username)
        if cached and time.time() - cached[0] < app._THUMB_TTL_SECONDS:
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
                resp = await client.get(app._THUMB_URL.format(username=username))
        except Exception as exc:
            app.logger.debug("thumbnail fetch failed for %s: %s", username, exc)
            raise HTTPException(status_code=502, detail="Thumbnail unavailable")

        if resp.status_code != 200 or not resp.content:
            raise HTTPException(status_code=404, detail="Thumbnail not found")

        media_type = resp.headers.get("content-type", "image/jpeg")
        app._thumb_cache[username] = (time.time(), resp.content, media_type)
        return Response(content=resp.content, media_type=media_type)
