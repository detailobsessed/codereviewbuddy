"""In-memory TTL cache for GitHub API responses.

Avoids redundant API calls when multiple MCP tools fetch the same data
within a short window (e.g. list_review_comments → resolve_stale_comments).

- Queries are cached with a 30-second TTL
- Mutations automatically clear the entire cache
- Cache is process-scoped (resets on MCP server restart)
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_cache: dict[str, tuple[float, Any]] = {}
_lock = threading.Lock()
_DEFAULT_TTL: float = 30.0

_SENTINEL = object()


def make_key(*args: Any, **kwargs: Any) -> str:
    """Create a stable cache key from function arguments."""
    raw = json.dumps({"a": args, "k": kwargs}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def get(key: str) -> Any:
    """Get a cached value if it exists and hasn't expired.

    Returns ``_SENTINEL`` on cache miss (to distinguish from cached ``None``).
    """
    with _lock:
        if key in _cache:
            timestamp, value = _cache[key]
            if time.monotonic() - timestamp < _DEFAULT_TTL:
                logger.debug("Cache hit: %s", key)
                return value
            del _cache[key]
            logger.debug("Cache expired: %s", key)
        return _SENTINEL


def put(key: str, value: Any) -> None:
    """Store a value in the cache with the current timestamp."""
    with _lock:
        _cache[key] = (time.monotonic(), value)
    logger.debug("Cache put: %s", key)


def clear() -> None:
    """Clear the entire cache (called after mutations)."""
    with _lock:
        if _cache:
            logger.debug("Cache cleared (%d entries)", len(_cache))
        _cache.clear()


def size() -> int:
    """Return the number of entries in the cache (for testing)."""
    with _lock:
        return len(_cache)
