import asyncio
import sqlite3
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as webapp
from downloader.auto_download import AutoDownloadScheduler
from downloader.tracker import Tracker


def test_tracker_persists_auto_download_preference(tmp_path):
    async def scenario():
        tracker = Tracker(tmp_path / "tracked.db")
        await tracker.upsert_download("alice")

        updated = await tracker.set_auto_download("alice", enabled=True)
        tracker.close()
        reopened = Tracker(tmp_path / "tracked.db")
        rows = await reopened.list_all()
        reopened.close()

        assert updated is True
        assert rows[0]["auto_download"] is True

    asyncio.run(scenario())


def test_scheduler_rechecks_disabled_preference_before_starting():
    async def scenario():
        calls = []

        class FakeTracker:
            async def is_auto_download_enabled(self, username):
                return False

            async def upsert_download(self, username):
                calls.append(("upsert", username))

        class FakeManager:
            async def start_download(self, **kwargs):
                calls.append(("start", kwargs))
                return {"status": "started"}

        scheduler = AutoDownloadScheduler(FakeManager(), FakeTracker())
        scheduler.schedule("alice")
        await scheduler._tasks["alice"]

        assert calls == []

    asyncio.run(scenario())


def test_scheduler_records_only_successful_start():
    async def scenario():
        calls = []

        class FakeTracker:
            async def is_auto_download_enabled(self, username):
                return True

            async def upsert_download(self, username):
                calls.append(("upsert", username))

        class FakeManager:
            async def start_download(self, **kwargs):
                calls.append(("start", kwargs["username"]))
                return {"error": "Already downloading alice"}

        scheduler = AutoDownloadScheduler(FakeManager(), FakeTracker())
        scheduler.schedule("alice")
        await scheduler._tasks["alice"]

        assert calls == [("start", "alice")]

    asyncio.run(scenario())


def test_scheduler_starts_enabled_streamer_and_records_download():
    async def scenario():
        calls = []

        class FakeTracker:
            async def is_auto_download_enabled(self, username):
                calls.append(("enabled", username))
                return True

            async def upsert_download(self, username):
                calls.append(("upsert", username))

        class FakeManager:
            async def start_download(self, **kwargs):
                calls.append(("start", kwargs["username"]))
                return {"status": "started"}

        scheduler = AutoDownloadScheduler(FakeManager(), FakeTracker())
        scheduler.schedule("alice")
        await scheduler._tasks["alice"]

        assert calls == [
            ("enabled", "alice"),
            ("start", "alice"),
            ("upsert", "alice"),
        ]

    asyncio.run(scenario())


def test_tracker_migrates_existing_database_with_auto_download_disabled(tmp_path):
    db_path = tmp_path / "tracked.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            "CREATE TABLE tracked ("
            "username TEXT PRIMARY KEY,"
            "first_added_at REAL NOT NULL,"
            "last_downloaded_at REAL NOT NULL,"
            "download_count INTEGER NOT NULL DEFAULT 1,"
            "last_status TEXT,"
            "last_status_checked_at REAL,"
            "last_seen_online_at REAL"
            ");"
            "INSERT INTO tracked ("
            "username, first_added_at, last_downloaded_at, download_count"
            ") VALUES ('alice', 1, 1, 1);"
        )

    async def scenario():
        tracker = Tracker(db_path)
        rows = await tracker.list_all()
        tracker.close()

        assert rows[0]["auto_download"] is False

    asyncio.run(scenario())


def test_status_poll_starts_enabled_streamer_when_public(monkeypatch):
    async def scenario():
        calls = []

        class FakeTracker:
            async def list_usernames(self):
                return ["alice"]

            async def update_status(self, username, status):
                calls.append(("status", username, status))

        class FakeScheduler:
            def schedule(self, username):
                calls.append(("schedule", username))

        async def fake_fetch_room_status(client, username):
            return "public"

        async def fake_sleep(seconds):
            raise asyncio.CancelledError

        monkeypatch.setattr(webapp, "tracker", FakeTracker())
        monkeypatch.setattr(webapp, "auto_download_scheduler", FakeScheduler(), raising=False)
        monkeypatch.setattr(webapp, "fetch_room_status", fake_fetch_room_status)
        monkeypatch.setattr(webapp.asyncio, "sleep", fake_sleep)

        try:
            await webapp._poll_tracked_status()
        except asyncio.CancelledError:
            pass

        assert calls == [
            ("status", "alice", "public"),
            ("schedule", "alice"),
        ]

    asyncio.run(scenario())


def test_auto_download_route_updates_tracked_streamer(monkeypatch):
    calls = []

    class FakeTracker:
        async def set_auto_download(self, username, enabled):
            calls.append((username, enabled))
            return True

    monkeypatch.setattr(webapp, "tracker", FakeTracker())

    response = TestClient(webapp.app).patch(
        "/api/tracked/Alice/auto-download",
        params={"enabled": "false"},
        headers={"origin": "http://localhost:8000"},
    )

    assert response.status_code == 200
    assert response.json() == {"username": "alice", "auto_download": False}
    assert calls == [("alice", False)]


def test_auto_download_route_rejects_cross_site_request(monkeypatch):
    class FakeTracker:
        async def set_auto_download(self, username, enabled):
            raise AssertionError("tracker must not be called")

    monkeypatch.setattr(webapp, "tracker", FakeTracker())

    response = TestClient(webapp.app).patch(
        "/api/tracked/alice/auto-download",
        params={"enabled": "true"},
        headers={"sec-fetch-site": "cross-site"},
    )

    assert response.status_code == 403


def test_auto_download_route_returns_not_found_for_unknown_streamer(monkeypatch):
    class FakeTracker:
        async def set_auto_download(self, username, enabled):
            return False

    monkeypatch.setattr(webapp, "tracker", FakeTracker())

    response = TestClient(webapp.app).patch(
        "/api/tracked/missing/auto-download",
        params={"enabled": "true"},
        headers={"origin": "http://localhost:8000"},
    )

    assert response.status_code == 404
