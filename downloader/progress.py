"""Download progress tracking and the video/audio startup barrier.

Standalone from HLSDownloader's control flow: neither type shares mutable
state with the download loop, and DownloadProgress is already imported
directly by manager.py.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


class _StartupBarrier:
    """Small two-party async barrier compatible with Python 3.10."""

    def __init__(self, parties: int):
        self.parties = parties
        self.arrived = 0
        self._event = asyncio.Event()
        self._broken = False

    async def arrive_and_wait(self) -> None:
        if self._broken:
            raise RuntimeError("startup barrier broken")
        self.arrived += 1
        if self.arrived >= self.parties:
            self._event.set()
        await self._event.wait()
        if self._broken:
            raise RuntimeError("startup barrier broken")

    def abort(self) -> None:
        self._broken = True
        self._event.set()


@dataclass
class DownloadProgress:
    username: str
    total_segments: int = 0
    downloaded_segments: int = 0
    failed_segments: int = 0
    bytes_downloaded: int = 0
    start_time: float = field(default_factory=time.time)
    is_live: bool = True
    output_path: str = ""
    error_message: str = ""
    warning_message: str = ""
    status: str = "starting"

    @property
    def progress_pct(self) -> float:
        if self.total_segments == 0:
            return 0.0
        return (self.downloaded_segments / self.total_segments) * 100

    @property
    def speed_mbps(self) -> float:
        elapsed = time.time() - self.start_time
        if elapsed == 0:
            return 0.0
        return (self.bytes_downloaded / elapsed) / (1024 * 1024)

    @property
    def elapsed_seconds(self) -> float:
        return time.time() - self.start_time

    def to_dict(self) -> dict:
        return {
            "username": self.username,
            "total_segments": self.total_segments,
            "downloaded_segments": self.downloaded_segments,
            "failed_segments": self.failed_segments,
            "bytes_downloaded": self.bytes_downloaded,
            "progress_pct": round(self.progress_pct, 1),
            "speed_mbps": round(self.speed_mbps, 2),
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "is_live": self.is_live,
            "output_path": self.output_path,
            "error_message": self.error_message,
            "warning_message": self.warning_message,
            "status": self.status,
        }
