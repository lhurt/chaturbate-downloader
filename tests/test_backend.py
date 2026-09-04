import asyncio
import sys
from pathlib import Path

import httpx
import m3u8
from fastapi import HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as webapp
import downloader.converter as converter_module
import downloader.hls as hls_module
from downloader.http_client import proxy_kwargs
from downloader.hls import DownloadProgress, HLSDownloader
from downloader.manager import DownloadManager


def test_validate_username_rejects_traversalish_names():
    bad_names = ["../alice", "alice/../bob", "..", "alice.bob", "alice-bob", "alice%2fbob"]

    for name in bad_names:
        try:
            webapp._validate_username(name)
        except HTTPException as exc:
            assert exc.status_code == 400
        else:
            raise AssertionError(f"{name!r} should have been rejected")


def test_index_renders_configured_modal_width(monkeypatch):
    monkeypatch.setattr(webapp, "MODAL_WIDTH_PERCENT", 55)

    client = TestClient(webapp.app)
    response = client.get("/")

    assert response.status_code == 200
    assert "--modal-width-percent: 55;" in response.text


def test_downloads_list_excludes_split_temp_mp4_files(tmp_path, monkeypatch):
    (tmp_path / "alice_2026-04-27_10-00-00.mp4").write_bytes(b"done")
    (tmp_path / "alice_2026-04-27_10-00-00_video.mp4").write_bytes(b"video")
    (tmp_path / "alice_2026-04-27_10-00-00_audio.mp4").write_bytes(b"audio")
    monkeypatch.setattr(webapp, "DOWNLOADS_DIR", tmp_path)

    client = TestClient(webapp.app)
    response = client.get("/api/downloads/list")

    assert response.status_code == 200
    assert [item["filename"] for item in response.json()] == [
        "alice_2026-04-27_10-00-00.mp4"
    ]


def test_downloads_list_reports_has_contact_sheet(tmp_path, monkeypatch):
    (tmp_path / "alice_2026-04-27_10-00-00.mp4").write_bytes(b"done")
    (tmp_path / "alice_2026-04-27_10-00-00_contactsheet.jpg").write_bytes(b"jpeg")
    (tmp_path / "bob_2026-04-27_11-00-00.mp4").write_bytes(b"done")
    monkeypatch.setattr(webapp, "DOWNLOADS_DIR", tmp_path)

    client = TestClient(webapp.app)
    response = client.get("/api/downloads/list")

    assert response.status_code == 200
    by_filename = {item["filename"]: item["has_contact_sheet"] for item in response.json()}
    assert by_filename == {
        "alice_2026-04-27_10-00-00.mp4": True,
        "bob_2026-04-27_11-00-00.mp4": False,
    }


def test_exact_filename_download_returns_exact_file(tmp_path, monkeypatch):
    requested = tmp_path / "alice_2026-04-27_10-00-00.mp4"
    other = tmp_path / "alice_2026-04-27_11-00-00.mp4"
    requested.write_bytes(b"exact file")
    other.write_bytes(b"other file")
    monkeypatch.setattr(webapp, "DOWNLOADS_DIR", tmp_path)

    client = TestClient(webapp.app)
    response = client.get(f"/api/downloads/file/{requested.name}")

    assert response.status_code == 200
    assert response.content == b"exact file"
    assert f'filename="{requested.name}"' in response.headers["content-disposition"]


def test_exact_filename_download_rejects_temp_track_file(tmp_path, monkeypatch):
    temp_track = tmp_path / "alice_2026-04-27_10-00-00_video.mp4"
    temp_track.write_bytes(b"temp")
    monkeypatch.setattr(webapp, "DOWNLOADS_DIR", tmp_path)

    client = TestClient(webapp.app)
    response = client.get(f"/api/downloads/file/{temp_track.name}")

    assert response.status_code == 404


def test_start_endpoint_rejects_ts_output_format():
    client = TestClient(webapp.app)

    response = client.post(
        "/api/download/start",
        params={"username": "alice", "output_format": "ts"},
        headers={"origin": "http://localhost:8000"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Format must be 'mp4'"


def test_start_endpoint_rejects_cross_site_requests():
    client = TestClient(webapp.app)

    response = client.post(
        "/api/download/start",
        params={"username": "alice"},
        headers={"sec-fetch-site": "cross-site"},
    )

    assert response.status_code == 403


def test_start_endpoint_rejects_unconfigured_origin_even_with_matching_host():
    client = TestClient(webapp.app)

    response = client.post(
        "/api/download/start",
        params={"username": "alice"},
        headers={"origin": "http://evil.example:8000", "host": "evil.example:8000"},
    )

    assert response.status_code == 403


def test_start_download_marks_tracker_status_public(monkeypatch):
    async def scenario():
        calls = []

        class FakeRequest:
            headers = {"origin": "http://localhost:8000"}

        class FakeManager:
            async def start_download(self, **kwargs):
                calls.append(("start", kwargs))
                return {"status": "started", "username": kwargs["username"]}

        class FakeTracker:
            async def upsert_download(self, username):
                calls.append(("upsert", username))

            async def update_status(self, username, status):
                calls.append(("status", username, status))

        monkeypatch.setattr(webapp, "manager", FakeManager())
        monkeypatch.setattr(webapp, "tracker", FakeTracker())

        result = await webapp.start_download(FakeRequest(), "Alice")

        assert result == {"status": "started", "username": "alice"}
        assert calls == [
            (
                "start",
                {"username": "alice", "output_format": "mp4", "max_duration": None},
            ),
            ("upsert", "alice"),
            ("status", "alice", "public"),
        ]

    asyncio.run(scenario())


def test_tracked_status_poll_skips_indeterminate_status(monkeypatch):
    async def scenario():
        updates = []

        class FakeTracker:
            async def list_usernames(self):
                return ["alice"]

            async def update_status(self, username, status):
                updates.append((username, status))

        async def fake_fetch_room_status(client, username):
            return None

        async def fake_sleep(seconds):
            raise asyncio.CancelledError

        monkeypatch.setattr(webapp, "tracker", FakeTracker())
        monkeypatch.setattr(webapp, "fetch_room_status", fake_fetch_room_status)
        monkeypatch.setattr(webapp.asyncio, "sleep", fake_sleep)

        try:
            await webapp._poll_tracked_status()
        except asyncio.CancelledError:
            pass

        assert updates == []

    asyncio.run(scenario())


def test_redact_text_urls_handles_absolute_and_relative_query_tokens():
    text = "https://cdn.example/playlist.m3u8?token=secret\nsegment.m4s?verify=secret"

    redacted = webapp._redact_text_urls(text)

    assert "secret" not in redacted
    assert "https://cdn.example/playlist.m3u8?…" in redacted
    assert "segment.m4s?…" in redacted


def test_hls_redaction_helpers_strip_tokens_from_urls_and_text():
    url = "https://cdn.example/path/seg.m4s?token=secret#frag"
    text = f"fetching {url} relative.m4s?verify=hidden"

    assert hls_module._redact_url(url) == "https://cdn.example/path/seg.m4s?…"
    redacted = hls_module._redact_text_urls(text)
    assert "secret" not in redacted
    assert "hidden" not in redacted
    assert "https://cdn.example/path/seg.m4s?…" in redacted


def test_exception_summary_falls_back_to_exception_type():
    assert hls_module._exception_summary(httpx.ConnectTimeout("")) == "ConnectTimeout"


def test_proxy_kwargs_reads_explicit_cb_proxy_url(monkeypatch):
    monkeypatch.setenv("CB_PROXY_URL", "http://proxy.example:8080")

    assert proxy_kwargs() == {"proxy": "http://proxy.example:8080"}


def test_completed_file_username_parsing_preserves_usernames_containing_20(tmp_path, monkeypatch):
    completed = tmp_path / "alice_2020_fan_2026-04-27_10-00-00.mp4"
    completed.write_bytes(b"done")
    monkeypatch.setattr(webapp, "DOWNLOADS_DIR", tmp_path)

    client = TestClient(webapp.app)
    response = client.get("/api/downloads/list")

    assert response.status_code == 200
    assert response.json()[0]["username"] == "alice_2020_fan"


def test_download_progress_has_non_error_warning_channel():
    progress = DownloadProgress(username="alice", warning_message="Solo video")

    data = progress.to_dict()

    assert data["warning_message"] == "Solo video"
    assert data["error_message"] == ""


def test_finalize_partial_audio_uses_warning_not_error(tmp_path):
    async def scenario():
        downloader = HLSDownloader(output_dir=tmp_path)
        video_file = tmp_path / "alice_video.mp4"
        audio_file = tmp_path / "alice_audio.mp4"
        video_file.write_bytes(b"video")
        audio_file.write_bytes(b"partial audio")
        progress = DownloadProgress(username="alice")

        result = await downloader._finalize(
            progress,
            "alice",
            "alice_2026-04-27_10-00-00",
            video_file,
            audio_file,
            audio_ok=False,
        )

        assert result.status == "done"
        assert result.error_message == ""
        assert result.warning_message == "Solo video (audio incompleto)"
        assert Path(result.output_path).read_bytes() == b"video"
        assert not audio_file.exists()

    asyncio.run(scenario())


def test_download_stream_refreshes_when_initial_master_resolution_fails(tmp_path):
    async def scenario():
        class FakeDownloader(HLSDownloader):
            def __init__(self):
                super().__init__(output_dir=tmp_path)
                self.resolves = []
                self.refreshes = 0

            async def _resolve_master(self, client, master_url):
                self.resolves.append(master_url)
                if len(self.resolves) == 1:
                    self._last_master_error = "Cannot fetch master playlist: timeout"
                    return None, None
                return "https://cdn.example/live/video.m3u8?token=new", None

            async def _refresh_url(self, username):
                self.refreshes += 1
                return "https://cdn.example/live/master.m3u8?token=new"

            async def _validate_playlist(self, client, url, label):
                return True

            async def _download_track(self, *args, **kwargs):
                return True

            async def _finalize(self, progress, *args, **kwargs):
                progress.status = "done"
                progress.output_path = str(tmp_path / "alice.mp4")
                return progress

        downloader = FakeDownloader()

        result = await downloader.download_stream(
            "alice",
            "https://cdn.example/live/master.m3u8?token=old",
        )

        assert result.status == "done"
        assert downloader.refreshes == 1
        assert downloader.resolves == [
            "https://cdn.example/live/master.m3u8?token=old",
            "https://cdn.example/live/master.m3u8?token=new",
        ]

    asyncio.run(scenario())


def test_download_stream_reports_initial_master_resolution_cause(tmp_path):
    async def scenario():
        class FakeDownloader(HLSDownloader):
            async def _resolve_master(self, client, master_url):
                self._last_master_error = "Cannot fetch master playlist: timed out"
                return None, None

            async def _refresh_url(self, username):
                return None

        downloader = FakeDownloader(output_dir=tmp_path)

        result = await downloader.download_stream(
            "alice",
            "https://cdn.example/live/master.m3u8?token=secret",
        )

        assert result.status == "error"
        assert result.error_message == (
            "No se pudo resolver el playlist de video: "
            "Cannot fetch master playlist: timed out"
        )

    asyncio.run(scenario())


def test_startup_barrier_waits_for_two_real_parties():
    async def scenario():
        barrier = hls_module._StartupBarrier(2)
        released = False

        async def first_party():
            nonlocal released
            await barrier.arrive_and_wait()
            released = True

        task = asyncio.create_task(first_party())
        await asyncio.sleep(0)
        assert not released
        await barrier.arrive_and_wait()
        await asyncio.wait_for(task, timeout=1)
        assert released

    asyncio.run(scenario())


def test_failed_segment_is_not_marked_downloaded_before_success(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(hls_module, "MAX_EMPTY_POLLS", 1)

        class FakeDownloader(HLSDownloader):
            def __init__(self):
                super().__init__(output_dir=tmp_path)
                self.fetches = 0

            async def _fetch_media_playlist(self, client, url, _depth=0):
                return m3u8.loads(
                    "#EXTM3U\n#EXT-X-TARGETDURATION:1\n"
                    "#EXTINF:1.0,\nseg.m4s?token=old\n"
                )

            async def _fetch_segment_with_retry(self, *args, **kwargs):
                self.fetches += 1
                if self.fetches == 1:
                    return None
                return b"segment"

        downloader = FakeDownloader()
        progress = DownloadProgress(username="alice")

        ok = await downloader._download_track(
            object(),
            asyncio.Event(),
            "https://cdn.example/live/playlist.m3u8?token=old",
            tmp_path / "track.mp4",
            "alice",
            "video",
            progress,
            max_duration=None,
        )

        assert ok is True
        assert downloader.fetches == 2
        assert progress.failed_segments == 1
        assert progress.downloaded_segments == 1

    asyncio.run(scenario())


def test_segment_403_refresh_does_not_count_failed_segment(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(hls_module, "MAX_EMPTY_POLLS", 1)

        class FakeDownloader(HLSDownloader):
            def __init__(self):
                super().__init__(output_dir=tmp_path)
                self.fetches = 0
                self.refreshes = 0

            async def _fetch_media_playlist(self, client, url, _depth=0):
                token = "new" if "new" in url else "old"
                return m3u8.loads(
                    "#EXTM3U\n#EXT-X-TARGETDURATION:1\n"
                    f"#EXTINF:1.0,\nseg.m4s?token={token}\n"
                )

            async def _resolve_master(self, client, master_url):
                return "https://cdn.example/live/playlist.m3u8?token=new", None

            async def _refresh_url(self, username):
                self.refreshes += 1
                return "https://example.invalid/master.m3u8?token=new"

            async def _fetch_segment_with_retry(self, client, semaphore, url, **kwargs):
                self.fetches += 1
                if self.fetches == 1:
                    request = httpx.Request("GET", url)
                    response = httpx.Response(403, request=request)
                    raise httpx.HTTPStatusError("forbidden", request=request, response=response)
                return b"segment"

        downloader = FakeDownloader()
        progress = DownloadProgress(username="alice")

        ok = await downloader._download_track(
            object(),
            asyncio.Event(),
            "https://cdn.example/live/playlist.m3u8?token=old",
            tmp_path / "track.mp4",
            "alice",
            "video",
            progress,
            max_duration=None,
        )

        assert ok is True
        assert downloader.refreshes == 1
        assert progress.failed_segments == 0
        assert progress.downloaded_segments == 1
        assert progress.total_segments == 1

    asyncio.run(scenario())


def test_mid_batch_segment_403_keeps_progress_totals_consistent(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(hls_module, "MAX_EMPTY_POLLS", 1)

        class FakeDownloader(HLSDownloader):
            def __init__(self):
                super().__init__(output_dir=tmp_path)
                self.refreshes = 0

            async def _fetch_media_playlist(self, client, url, _depth=0):
                token = "new" if "new" in url else "old"
                return m3u8.loads(
                    "#EXTM3U\n#EXT-X-TARGETDURATION:1\n"
                    f"#EXTINF:1.0,\nseg-0.m4s?token={token}\n"
                    f"#EXTINF:1.0,\nseg-1.m4s?token={token}\n"
                )

            async def _resolve_master(self, client, master_url):
                return "https://cdn.example/live/playlist.m3u8?token=new", None

            async def _refresh_url(self, username):
                self.refreshes += 1
                return "https://example.invalid/master.m3u8?token=new"

            async def _fetch_segment_with_retry(self, client, semaphore, url, **kwargs):
                if "seg-1" in url and "token=old" in url:
                    request = httpx.Request("GET", url)
                    response = httpx.Response(403, request=request)
                    raise httpx.HTTPStatusError("forbidden", request=request, response=response)
                if "seg-0" in url:
                    return b"zero"
                return b"one"

        downloader = FakeDownloader()
        progress = DownloadProgress(username="alice")
        output_file = tmp_path / "track.mp4"

        ok = await downloader._download_track(
            object(),
            asyncio.Event(),
            "https://cdn.example/live/playlist.m3u8?token=old",
            output_file,
            "alice",
            "video",
            progress,
            max_duration=None,
        )

        assert ok is True
        assert downloader.refreshes == 1
        assert progress.failed_segments == 0
        assert progress.downloaded_segments == 2
        assert progress.total_segments == 2
        assert output_file.read_bytes() == b"zeroone"

    asyncio.run(scenario())


def test_refresh_overlap_dedupe_uses_token_insensitive_identity():
    old_url = "https://cdn.example/live/seg-1.m4s?token=old"
    new_url = "https://cdn.example/live/seg-1.m4s?token=new"

    assert hls_module._segment_identity(old_url) == hls_module._segment_identity(new_url)


def test_generate_contact_sheet_builds_tile_filter_from_duration(monkeypatch, tmp_path):
    calls = []

    monkeypatch.setattr(converter_module, "_ffmpeg_available", lambda: True)
    monkeypatch.setattr(converter_module, "_probe_duration", lambda _path: 725.0)

    class FakeResult:
        returncode = 0
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"jpeg")
        return FakeResult()

    monkeypatch.setattr(converter_module.subprocess, "run", fake_run)

    output = tmp_path / "sheet.jpg"
    ok = converter_module.generate_contact_sheet(str(tmp_path / "in.mp4"), str(output))

    assert ok is True
    assert output.exists()

    # 725s at a 60s interval samples 13 frames (0..720), each via a fast
    # keyframe seek, then a final call tiles them into a 10x2 grid.
    extraction_calls = [c for c in calls if "-ss" in c]
    assert len(extraction_calls) == 13

    tile_cmd = calls[-1]
    vf_index = tile_cmd.index("-vf") + 1
    assert tile_cmd[vf_index] == "tile=10x2"


def test_generate_contact_sheet_returns_false_without_ffmpeg(monkeypatch, tmp_path):
    monkeypatch.setattr(converter_module, "_ffmpeg_available", lambda: False)

    ok = converter_module.generate_contact_sheet(
        str(tmp_path / "in.mp4"), str(tmp_path / "sheet.jpg")
    )

    assert ok is False


def test_generate_contact_sheet_returns_false_on_ffmpeg_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(converter_module, "_ffmpeg_available", lambda: True)
    monkeypatch.setattr(converter_module, "_probe_duration", lambda _path: None)

    class FakeResult:
        returncode = 1
        stderr = "boom"

    monkeypatch.setattr(converter_module.subprocess, "run", lambda *a, **k: FakeResult())

    ok = converter_module.generate_contact_sheet(
        str(tmp_path / "in.mp4"), str(tmp_path / "sheet.jpg")
    )

    assert ok is False


def test_contact_sheet_endpoint_generates_and_caches(tmp_path, monkeypatch):
    completed = tmp_path / "alice_2026-04-27_10-00-00.mp4"
    completed.write_bytes(b"done")
    monkeypatch.setattr(webapp, "DOWNLOADS_DIR", tmp_path)

    calls = []

    def fake_generate_contact_sheet(input_file, output_jpg, *args, **kwargs):
        calls.append((input_file, output_jpg, args))
        Path(output_jpg).write_bytes(b"jpegbytes")
        return True

    monkeypatch.setattr(webapp, "generate_contact_sheet", fake_generate_contact_sheet)

    client = TestClient(webapp.app)
    response = client.get(f"/api/downloads/contact-sheet/{completed.name}")

    assert response.status_code == 200
    assert response.content == b"jpegbytes"
    assert len(calls) == 1


def test_contact_sheet_endpoint_forwards_env_configured_options(tmp_path, monkeypatch):
    completed = tmp_path / "alice_2026-04-27_10-00-00.mp4"
    completed.write_bytes(b"done")
    monkeypatch.setattr(webapp, "DOWNLOADS_DIR", tmp_path)
    monkeypatch.setattr(webapp, "CONTACT_SHEET_INTERVAL_SECONDS", 30)
    monkeypatch.setattr(webapp, "CONTACT_SHEET_TILE_WIDTH", 200)
    monkeypatch.setattr(webapp, "CONTACT_SHEET_COLUMNS", 5)

    calls = []

    def fake_generate_contact_sheet(input_file, output_jpg, *args, **kwargs):
        calls.append(args)
        Path(output_jpg).write_bytes(b"jpegbytes")
        return True

    monkeypatch.setattr(webapp, "generate_contact_sheet", fake_generate_contact_sheet)

    client = TestClient(webapp.app)
    response = client.get(f"/api/downloads/contact-sheet/{completed.name}")

    assert response.status_code == 200
    assert calls == [(30, 200, 5)]

    sheet_path = tmp_path / "alice_2026-04-27_10-00-00_contactsheet.jpg"
    assert sheet_path.exists()

    # Second request should reuse the cached file rather than regenerating.
    response2 = client.get(f"/api/downloads/contact-sheet/{completed.name}")
    assert response2.status_code == 200
    assert response2.content == b"jpegbytes"
    assert len(calls) == 1


def test_contact_sheet_endpoint_rejects_non_media_file(tmp_path, monkeypatch):
    temp_track = tmp_path / "alice_2026-04-27_10-00-00_video.mp4"
    temp_track.write_bytes(b"temp")
    monkeypatch.setattr(webapp, "DOWNLOADS_DIR", tmp_path)

    client = TestClient(webapp.app)
    response = client.get(f"/api/downloads/contact-sheet/{temp_track.name}")

    assert response.status_code == 404


def test_contact_sheet_endpoint_rejects_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "DOWNLOADS_DIR", tmp_path)

    client = TestClient(webapp.app)
    response = client.get("/api/downloads/contact-sheet/alice_2026-04-27_10-00-00.mp4")

    assert response.status_code == 404


def test_contact_sheet_endpoint_returns_502_on_generation_failure(tmp_path, monkeypatch):
    completed = tmp_path / "alice_2026-04-27_10-00-00.mp4"
    completed.write_bytes(b"done")
    monkeypatch.setattr(webapp, "DOWNLOADS_DIR", tmp_path)
    monkeypatch.setattr(webapp, "generate_contact_sheet", lambda *a, **k: False)

    client = TestClient(webapp.app)
    response = client.get(f"/api/downloads/contact-sheet/{completed.name}")

    assert response.status_code == 502


def test_async_mux_cancellation_terminates_ffmpeg(monkeypatch, tmp_path):
    async def scenario():
        monkeypatch.setattr(converter_module, "_ffmpeg_available", lambda: True)
        monkeypatch.setattr(converter_module, "_probe_start_time", lambda _path: None)
        monkeypatch.setattr(converter_module, "_probe_duration", lambda _path: None)

        class FakeProcess:
            def __init__(self):
                self.returncode = None
                self.terminated = False
                self.killed = False
                self._done = asyncio.Event()

            async def communicate(self):
                await self._done.wait()
                self.returncode = 0
                return b"", b""

            def terminate(self):
                self.terminated = True
                self.returncode = -15
                self._done.set()

            def kill(self):
                self.killed = True
                self.returncode = -9
                self._done.set()

            async def wait(self):
                await self._done.wait()
                return self.returncode

        process = FakeProcess()

        async def fake_create_subprocess_exec(*args, **kwargs):
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

        task = asyncio.create_task(
            converter_module.mux_video_audio_async(
                str(tmp_path / "video.mp4"),
                str(tmp_path / "audio.mp4"),
                str(tmp_path / "out.mp4"),
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("mux task should have been cancelled")

        assert process.terminated is True
        assert process.killed is False

    asyncio.run(scenario())


def test_async_mux_timeout_terminates_ffmpeg(monkeypatch, tmp_path):
    async def scenario():
        monkeypatch.setattr(converter_module, "_ffmpeg_available", lambda: True)
        monkeypatch.setattr(converter_module, "_probe_start_time", lambda _path: None)
        monkeypatch.setattr(converter_module, "_probe_duration", lambda _path: None)
        monkeypatch.setattr(converter_module, "FFMPEG_MUX_TIMEOUT", 0.01)

        class FakeProcess:
            def __init__(self):
                self.returncode = None
                self.terminated = False
                self.killed = False
                self._done = asyncio.Event()

            async def communicate(self):
                await asyncio.Event().wait()
                return b"", b""

            def terminate(self):
                self.terminated = True
                self.returncode = -15
                self._done.set()

            def kill(self):
                self.killed = True
                self.returncode = -9
                self._done.set()

            async def wait(self):
                await self._done.wait()
                return self.returncode

        process = FakeProcess()

        async def fake_create_subprocess_exec(*args, **kwargs):
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

        success = await converter_module.mux_video_audio_async(
            str(tmp_path / "video.mp4"),
            str(tmp_path / "audio.mp4"),
            str(tmp_path / "out.mp4"),
        )

        assert success is False
        assert process.terminated is True
        assert process.killed is False

    asyncio.run(scenario())


def test_stop_during_start_reservation_prevents_untracked_task(monkeypatch, tmp_path):
    async def scenario():
        import downloader.manager as manager_module

        extract_started = asyncio.Event()
        extract_can_finish = asyncio.Event()
        download_called = False

        async def fake_extract_hls_url(username):
            extract_started.set()
            await extract_can_finish.wait()
            return "https://example.invalid/stream.m3u8?token=secret"

        class FakeDownloader:
            async def download_stream(self, *args, **kwargs):
                nonlocal download_called
                download_called = True
                raise AssertionError("download_stream should not be called after stop")

            def stop(self, username):
                pass

        monkeypatch.setattr(manager_module, "extract_hls_url", fake_extract_hls_url)
        manager = DownloadManager(output_dir=tmp_path)
        monkeypatch.setattr(manager, "_get_downloader", lambda: FakeDownloader())

        start_task = asyncio.create_task(manager.start_download("alice"))
        await asyncio.wait_for(extract_started.wait(), timeout=1)

        stop_result = await manager.stop_download("alice")
        assert stop_result == {"status": "stopped", "username": "alice"}

        extract_can_finish.set()
        start_result = await asyncio.wait_for(start_task, timeout=1)

        assert start_result == {"status": "stopped", "username": "alice"}
        assert not download_called
        assert "alice" not in manager._tasks

    asyncio.run(scenario())


def test_stop_all_during_start_clears_pending_stop_event(monkeypatch, tmp_path):
    async def scenario():
        import downloader.manager as manager_module

        extract_started = asyncio.Event()
        extract_can_finish = asyncio.Event()

        async def fake_extract_hls_url(username):
            extract_started.set()
            await extract_can_finish.wait()
            return "https://example.invalid/stream.m3u8?token=secret"

        monkeypatch.setattr(manager_module, "extract_hls_url", fake_extract_hls_url)
        manager = DownloadManager(output_dir=tmp_path)

        start_task = asyncio.create_task(manager.start_download("alice"))
        await asyncio.wait_for(extract_started.wait(), timeout=1)

        stop_result = await manager.stop_all()
        assert stop_result == {"status": "all_stopped"}

        extract_can_finish.set()
        start_result = await asyncio.wait_for(start_task, timeout=1)

        assert start_result == {"status": "stopped", "username": "alice"}
        assert "alice" not in manager._tasks
        assert manager._downloader is not None
        assert "alice" not in manager._downloader._stop_events

    asyncio.run(scenario())


def test_stopped_start_cannot_claim_newer_start_reservation(monkeypatch, tmp_path):
    async def scenario():
        import downloader.manager as manager_module

        first_started = asyncio.Event()
        first_can_finish = asyncio.Event()
        second_can_finish = asyncio.Event()
        calls = 0
        download_usernames = []

        async def fake_extract_hls_url(username):
            nonlocal calls
            calls += 1
            if calls == 1:
                first_started.set()
                await first_can_finish.wait()
                return "https://example.invalid/first.m3u8?token=old"
            await second_can_finish.wait()
            return "https://example.invalid/second.m3u8?token=new"

        class FakeDownloader:
            async def download_stream(self, username, *args, **kwargs):
                download_usernames.append(username)
                return manager_module.DownloadProgress(username=username, status="done")

            def stop(self, username):
                pass

            def stop_all(self):
                pass

        monkeypatch.setattr(manager_module, "extract_hls_url", fake_extract_hls_url)
        manager = DownloadManager(output_dir=tmp_path)
        monkeypatch.setattr(manager, "_get_downloader", lambda: FakeDownloader())

        first_task = asyncio.create_task(manager.start_download("alice"))
        await asyncio.wait_for(first_started.wait(), timeout=1)
        assert await manager.stop_download("alice") == {"status": "stopped", "username": "alice"}

        second_task = asyncio.create_task(manager.start_download("alice"))
        await asyncio.sleep(0)

        first_can_finish.set()
        assert await asyncio.wait_for(first_task, timeout=1) == {
            "status": "stopped",
            "username": "alice",
        }

        second_can_finish.set()
        assert await asyncio.wait_for(second_task, timeout=1) == {
            "status": "started",
            "username": "alice",
        }
        await asyncio.sleep(0)
        assert download_usernames == ["alice"]

    asyncio.run(scenario())


def test_status_handles_existing_download_while_new_start_is_reserved(monkeypatch, tmp_path):
    async def scenario():
        import downloader.manager as manager_module

        extract_started = asyncio.Event()
        extract_can_finish = asyncio.Event()

        async def fake_extract_hls_url(username):
            extract_started.set()
            await extract_can_finish.wait()
            return None

        monkeypatch.setattr(manager_module, "extract_hls_url", fake_extract_hls_url)
        manager = DownloadManager(output_dir=tmp_path)
        manager._downloads["alice"] = manager_module.DownloadProgress(username="alice", status="done")

        start_task = asyncio.create_task(manager.start_download("alice"))
        await asyncio.wait_for(extract_started.wait(), timeout=1)

        assert manager.get_download("alice")["active"] is False
        assert manager.get_status()[0]["active"] is False

        extract_can_finish.set()
        await asyncio.wait_for(start_task, timeout=1)

    asyncio.run(scenario())


def test_manager_invokes_on_complete_after_successful_download(monkeypatch, tmp_path):
    async def scenario():
        import downloader.manager as manager_module

        completed_paths = []

        async def fake_extract_hls_url(username):
            return "https://example.invalid/stream.m3u8?token=secret"

        class FakeDownloader:
            async def download_stream(self, username, *args, **kwargs):
                return manager_module.DownloadProgress(
                    username=username, status="done", output_path="/downloads/alice.mp4"
                )

            def stop(self, username):
                pass

        monkeypatch.setattr(manager_module, "extract_hls_url", fake_extract_hls_url)
        manager = DownloadManager(output_dir=tmp_path, on_complete=completed_paths.append)
        monkeypatch.setattr(manager, "_get_downloader", lambda: FakeDownloader())

        await manager.start_download("alice")
        await asyncio.sleep(0)

        assert completed_paths == ["/downloads/alice.mp4"]

    asyncio.run(scenario())


def test_manager_skips_on_complete_when_download_errors(monkeypatch, tmp_path):
    async def scenario():
        import downloader.manager as manager_module

        completed_paths = []

        async def fake_extract_hls_url(username):
            return "https://example.invalid/stream.m3u8?token=secret"

        class FakeDownloader:
            async def download_stream(self, username, *args, **kwargs):
                return manager_module.DownloadProgress(username=username, status="error")

            def stop(self, username):
                pass

        monkeypatch.setattr(manager_module, "extract_hls_url", fake_extract_hls_url)
        manager = DownloadManager(output_dir=tmp_path, on_complete=completed_paths.append)
        monkeypatch.setattr(manager, "_get_downloader", lambda: FakeDownloader())

        await manager.start_download("alice")
        await asyncio.sleep(0)

        assert completed_paths == []

    asyncio.run(scenario())


def test_schedule_contact_sheet_noop_when_auto_generate_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(webapp, "CONTACT_SHEET_AUTO_GENERATE", False)
    created = []
    monkeypatch.setattr(webapp.asyncio, "create_task", lambda coro: created.append(coro))

    webapp._schedule_contact_sheet(tmp_path / "alice_2026-04-27_10-00-00.mp4")

    assert created == []


def test_schedule_contact_sheet_spawns_task_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setattr(webapp, "CONTACT_SHEET_AUTO_GENERATE", True)
    created = []

    def fake_create_task(coro):
        created.append(coro)
        coro.close()  # avoid an "never awaited" warning; we only assert scheduling here

    monkeypatch.setattr(webapp.asyncio, "create_task", fake_create_task)

    webapp._schedule_contact_sheet(tmp_path / "alice_2026-04-27_10-00-00.mp4")

    assert len(created) == 1


def test_downloads_list_auto_generates_missing_contact_sheets_when_enabled(tmp_path, monkeypatch):
    async def scenario():
        (tmp_path / "alice_2026-04-27_10-00-00.mp4").write_bytes(b"done")
        monkeypatch.setattr(webapp, "DOWNLOADS_DIR", tmp_path)
        monkeypatch.setattr(webapp, "CONTACT_SHEET_AUTO_GENERATE", True)

        def fake_generate_contact_sheet(input_file, output_jpg, *args, **kwargs):
            Path(output_jpg).write_bytes(b"jpegbytes")
            return True

        monkeypatch.setattr(webapp, "generate_contact_sheet", fake_generate_contact_sheet)

        result = await webapp.list_downloaded_files()
        assert result[0]["has_contact_sheet"] is False

        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        await asyncio.wait_for(asyncio.gather(*pending), timeout=1)

        sheet_path = tmp_path / "alice_2026-04-27_10-00-00_contactsheet.jpg"
        assert sheet_path.exists()

    asyncio.run(scenario())


def test_downloads_list_does_not_auto_generate_when_disabled(tmp_path, monkeypatch):
    async def scenario():
        (tmp_path / "alice_2026-04-27_10-00-00.mp4").write_bytes(b"done")
        monkeypatch.setattr(webapp, "DOWNLOADS_DIR", tmp_path)
        monkeypatch.setattr(webapp, "CONTACT_SHEET_AUTO_GENERATE", False)

        calls = []
        monkeypatch.setattr(
            webapp, "generate_contact_sheet", lambda *a, **k: calls.append(a) or True
        )

        await webapp.list_downloaded_files()
        await asyncio.sleep(0)

        assert calls == []

    asyncio.run(scenario())
