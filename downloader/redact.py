"""Shared helpers for keeping auth tokens out of logs and debug output."""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlsplit, urlunsplit


def _redact_url(url: Optional[str]) -> Optional[str]:
    """Remove query/fragment token material before logging a URL."""
    if not url:
        return url
    parts = urlsplit(url)
    query = "…" if parts.query else ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def _redact_text_urls(text: str) -> str:
    """Redact token-bearing query strings in free-form log text."""
    redacted = re.sub(
        r"https?://[^\s\"'<>]+",
        lambda match: _redact_url(match.group(0)) or "",
        text,
    )
    return re.sub(r"([^#\s\"'<>?]+)\?[^\s\"'<>]+", r"\1?…", redacted)


def _exception_summary(exc: Exception) -> str:
    """Return useful exception text even for exceptions with empty str()."""
    message = str(exc).strip()
    if message:
        return message
    return type(exc).__name__


def _segment_identity(url: str) -> str:
    """Identify a segment independent of rotating auth query tokens."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
