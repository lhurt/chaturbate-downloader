"""Debug-only endpoints for inspecting HLS URL extraction and playlist
content without starting a download.

`_validate_username` and `_capture_downloader_logs` are app.py-owned state
referenced as `app.<name>` (see routers/downloads.py's docstring); everything
else here (redaction, playlist helpers) is imported directly from its own
module since app.py doesn't own it and nothing patches it through `app`.
"""

from __future__ import annotations

import httpx
import m3u8
from fastapi import APIRouter

from downloader.extractor import DEFAULT_HEADERS, extract_hls_url
from downloader.hls import _abs_url, _select_best_variant
from downloader.http_client import proxy_kwargs
from downloader.redact import _redact_text_urls, _redact_url

import app

router = APIRouter(tags=["debug"])


@router.get("/api/debug/extract/{username}")
async def debug_extract(username: str):
    """Debug: test HLS URL extraction for a room without downloading."""
    username = app._validate_username(username)

    with app._capture_downloader_logs() as log_capture:
        url = await extract_hls_url(username)
        logs = _redact_text_urls(log_capture.getvalue())

    return {
        "username": username,
        "hls_url": _redact_url(url),
        "found": url is not None,
        "logs": logs,
    }


@router.get("/api/debug/playlist/{username}")
async def debug_playlist(username: str):
    """Debug: fetch and parse the HLS playlist, show its full contents."""
    username = app._validate_username(username)

    with app._capture_downloader_logs() as log_capture:
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
