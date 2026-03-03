"""Single-instance enforcement for the codereviewbuddy MCP server.

When Windsurf restarts the server it sometimes spawns the new process before
killing the old one.  Both processes then write to the same stdout pipe, which
produces interleaved JSON-RPC frames that mcp-go cannot parse, causing it to
restart again — a self-reinforcing cascade (see GH issue #211).

``enforce_single_instance()`` is called once at server startup.  It:
1. Computes ``~/.codereviewbuddy/server.{ppid}.pid`` — one lock file per
   parent process (i.e. per Windsurf window), so multiple IDE windows can
   each run their own server without interfering with each other.
2. Reads the lock file (if it exists) and SIGTERMs the old process.
3. Writes our own PID to the file and returns the path.

Cleanup (``_remove_pid_file``) is called from the FastMCP lifespan ``finally``
block in ``server.py``, which runs on both clean shutdown and crashes.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_PID_DIR = Path.home() / ".codereviewbuddy"
_SIGTERM_WAIT_SECS = 0.5


def enforce_single_instance(pid_file: Path | None = None) -> Path:
    """Terminate any existing server process for this parent and claim the PID file.

    Returns the PID file path so the caller can pass it to ``_remove_pid_file``.
    """
    if pid_file is None:
        pid_file = _PID_DIR / f"server.{os.getppid()}.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)

    if pid_file.exists():
        _terminate_existing(pid_file)

    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    logger.debug("Single-instance lock acquired (pid=%d, ppid=%d, file=%s)", os.getpid(), os.getppid(), pid_file)
    return pid_file


def _terminate_existing(pid_file: Path) -> None:
    """Read the PID file and SIGTERM the old process if it is still running."""
    try:
        old_pid = int(pid_file.read_text(encoding="utf-8").strip())
    except ValueError, OSError:
        return

    if old_pid == os.getpid():
        return

    try:
        os.kill(old_pid, signal.SIGTERM)
        logger.info(
            "Sent SIGTERM to previous server process (pid=%d) — waiting %.1fs before taking over stdout",
            old_pid,
            _SIGTERM_WAIT_SECS,
        )
        time.sleep(_SIGTERM_WAIT_SECS)
    except ProcessLookupError:
        logger.debug("Previous server process (pid=%d) already exited", old_pid)
    except PermissionError:
        logger.warning(
            "Could not SIGTERM previous server process (pid=%d) — proceeding anyway; stdout corruption may occur",
            old_pid,
        )


def _remove_pid_file(pid_file: Path) -> None:
    """Remove the PID file on clean exit (atexit handler)."""
    try:
        if pid_file.exists() and pid_file.read_text(encoding="utf-8").strip() == str(os.getpid()):
            pid_file.unlink()
    except OSError:
        pass
