"""Raw I/O tap for stdio transport diagnostics.

Wraps sys.stdin and sys.stdout to log every JSON-RPC line at the byte
level, before the MCP library touches them. This provides a ground-truth
audit trail for diagnosing transport hangs (issue #65).

Logs are written to ~/.codereviewbuddy/io_tap.jsonl.
Enable via ``[diagnostics] io_tap = true`` in .codereviewbuddy.toml,
or the CODEREVIEWBUDDY_IO_TAP=1 environment variable (overrides config).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

IO_TAP_ENV = "CODEREVIEWBUDDY_IO_TAP"
LOG_DIR = Path.home() / ".codereviewbuddy"
LOG_FILE = LOG_DIR / "io_tap.jsonl"
# Unique sentinel to grep/remove temporary diagnostics once issue #65 is resolved.
ISSUE_65_TRACKING_TAG = "CRB-ISSUE-65-TRACKING"


_JSONRPC_ID_RE = re.compile(r'"id"\s*:\s*(\d+|"[^"]*")')
_JSONRPC_METHOD_RE = re.compile(r'"method"\s*:\s*"([^"]+)"')


def _extract_jsonrpc_info(text: str) -> dict[str, str | int]:
    """Extract JSON-RPC id and method from a line for correlation."""
    info: dict[str, str | int] = {}
    m = _JSONRPC_ID_RE.search(text)
    if m:
        info["rpc_id"] = m.group(1).strip('"')
    m = _JSONRPC_METHOD_RE.search(text)
    if m:
        info["rpc_method"] = m.group(1)

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        info["rpc_envelope"] = "parse_error"
        return info

    if not isinstance(payload, dict):
        return info

    has_method = "method" in payload
    has_id = "id" in payload
    has_result_or_error = "result" in payload or "error" in payload

    if has_method and has_id:
        info["rpc_envelope"] = "request"
    elif has_method and not has_id:
        info["rpc_envelope"] = "notification"
    elif has_result_or_error:
        info["rpc_envelope"] = "response"

    error_obj = payload.get("error")
    if isinstance(error_obj, dict) and isinstance(error_obj.get("code"), int):
        info["rpc_error_code"] = error_obj["code"]

    return info


def _log_entry(
    log_path: Path,
    direction: str,
    data: bytes,
    *,
    phase: str = "data",
    extra: dict[str, object] | None = None,
) -> None:
    """Append a single I/O event to the JSONL log file.

    Parameters
    ----------
    phase:
        One of "data" (legacy one-shot), "write_start", "write_done",
        "flush_start", "flush_done".
    extra:
        Additional fields merged into the log entry.
    """
    try:
        text = data.decode("utf-8", errors="replace").strip()
        if not text:
            return
        entry: dict[str, object] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "mono": time.monotonic(),
            "phase": phase,
            "dir": direction,
            "direction": direction,
            "bytes": len(data),
            "line": text[:500],
            "tracking_tag": ISSUE_65_TRACKING_TAG,
        }
        entry.update(_extract_jsonrpc_info(text))
        if extra:
            entry.update(extra)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        logger.debug("Failed to write I/O tap entry", exc_info=True)


class _TappedBuffer:
    """Transparent proxy for binary buffer streams (BufferedReader/BufferedWriter).

    MCP's stdio_server accesses sys.stdin.buffer and sys.stdout.buffer
    directly, wrapping them in a fresh TextIOWrapper. This class intercepts
    read/readline/write at the bytes level so the tap actually captures
    JSON-RPC traffic.
    """

    def __init__(self, wrapped: Any, direction: str, log_path: Path) -> None:
        object.__setattr__(self, "_wrapped", wrapped)
        object.__setattr__(self, "_direction", direction)
        object.__setattr__(self, "_log_path", log_path)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._wrapped, name, value)

    def __iter__(self) -> Any:
        return iter(self._wrapped)

    def __next__(self) -> Any:
        return next(self._wrapped)

    def readline(self, *args: Any, **kwargs: Any) -> bytes:
        result = self._wrapped.readline(*args, **kwargs)
        if result:
            _log_entry(self._log_path, self._direction, result)
        return result

    def read(self, *args: Any, **kwargs: Any) -> bytes:
        result = self._wrapped.read(*args, **kwargs)
        if result:
            _log_entry(self._log_path, self._direction, result)
        return result

    def read1(self, *args: Any, **kwargs: Any) -> bytes:
        result = self._wrapped.read1(*args, **kwargs)
        if result:
            _log_entry(self._log_path, self._direction, result)
        return result

    def write(self, data: Any) -> int:
        if data:
            raw = data if isinstance(data, bytes) else bytes(data)
            _log_entry(self._log_path, self._direction, raw, phase="write_start")
        result = self._wrapped.write(data)
        if data:
            raw = data if isinstance(data, bytes) else bytes(data)
            _log_entry(self._log_path, self._direction, raw, phase="write_done", extra={"written": result})
        return result

    def flush(self) -> None:
        _log_entry(self._log_path, self._direction, b"<flush>", phase="flush_start")
        self._wrapped.flush()
        _log_entry(self._log_path, self._direction, b"<flush>", phase="flush_done")


class _TappedStream:
    """Transparent proxy for text streams (sys.stdin / sys.stdout).

    Intercepts .buffer access to return a _TappedBuffer, ensuring the tap
    works even when MCP's stdio_server does TextIOWrapper(sys.stdin.buffer).
    Also intercepts text-level read/write for non-MCP consumers.
    """

    def __init__(self, wrapped: Any, direction: str, log_path: Path) -> None:
        object.__setattr__(self, "_wrapped", wrapped)
        object.__setattr__(self, "_direction", direction)
        object.__setattr__(self, "_log_path", log_path)
        # Pre-create the tapped buffer so .buffer always returns the same instance
        object.__setattr__(
            self,
            "_tapped_buffer",
            _TappedBuffer(wrapped.buffer, direction, log_path),
        )

    @property
    def buffer(self) -> _TappedBuffer:
        """Return tapped buffer — this is how MCP's stdio_server accesses I/O."""
        return self._tapped_buffer

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._wrapped, name, value)

    def __iter__(self) -> Any:
        return iter(self._wrapped)

    def __next__(self) -> Any:
        return next(self._wrapped)

    def readline(self, *args: Any, **kwargs: Any) -> Any:
        result = self._wrapped.readline(*args, **kwargs)
        if result:
            _log_entry(self._log_path, self._direction, result if isinstance(result, bytes) else result.encode("utf-8", errors="replace"))
        return result

    def read(self, *args: Any, **kwargs: Any) -> Any:
        result = self._wrapped.read(*args, **kwargs)
        if result:
            _log_entry(self._log_path, self._direction, result if isinstance(result, bytes) else result.encode("utf-8", errors="replace"))
        return result

    def write(self, data: Any) -> Any:
        if data:
            raw = data if isinstance(data, bytes) else data.encode("utf-8", errors="replace")
            _log_entry(self._log_path, self._direction, raw, phase="write_start")
        result = self._wrapped.write(data)
        if data:
            raw = data if isinstance(data, bytes) else data.encode("utf-8", errors="replace")
            _log_entry(self._log_path, self._direction, raw, phase="write_done", extra={"written": result})
        return result


def _should_enable_io_tap() -> bool:
    """Check if IO tap should be enabled (env var overrides config)."""
    env_val = os.environ.get(IO_TAP_ENV)
    if env_val is not None:
        return env_val == "1"

    try:
        from codereviewbuddy.config import load_config  # noqa: PLC0415

        return load_config().diagnostics.io_tap
    except Exception:
        return False


def install_io_tap() -> bool:
    """Install I/O tap on stdin/stdout if enabled via config or env var.

    Checks ``CODEREVIEWBUDDY_IO_TAP`` env var first (overrides config),
    then falls back to ``[diagnostics] io_tap`` in .codereviewbuddy.toml.

    Must be called BEFORE FastMCP starts the stdio transport.
    Returns True if the tap was installed, False otherwise.
    """
    if not _should_enable_io_tap():
        return False

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning("Failed to create I/O tap log directory: %s", LOG_DIR)
        return False

    import sys  # noqa: PLC0415

    original_stdin = sys.stdin
    original_stdout = sys.stdout
    try:
        sys.stdin = _TappedStream(sys.stdin, "stdin", LOG_FILE)
        sys.stdout = _TappedStream(sys.stdout, "stdout", LOG_FILE)
    except Exception:
        sys.stdin = original_stdin
        sys.stdout = original_stdout
        logger.exception("Failed to install I/O tap")
        return False
    else:
        logger.info("I/O tap installed, logging to %s", LOG_FILE)
        return True
