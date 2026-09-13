"""Filesystem sweeps for orphaned download artifacts.

Both sweeps below reclaim files that end up with nothing pointing at them
anymore, through paths other than the app's own delete/finalize logic:
a crash or ungraceful shutdown mid-download leaves temp `_video.mp4`/
`_audio.mp4` tracks behind, and a `.mp4` can disappear via a manual `rm` or
an external tool watching the downloads directory (a NAS scanner,
jDownloader, etc.), leaving its `_contactsheet.jpg` sidecar orphaned.

Pure functions taking the downloads directory and any other state they need
explicitly, so they're testable against a tmp_path without a FastAPI app.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

TEMP_TRACK_STEM_RE = re.compile(
    r"^(?P<username>.+)_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_(?:video|audio)$"
)
CONTACT_SHEET_STEM_RE = re.compile(r"^(?P<source_stem>.+)_contactsheet$")


def cleanup_orphaned_temp_files(downloads_dir: Path, active_usernames: set[str]) -> list[str]:
    """Remove leftover `_video.mp4`/`_audio.mp4` temp tracks with no owning
    download. These are produced by HLSDownloader mid-download and are only
    ever renamed/removed by a normal finalize(); a crash, force-cancel after
    the stop watchdog, or ungraceful shutdown skips that step and leaves them
    behind. They're intentionally hidden from every read endpoint, so this is
    the only path that reclaims them. Safe to call any time: usernames in
    `active_usernames` (an active reservation or task) are left untouched."""
    removed = []
    for f in downloads_dir.iterdir():
        if not f.is_file() or f.suffix != ".mp4":
            continue
        match = TEMP_TRACK_STEM_RE.match(f.stem)
        if not match or match.group("username") in active_usernames:
            continue
        try:
            f.unlink()
            removed.append(f.name)
        except OSError as exc:
            logger.warning("Failed to remove orphaned temp file %s: %s", f.name, exc)
    if removed:
        logger.info("Removed %d orphaned temp file(s): %s", len(removed), removed)
    return removed


def cleanup_orphaned_contact_sheets(downloads_dir: Path) -> list[str]:
    """Remove contact-sheet JPEGs whose source recording no longer exists.
    `delete_file` removes the sheet alongside the .mp4 when a user deletes
    through the API, but the .mp4 can also disappear by other means -- a
    manual `rm`, or an external tool watching `downloads/` (a NAS scanner,
    jDownloader, etc. -- see the bind mounts in compose.override.yaml) that
    has no reason to know about the sidecar .jpg. This reclaims those."""
    removed = []
    for f in downloads_dir.iterdir():
        if not f.is_file() or f.suffix != ".jpg":
            continue
        match = CONTACT_SHEET_STEM_RE.match(f.stem)
        if not match:
            continue
        source = f.with_name(match.group("source_stem") + ".mp4")
        if source.exists():
            continue
        try:
            f.unlink()
            removed.append(f.name)
        except OSError as exc:
            logger.warning("Failed to remove orphaned contact sheet %s: %s", f.name, exc)
    if removed:
        logger.info("Removed %d orphaned contact sheet(s): %s", len(removed), removed)
    return removed
