"""Version checking tool — compares running version against PyPI."""

from __future__ import annotations

import importlib.metadata
import logging

import httpx
from packaging.version import Version

from codereviewbuddy.models import UpdateCheckResult

logger = logging.getLogger(__name__)

_PYPI_URL = "https://pypi.org/pypi/codereviewbuddy/json"
_TIMEOUT = 2.0


def _get_current_version() -> str:
    """Get the currently installed version of codereviewbuddy."""
    try:
        return importlib.metadata.version("codereviewbuddy")
    except Exception:
        return "unknown"


async def _get_latest_version() -> str | None:
    """Query PyPI for the latest version. Returns None on failure."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(_PYPI_URL)
            resp.raise_for_status()
            data = resp.json()
            return data["info"]["version"]
    except Exception:
        logger.debug("Failed to check PyPI for latest version", exc_info=True)
        return None


async def check_for_updates() -> UpdateCheckResult:
    """Check if a newer version of codereviewbuddy is available on PyPI."""
    current = _get_current_version()
    latest_str = await _get_latest_version()

    if latest_str is None:
        return UpdateCheckResult(
            current_version=current,
            latest_version="unknown",
            update_available=False,
        )

    try:
        update_available = Version(latest_str) > Version(current)
    except Exception:
        logger.debug("Failed to parse version strings", exc_info=True)
        return UpdateCheckResult(
            current_version=current,
            latest_version=latest_str,
            update_available=False,
        )
    return UpdateCheckResult(
        current_version=current,
        latest_version=latest_str,
        update_available=update_available,
    )
