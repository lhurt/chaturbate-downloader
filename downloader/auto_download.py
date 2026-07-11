from __future__ import annotations

import asyncio
import logging
from functools import partial

from .manager import DownloadManager
from .tracker import Tracker

logger = logging.getLogger(__name__)


class AutoDownloadScheduler:
    def __init__(
        self,
        manager: DownloadManager,
        tracker: Tracker,
        max_concurrent: int = 4,
    ) -> None:
        self._manager = manager
        self._tracker = tracker
        self._limit = asyncio.Semaphore(max_concurrent)
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def schedule(self, username: str) -> None:
        current = self._tasks.get(username)
        if current is not None and not current.done():
            return

        task = asyncio.create_task(
            self._start_if_enabled(username),
            name=f"auto-download:{username}",
        )
        self._tasks[username] = task
        task.add_done_callback(partial(self._finish, username))

    async def _start_if_enabled(self, username: str) -> None:
        async with self._limit:
            if not await self._tracker.is_auto_download_enabled(username):
                return

            result = await self._manager.start_download(
                username=username,
                output_format="mp4",
                max_duration=None,
            )
            if result.get("status") == "started":
                await self._tracker.upsert_download(username)
                return

            logger.debug(
                "automatic download not started for %s: %s",
                username,
                result.get("error", result.get("status", "unknown result")),
            )

    def _finish(self, username: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(username) is task:
            self._tasks.pop(username, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "automatic download task failed for %s: %s",
                username,
                error,
            )

    async def stop(self) -> None:
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
