"""Direct tests for downloader.extractor's real HTTP-facing logic.

Every existing test elsewhere in the suite that touches "extraction"
monkeypatches extract_hls_url/fetch_room_status wholesale, so the actual
4-strategy fallback chain, the dossier-JSON/regex-HTML parsing, and the
CSRF edge-ajax request building had zero coverage. These use
httpx.MockTransport to simulate Chaturbate's responses without any real
network I/O.
"""

import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from downloader.extractor import (
    _build_edge_ajax_request,
    _extract_dossier_json,
    _find_m3u8_in_html,
    _strategy_chatvideocontext,
    _strategy_dossier,
    _strategy_edge_ajax,
    _strategy_regex_html,
    extract_hls_url,
    fetch_room_status,
)


_RealAsyncClient = httpx.AsyncClient


def _client(handler: callable) -> httpx.AsyncClient:
    return _RealAsyncClient(transport=httpx.MockTransport(handler))


def _patch_async_client(monkeypatch, handler: callable) -> None:
    """extract_hls_url builds its own httpx.AsyncClient internally (with no
    way to inject a transport from outside), so route every client it
    constructs through a mock transport instead."""
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **k: _RealAsyncClient(transport=httpx.MockTransport(handler)),
    )


# ─── _extract_dossier_json / _find_m3u8_in_html (pure helpers) ─────


def test_extract_dossier_json_parses_json_delimited_by_single_quotes():
    # Real pages wrap the JSON blob in single quotes so its own double quotes
    # don't need escaping: var initialRoomDossier = '{"hls_source": "..."}';
    html = (
        "<script>window.initialRoomDossier = "
        "'{\"hls_source\": \"https://edge.example/live.m3u8?t=1\"}'"
        ";</script>"
    )
    data = _extract_dossier_json(html)
    assert data == {"hls_source": "https://edge.example/live.m3u8?t=1"}


def test_extract_dossier_json_handles_escaped_forward_slashes():
    # Django's |escapejs and similar template filters escape "/" as "\/".
    html = (
        "<script>window.initialRoomDossier = "
        '\'{"hls_source": "https:\\/\\/edge.example\\/live.m3u8?t=1"}\''
        ";</script>"
    )
    data = _extract_dossier_json(html)
    assert data == {"hls_source": "https://edge.example/live.m3u8?t=1"}


def test_extract_dossier_json_returns_none_without_a_match():
    assert _extract_dossier_json("<html>nothing here</html>") is None


def test_extract_dossier_json_returns_none_on_invalid_json():
    html = 'initialRoomDossier = "not actually json";'
    assert _extract_dossier_json(html) is None


def test_find_m3u8_in_html_prefers_playlist_over_chunklist():
    html = (
        '<a href="https://cdn.example/chunklist_b1.m3u8">x</a>'
        '<a href="https://cdn.example/playlist.m3u8">y</a>'
    )
    assert _find_m3u8_in_html(html) == "https://cdn.example/playlist.m3u8"


def test_find_m3u8_in_html_falls_back_to_first_match():
    html = '<a href="https://cdn.example/chunklist_b1.m3u8">x</a>'
    assert _find_m3u8_in_html(html) == "https://cdn.example/chunklist_b1.m3u8"


def test_find_m3u8_in_html_returns_none_when_absent():
    assert _find_m3u8_in_html("<html>offline</html>") is None


def test_build_edge_ajax_request_shape():
    headers, cookies, data = _build_edge_ajax_request("alice")
    assert headers["X-CSRFToken"] == cookies["csrftoken"]
    assert headers["Referer"] == "https://chaturbate.com/alice/"
    assert "room_slug=alice" in data


# ─── _strategy_chatvideocontext ────────────────────────────────────


def test_strategy_chatvideocontext_returns_url_when_present():
    def handler(request):
        return httpx.Response(200, json={"hls_source": "https://edge.example/a.m3u8"})

    async def scenario():
        async with _client(handler) as client:
            return await _strategy_chatvideocontext(client, "alice")

    assert asyncio.run(scenario()) == "https://edge.example/a.m3u8"


def test_strategy_chatvideocontext_returns_none_when_offline():
    def handler(request):
        return httpx.Response(200, json={"room_status": "offline"})

    async def scenario():
        async with _client(handler) as client:
            return await _strategy_chatvideocontext(client, "alice")

    assert asyncio.run(scenario()) is None


def test_strategy_chatvideocontext_returns_none_on_non_200():
    def handler(request):
        return httpx.Response(429, text="rate limited")

    async def scenario():
        async with _client(handler) as client:
            return await _strategy_chatvideocontext(client, "alice")

    assert asyncio.run(scenario()) is None


def test_strategy_chatvideocontext_returns_none_on_network_error():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    async def scenario():
        async with _client(handler) as client:
            return await _strategy_chatvideocontext(client, "alice")

    assert asyncio.run(scenario()) is None


# ─── _strategy_dossier ──────────────────────────────────────────────


def test_strategy_dossier_returns_url_from_page_html():
    html = (
        "<script>window.initialRoomDossier = "
        "'{\"hls_source\": \"https://edge.example/dossier.m3u8\"}'"
        ";</script>"
    )

    def handler(request):
        return httpx.Response(200, text=html)

    async def scenario():
        async with _client(handler) as client:
            return await _strategy_dossier(client, "alice")

    assert asyncio.run(scenario()) == "https://edge.example/dossier.m3u8"


def test_strategy_dossier_returns_none_without_dossier():
    def handler(request):
        return httpx.Response(200, text="<html>no dossier here</html>")

    async def scenario():
        async with _client(handler) as client:
            return await _strategy_dossier(client, "alice")

    assert asyncio.run(scenario()) is None


def test_strategy_dossier_returns_none_on_non_200():
    def handler(request):
        return httpx.Response(404)

    async def scenario():
        async with _client(handler) as client:
            return await _strategy_dossier(client, "alice")

    assert asyncio.run(scenario()) is None


# ─── _strategy_edge_ajax ────────────────────────────────────────────


def test_strategy_edge_ajax_returns_url_on_success():
    def handler(request):
        return httpx.Response(
            200, json={"success": True, "url": "https://edge.example/ajax.m3u8"}
        )

    async def scenario():
        async with _client(handler) as client:
            return await _strategy_edge_ajax(client, "alice")

    assert asyncio.run(scenario()) == "https://edge.example/ajax.m3u8"


def test_strategy_edge_ajax_returns_none_when_not_successful():
    def handler(request):
        return httpx.Response(200, json={"success": False, "room_status": "offline"})

    async def scenario():
        async with _client(handler) as client:
            return await _strategy_edge_ajax(client, "alice")

    assert asyncio.run(scenario()) is None


def test_strategy_edge_ajax_returns_none_on_non_200():
    def handler(request):
        return httpx.Response(500)

    async def scenario():
        async with _client(handler) as client:
            return await _strategy_edge_ajax(client, "alice")

    assert asyncio.run(scenario()) is None


# ─── _strategy_regex_html ───────────────────────────────────────────


def test_strategy_regex_html_returns_url_when_present():
    def handler(request):
        return httpx.Response(
            200, text='<a href="https://cdn.example/playlist.m3u8">watch</a>'
        )

    async def scenario():
        async with _client(handler) as client:
            return await _strategy_regex_html(client, "alice")

    assert asyncio.run(scenario()) == "https://cdn.example/playlist.m3u8"


def test_strategy_regex_html_returns_none_when_offline():
    def handler(request):
        return httpx.Response(200, text="<html>alice is currently offline</html>")

    async def scenario():
        async with _client(handler) as client:
            return await _strategy_regex_html(client, "alice")

    assert asyncio.run(scenario()) is None


# ─── fetch_room_status ──────────────────────────────────────────────


def test_fetch_room_status_uses_chatvideocontext_when_available():
    def handler(request):
        assert "chatvideocontext" in str(request.url)
        return httpx.Response(200, json={"room_status": "public"})

    async def scenario():
        async with _client(handler) as client:
            return await fetch_room_status(client, "alice")

    assert asyncio.run(scenario()) == "public"


def test_fetch_room_status_returns_deleted_on_404():
    def handler(request):
        return httpx.Response(404)

    async def scenario():
        async with _client(handler) as client:
            return await fetch_room_status(client, "alice")

    assert asyncio.run(scenario()) == "deleted"


def test_fetch_room_status_falls_back_to_edge_ajax():
    calls = []

    def handler(request):
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json={})  # no room_status -> fall through
        return httpx.Response(200, json={"room_status": "away"})

    async def scenario():
        async with _client(handler) as client:
            return await fetch_room_status(client, "alice")

    assert asyncio.run(scenario()) == "away"
    assert calls == ["GET", "POST"]


# ─── extract_hls_url (full fallback chain) ──────────────────────────


def test_extract_hls_url_stops_at_first_successful_strategy(monkeypatch):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if "chatvideocontext" in str(request.url):
            return httpx.Response(200, json={"hls_source": "https://edge.example/x.m3u8"})
        raise AssertionError("should not fall through past the first successful strategy")

    _patch_async_client(monkeypatch, handler)

    assert asyncio.run(extract_hls_url("alice")) == "https://edge.example/x.m3u8"
    assert calls == ["https://chaturbate.com/api/chatvideocontext/alice/"]


def test_extract_hls_url_returns_none_when_room_is_offline(monkeypatch):
    def handler(request):
        # Every strategy's HTTP call reports "not found" in its own way.
        if "chatvideocontext" in str(request.url) or "get_edge_hls_url_ajax" in str(request.url):
            return httpx.Response(200, json={"room_status": "offline"})
        return httpx.Response(200, text="<html>alice is currently offline</html>")

    _patch_async_client(monkeypatch, handler)

    assert asyncio.run(extract_hls_url("alice")) is None


def test_extract_hls_url_uses_dossier_when_chatvideocontext_has_no_source(monkeypatch):
    html = (
        "<script>window.initialRoomDossier = "
        "'{\"hls_source\": \"https://edge.example/dossier.m3u8\"}'"
        ";</script>"
    )

    def handler(request):
        url = str(request.url)
        if "chatvideocontext" in url:
            return httpx.Response(200, json={"room_status": "public"})  # no hls_source
        if url.rstrip("/") == "https://chaturbate.com/alice":
            return httpx.Response(200, text=html)
        raise AssertionError(f"unexpected request to {url}")

    _patch_async_client(monkeypatch, handler)

    assert asyncio.run(extract_hls_url("alice")) == "https://edge.example/dossier.m3u8"
