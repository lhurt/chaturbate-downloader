"""Shared HTTP client configuration."""

from __future__ import annotations

import os
from typing import Any

PROXY_ENV_VAR = "CB_PROXY_URL"


def proxy_kwargs() -> dict[str, Any]:
    """Return explicit httpx proxy config for stream-related requests."""
    proxy_url = os.getenv(PROXY_ENV_VAR, "").strip()
    if not proxy_url:
        return {}
    return {"proxy": proxy_url}
