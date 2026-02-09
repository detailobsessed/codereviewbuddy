"""Write-operation observability middleware for codereviewbuddy.

Logs all tool calls to ~/.codereviewbuddy/tool_calls.jsonl and detects
rapid write sequences that are known to trigger client-side transport
hangs (see issue #65).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

from fastmcp.server.middleware.middleware import CallNext, Middleware, MiddlewareContext

logger = logging.getLogger(__name__)

WRITE_TOOLS = frozenset({
    "resolve_comment",
    "resolve_stale_comments",
    "reply_to_comment",
    "request_rereview",
    "create_issue_from_comment",
})

RAPID_WRITE_THRESHOLD = 4
RAPID_WRITE_WINDOW_SECONDS = 2.0

LOG_DIR = Path.home() / ".codereviewbuddy"
LOG_FILE = LOG_DIR / "tool_calls.jsonl"
MAX_LOG_LINES = 1000


class WriteOperationMiddleware(Middleware):
    """Middleware that tracks write operations and detects rapid-call patterns.

    - Logs every tool call to ~/.codereviewbuddy/tool_calls.jsonl
    - Detects rapid write sequences (4+ writes in 2s) and emits a warning
    - Warns on slow writes (>30s)
    """

    def __init__(
        self,
        *,
        log_dir: Path | None = None,
        rapid_threshold: int = RAPID_WRITE_THRESHOLD,
        rapid_window: float = RAPID_WRITE_WINDOW_SECONDS,
        slow_threshold: float = 30.0,
    ) -> None:
        self._log_dir = log_dir or LOG_DIR
        self._log_file = self._log_dir / "tool_calls.jsonl"
        self._rapid_threshold = rapid_threshold
        self._rapid_window = rapid_window
        self._slow_threshold = slow_threshold
        self._recent_writes: deque[float] = deque()
        self._log_count = 0
        self._call_id = 0
        self._ensure_log_dir()

    def _ensure_log_dir(self) -> None:
        """Create log directory if it doesn't exist."""
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("Could not create log directory: %s", self._log_dir)

    def _append_log(self, entry: dict[str, Any]) -> None:
        """Append a JSON log entry to the tool_calls.jsonl file."""
        try:
            with self._log_file.open("a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except OSError:
            logger.warning("Could not write to log file: %s", self._log_file)

    def _truncate_log_if_needed(self) -> None:
        """Keep only the last MAX_LOG_LINES entries."""
        try:
            if not self._log_file.exists():
                return
            lines = self._log_file.read_text().splitlines()
            if len(lines) > MAX_LOG_LINES:
                self._log_file.write_text("\n".join(lines[-MAX_LOG_LINES:]) + "\n")
        except OSError:
            pass

    def _check_rapid_writes(self, now: float) -> str | None:
        """Check if we're in a rapid write sequence. Returns warning message or None."""
        self._recent_writes.append(now)

        # Prune old entries outside the window
        cutoff = now - self._rapid_window
        while self._recent_writes and self._recent_writes[0] < cutoff:
            self._recent_writes.popleft()

        if len(self._recent_writes) >= self._rapid_threshold:
            window = now - self._recent_writes[0]
            return (
                f"Rapid write sequence detected ({len(self._recent_writes)} writes "
                f"in {window:.1f}s) — this pattern is known to trigger client-side "
                f"transport hangs (see issue #65)"
            )
        return None

    async def on_call_tool(
        self,
        context: MiddlewareContext,
        call_next: CallNext,
    ) -> Any:
        """Intercept tool calls to log and detect rapid writes."""
        tool_name = getattr(context.message, "name", "unknown")
        is_write = tool_name in WRITE_TOOLS
        start = time.perf_counter()
        warning = None

        # Check for rapid writes before the call
        if is_write:
            warning = self._check_rapid_writes(time.time())
            if warning:
                logger.warning(warning)

        # Two-phase logging: write a "started" entry BEFORE the call so that
        # if the transport hangs and call_next never returns, we still have
        # evidence on disk. A "started" entry with no matching "completed"
        # entry for the same call_id = hung call.
        self._call_id += 1
        call_id = self._call_id
        self._append_log({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "call_id": call_id,
            "phase": "started",
            "tool": tool_name,
            "write": is_write,
            "warning": warning,
        })

        error = False
        cancelled = False
        try:
            result = await call_next(context)
        except BaseException:
            error = True
            # Detect cancellations — these are the most important to log
            # for debugging transport hangs (issue #65).
            cancelled = isinstance(sys.exc_info()[1], asyncio.CancelledError)
            raise
        else:
            duration_ms = (time.perf_counter() - start) * 1000

            # Check for slow writes
            if is_write and duration_ms > self._slow_threshold * 1000:
                slow_warning = (
                    f"Slow write operation: {tool_name} took {duration_ms:.0f}ms (threshold: {self._slow_threshold * 1000:.0f}ms)"
                )
                logger.warning(slow_warning)
                warning = f"{warning}; {slow_warning}" if warning else slow_warning

            return result
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            entry: dict[str, Any] = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "call_id": call_id,
                "phase": "completed",
                "tool": tool_name,
                "write": is_write,
                "duration_ms": round(duration_ms),
                "warning": warning,
            }
            if error:
                entry["error"] = True
            if cancelled:
                entry["cancelled"] = True
            self._append_log(entry)
            self._log_count += 1
            if self._log_count % 100 == 0:
                self._truncate_log_if_needed()
