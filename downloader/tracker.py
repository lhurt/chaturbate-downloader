"""SQLite-backed registry of streamers that have ever been downloaded.

Stores one row per username plus the most recent online-status snapshot
captured by the background poller. All sqlite3 calls run via the default
executor so the asyncio event loop never blocks; a threading.Lock
serializes writes since one connection is shared across threads.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS tracked (
    username                TEXT PRIMARY KEY,
    first_added_at          REAL NOT NULL,
    last_downloaded_at      REAL NOT NULL,
    download_count          INTEGER NOT NULL DEFAULT 1,
    last_status             TEXT,
    last_status_checked_at  REAL,
    last_seen_online_at     REAL,
    auto_download           INTEGER NOT NULL DEFAULT 0
);
"""


class Tracker:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._conn.executescript(SCHEMA)
            columns = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(tracked)").fetchall()
            }
            if "auto_download" not in columns:
                self._conn.execute(
                    "ALTER TABLE tracked "
                    "ADD COLUMN auto_download INTEGER NOT NULL DEFAULT 0"
                )

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    def _add_sync(self, username: str) -> bool:
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO tracked (username, first_added_at, last_downloaded_at, download_count)
                VALUES (?, ?, ?, 0)
                ON CONFLICT(username) DO NOTHING
                """,
                (username, now, now),
            )
        return cur.rowcount > 0

    def _upsert_download_sync(self, username: str) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO tracked (username, first_added_at, last_downloaded_at, download_count)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(username) DO UPDATE SET
                    last_downloaded_at = excluded.last_downloaded_at,
                    download_count = download_count + 1
                """,
                (username, now, now),
            )

    def _list_sync(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT username, first_added_at, last_downloaded_at, download_count,
                       last_status, last_status_checked_at, last_seen_online_at,
                       auto_download
                FROM tracked
                ORDER BY
                    CASE WHEN last_status = 'public' THEN 0 ELSE 1 END,
                    last_downloaded_at DESC
                """
            ).fetchall()
        result = [dict(row) for row in rows]
        for row in result:
            row["auto_download"] = bool(row["auto_download"])
        return result

    def _list_usernames_sync(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT username FROM tracked").fetchall()
        return [r["username"] for r in rows]

    def _delete_sync(self, username: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM tracked WHERE username = ?", (username,)
            )
        return cur.rowcount > 0

    def _set_auto_download_sync(self, username: str, enabled: bool) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE tracked SET auto_download = ? WHERE username = ?",
                (int(enabled), username),
            )
        return cursor.rowcount > 0

    def _is_auto_download_enabled_sync(self, username: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT auto_download FROM tracked WHERE username = ?",
                (username,),
            ).fetchone()
        return bool(row and row["auto_download"])

    def _update_status_sync(self, username: str, status: Optional[str]) -> None:
        now = time.time()
        is_online = status == "public"
        with self._lock:
            if is_online:
                self._conn.execute(
                    """
                    UPDATE tracked SET
                        last_status = ?,
                        last_status_checked_at = ?,
                        last_seen_online_at = ?
                    WHERE username = ?
                    """,
                    (status, now, now, username),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE tracked SET
                        last_status = ?,
                        last_status_checked_at = ?
                    WHERE username = ?
                    """,
                    (status, now, username),
                )

    async def add(self, username: str) -> bool:
        """Track a username without recording a download. Returns False if already tracked."""
        return await asyncio.get_running_loop().run_in_executor(
            None, self._add_sync, username
        )

    async def upsert_download(self, username: str) -> None:
        await asyncio.get_running_loop().run_in_executor(
            None, self._upsert_download_sync, username
        )

    async def list_all(self) -> list[dict]:
        return await asyncio.get_running_loop().run_in_executor(
            None, self._list_sync
        )

    async def list_usernames(self) -> list[str]:
        return await asyncio.get_running_loop().run_in_executor(
            None, self._list_usernames_sync
        )

    async def delete(self, username: str) -> bool:
        return await asyncio.get_running_loop().run_in_executor(
            None, self._delete_sync, username
        )

    async def set_auto_download(self, username: str, enabled: bool) -> bool:
        return await asyncio.get_running_loop().run_in_executor(
            None, self._set_auto_download_sync, username, enabled
        )

    async def is_auto_download_enabled(self, username: str) -> bool:
        return await asyncio.get_running_loop().run_in_executor(
            None, self._is_auto_download_enabled_sync, username
        )

    async def update_status(self, username: str, status: Optional[str]) -> None:
        await asyncio.get_running_loop().run_in_executor(
            None, self._update_status_sync, username, status
        )
