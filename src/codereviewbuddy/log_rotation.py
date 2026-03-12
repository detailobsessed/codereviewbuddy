"""Size-based log rotation for diagnostic JSONL files.

Rotates ``foo.jsonl`` → ``foo.jsonl.1`` → … → ``foo.jsonl.N`` when the
active file exceeds *max_bytes*.  Oldest backup beyond *backup_count* is
deleted.  Safe to call from multiple concurrent processes — worst case a
few extra lines land before one process triggers the rename cascade.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_MAX_BYTES: int = 2 * 1024 * 1024  # 2 MB
DEFAULT_BACKUP_COUNT: int = 7
_CHECK_EVERY_WRITES: int = 50


def rotate_if_needed(
    log_path: Path,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
) -> bool:
    """Rotate *log_path* if it exceeds *max_bytes*.

    Returns ``True`` if a rotation was performed.
    """
    try:
        if not log_path.exists():
            return False
        if log_path.stat().st_size <= max_bytes:
            return False
        _do_rotate(log_path, backup_count)
    except OSError:
        logger.debug("Log rotation failed for %s", log_path, exc_info=True)
        return False
    else:
        return True


def _do_rotate(log_path: Path, backup_count: int) -> None:
    """Perform the actual file rename cascade."""
    # Delete the oldest backup if it exists
    oldest = Path(f"{log_path}.{backup_count}")
    if oldest.exists():
        oldest.unlink()

    # Shift backups: .6 → .7, .5 → .6, …, .1 → .2
    for i in range(backup_count - 1, 0, -1):
        src = Path(f"{log_path}.{i}")
        dst = Path(f"{log_path}.{i + 1}")
        if src.exists():
            src.rename(dst)

    # Rotate current file → .1
    log_path.rename(Path(f"{log_path}.1"))


def cleanup_stale_pid_files(log_dir: Path) -> int:
    """Remove ``server.*.pid`` files whose processes are no longer running.

    Returns the number of stale PID files removed.
    """
    removed = 0
    try:
        for pid_file in log_dir.glob("server.*.pid"):
            try:
                pid = int(pid_file.read_text(encoding="utf-8").strip())
                # os.kill(pid, 0) raises OSError if process doesn't exist
                os.kill(pid, 0)
            except ValueError, ProcessLookupError:
                pid_file.unlink(missing_ok=True)
                removed += 1
            except PermissionError:
                pass  # Process exists but we can't signal it
            except OSError:
                pid_file.unlink(missing_ok=True)
                removed += 1
    except OSError:
        logger.debug("Failed to clean up PID files in %s", log_dir, exc_info=True)
    return removed
