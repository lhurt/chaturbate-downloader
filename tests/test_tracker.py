"""Direct tests for downloader.tracker's real SQLite-backed CRUD.

test_auto_download.py exercises upsert_download/set_auto_download/list_all
against a real Tracker, but add/delete/update_status/list_usernames/
is_auto_download_enabled were only ever exercised through FakeTracker
doubles elsewhere in the suite -- a bug in the actual SQL (wrong column,
wrong WHERE clause) would never have been caught.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from downloader.tracker import Tracker


def test_add_then_add_again_reports_already_tracked(tmp_path):
    async def scenario():
        tracker = Tracker(tmp_path / "tracked.db")
        try:
            first = await tracker.add("alice")
            second = await tracker.add("alice")
            usernames = await tracker.list_usernames()
        finally:
            tracker.close()
        return first, second, usernames

    first, second, usernames = asyncio.run(scenario())

    assert first is True
    assert second is False
    assert usernames == ["alice"]


def test_delete_removes_a_tracked_username(tmp_path):
    async def scenario():
        tracker = Tracker(tmp_path / "tracked.db")
        try:
            await tracker.add("alice")
            removed = await tracker.delete("alice")
            removed_again = await tracker.delete("alice")
            usernames = await tracker.list_usernames()
        finally:
            tracker.close()
        return removed, removed_again, usernames

    removed, removed_again, usernames = asyncio.run(scenario())

    assert removed is True
    assert removed_again is False
    assert usernames == []


def test_update_status_public_records_last_seen_online(tmp_path):
    async def scenario():
        tracker = Tracker(tmp_path / "tracked.db")
        try:
            await tracker.add("alice")
            await tracker.update_status("alice", "public")
            rows = await tracker.list_all()
        finally:
            tracker.close()
        return rows

    rows = asyncio.run(scenario())

    assert rows[0]["last_status"] == "public"
    assert rows[0]["last_seen_online_at"] is not None


def test_update_status_offline_does_not_touch_last_seen_online(tmp_path):
    async def scenario():
        tracker = Tracker(tmp_path / "tracked.db")
        try:
            await tracker.add("alice")
            await tracker.update_status("alice", "public")
            first_seen = (await tracker.list_all())[0]["last_seen_online_at"]

            await tracker.update_status("alice", "offline")
            rows = await tracker.list_all()
        finally:
            tracker.close()
        return first_seen, rows

    first_seen, rows = asyncio.run(scenario())

    assert rows[0]["last_status"] == "offline"
    assert rows[0]["last_seen_online_at"] == first_seen


def test_is_auto_download_enabled_defaults_false_until_set(tmp_path):
    async def scenario():
        tracker = Tracker(tmp_path / "tracked.db")
        try:
            await tracker.add("alice")
            before = await tracker.is_auto_download_enabled("alice")
            await tracker.set_auto_download("alice", enabled=True)
            after = await tracker.is_auto_download_enabled("alice")
        finally:
            tracker.close()
        return before, after

    before, after = asyncio.run(scenario())

    assert before is False
    assert after is True


def test_is_auto_download_enabled_false_for_unknown_username(tmp_path):
    async def scenario():
        tracker = Tracker(tmp_path / "tracked.db")
        try:
            return await tracker.is_auto_download_enabled("nobody")
        finally:
            tracker.close()

    assert asyncio.run(scenario()) is False


def test_set_auto_download_returns_false_for_untracked_username(tmp_path):
    async def scenario():
        tracker = Tracker(tmp_path / "tracked.db")
        try:
            return await tracker.set_auto_download("nobody", enabled=True)
        finally:
            tracker.close()

    assert asyncio.run(scenario()) is False


def test_list_all_orders_public_streamers_first(tmp_path):
    async def scenario():
        tracker = Tracker(tmp_path / "tracked.db")
        try:
            await tracker.add("alice")
            await tracker.add("bob")
            await tracker.update_status("bob", "public")
            rows = await tracker.list_all()
        finally:
            tracker.close()
        return rows

    rows = asyncio.run(scenario())

    assert [r["username"] for r in rows] == ["bob", "alice"]
