"""Write-operation observability middleware for codereviewbuddy.

Logs all tool calls to ~/.codereviewbuddy/tool_calls.jsonl and detects
rapid write sequences that are known to trigger client-side transport
hangs (see issue #65).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
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
# Unique sentinel to grep/remove all temporary diagnostics once issue #65 is resolved.
ISSUE_65_TRACKING_TAG = "CRB-ISSUE-65-TRACKING"


class WriteOperationMiddleware(Middleware):
    """Middleware that tracks write operations and detects rapid-call patterns.

    - Logs every tool call to ~/.codereviewbuddy/tool_calls.jsonl
    - Detects rapid write sequences (4+ writes in 2s) and emits a warning
    - Warns on slow writes (>30s)
    """

    def __init__(  # noqa: PLR0913
        self,
        *,
        log_dir: Path | None = None,
        rapid_threshold: int = RAPID_WRITE_THRESHOLD,
        rapid_window: float = RAPID_WRITE_WINDOW_SECONDS,
        slow_threshold: float = 30.0,
        heartbeat_enabled: bool = False,
        heartbeat_interval_ms: int = 5000,
        include_args_fingerprint: bool = True,
    ) -> None:
        self._log_dir = log_dir or LOG_DIR
        self._log_file = self._log_dir / "tool_calls.jsonl"
        self._rapid_threshold = rapid_threshold
        self._rapid_window = rapid_window
        self._slow_threshold = slow_threshold
        self._heartbeat_enabled = heartbeat_enabled
        self._heartbeat_interval_ms = max(heartbeat_interval_ms, 100)
        self._include_args_fingerprint = include_args_fingerprint
        self._recent_writes: deque[float] = deque()
        self._log_count = 0
        self._call_id = 0
        self._ensure_log_dir()

    def configure_diagnostics(
        self,
        *,
        heartbeat_enabled: bool,
        heartbeat_interval_ms: int,
        include_args_fingerprint: bool,
    ) -> None:
        """Update runtime diagnostics settings from config."""
        self._heartbeat_enabled = heartbeat_enabled
        self._heartbeat_interval_ms = max(heartbeat_interval_ms, 100)
        self._include_args_fingerprint = include_args_fingerprint

    def _ensure_log_dir(self) -> None:
        """Create log directory if it doesn't exist."""
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("Could not create log directory: %s", self._log_dir)

    def _append_log(self, entry: dict[str, Any]) -> None:
        """Append a JSON log entry to the tool_calls.jsonl file."""
        try:
            with self._log_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except OSError:
            logger.warning("Could not write to log file: %s", self._log_file)

    def _truncate_log_if_needed(self) -> None:
        """Keep only the last MAX_LOG_LINES entries."""
        try:
            if not self._log_file.exists():
                return
            lines = self._log_file.read_text(encoding="utf-8").splitlines()
            if len(lines) > MAX_LOG_LINES:
                self._log_file.write_text("\n".join(lines[-MAX_LOG_LINES:]) + "\n", encoding="utf-8")
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

    @staticmethod
    def _serialize_args(arguments: Any) -> bytes | None:
        """Serialize tool arguments deterministically for size/fingerprint metadata."""
        if arguments is None:
            return None
        payload = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
        return payload.encode("utf-8")

    async def _heartbeat_loop(
        self,
        *,
        call_id: int,
        tool_name: str,
        call_type: str,
        task_id: int | None,
        start_mono: float,
    ) -> None:
        """Emit periodic in-flight heartbeat entries while a tool call is pending."""
        while True:
            await asyncio.sleep(self._heartbeat_interval_ms / 1000)
            now_mono = time.monotonic()
            self._append_log({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "call_id": call_id,
                "phase": "heartbeat",
                "tool": tool_name,
                "call_type": call_type,
                "task_id": task_id,
                "mono": now_mono,
                "inflight_ms": round((now_mono - start_mono) * 1000),
                "tracking_tag": ISSUE_65_TRACKING_TAG,
            })

    async def on_call_tool(  # noqa: PLR0912, PLR0914, PLR0915
        self,
        context: MiddlewareContext,
        call_next: CallNext,
    ) -> Any:
        """Intercept tool calls to log and detect rapid writes."""
        tool_name = getattr(context.message, "name", "unknown")
        is_write = tool_name in WRITE_TOOLS
        call_type = "write" if is_write else "read"
        current_task = asyncio.current_task()
        task_id = id(current_task) if current_task is not None else None
        start = time.perf_counter()
        start_mono = time.monotonic()
        warning = None
        heartbeat_task: asyncio.Task[None] | None = None
        args_size_bytes: int | None = None
        args_fingerprint: str | None = None

        serialized_args = self._serialize_args(getattr(context.message, "arguments", None))
        if serialized_args is not None:
            args_size_bytes = len(serialized_args)
            if self._include_args_fingerprint:
                args_fingerprint = hashlib.sha256(serialized_args).hexdigest()

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
        started_entry: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "call_id": call_id,
            "phase": "started",
            "tool": tool_name,
            "write": is_write,
            "call_type": call_type,
            "task_id": task_id,
            "mono": start_mono,
            "mono_start": start_mono,
            "warning": warning,
            "tracking_tag": ISSUE_65_TRACKING_TAG,
        }
        if args_size_bytes is not None:
            started_entry["args_size_bytes"] = args_size_bytes
        if args_fingerprint is not None:
            started_entry["args_fingerprint"] = args_fingerprint
        self._append_log(started_entry)

        if self._heartbeat_enabled:
            heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(
                    call_id=call_id,
                    tool_name=tool_name,
                    call_type=call_type,
                    task_id=task_id,
                    start_mono=start_mono,
                )
            )

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
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task

            duration_ms = (time.perf_counter() - start) * 1000
            end_mono = time.monotonic()
            entry: dict[str, Any] = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "call_id": call_id,
                "phase": "completed",
                "tool": tool_name,
                "write": is_write,
                "call_type": call_type,
                "task_id": task_id,
                "duration_ms": round(duration_ms),
                "elapsed_ms_precise": round(duration_ms, 3),
                "mono": end_mono,
                "mono_end": end_mono,
                "warning": warning,
                "tracking_tag": ISSUE_65_TRACKING_TAG,
            }
            if error:
                entry["error"] = True
            if cancelled:
                entry["cancelled"] = True
            self._append_log(entry)
            self._log_count += 1
            if self._log_count % 100 == 0:
                self._truncate_log_if_needed()
