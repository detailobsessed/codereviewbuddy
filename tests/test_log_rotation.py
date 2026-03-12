"""Tests for size-based log rotation and stale PID cleanup."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from codereviewbuddy.log_rotation import (
    DEFAULT_BACKUP_COUNT,
    DEFAULT_MAX_BYTES,
    cleanup_stale_pid_files,
    rotate_if_needed,
)


class TestRotateIfNeeded:
    def test_no_rotation_when_under_limit(self, tmp_path: Path):
        log = tmp_path / "test.jsonl"
        log.write_text("small\n", encoding="utf-8")
        assert rotate_if_needed(log) is False
        assert log.read_text(encoding="utf-8") == "small\n"

    def test_no_rotation_when_file_missing(self, tmp_path: Path):
        log = tmp_path / "missing.jsonl"
        assert rotate_if_needed(log) is False

    def test_rotates_when_over_limit(self, tmp_path: Path):
        log = tmp_path / "test.jsonl"
        log.write_text("x" * 200, encoding="utf-8")
        assert rotate_if_needed(log, max_bytes=100) is True
        assert not log.exists()  # original rotated away
        assert (tmp_path / "test.jsonl.1").read_text(encoding="utf-8") == "x" * 200

    def test_backup_chain(self, tmp_path: Path):
        log = tmp_path / "test.jsonl"

        # Create 3 rotations
        for i in range(3):
            log.write_text(f"round-{i}\n" + "x" * 200, encoding="utf-8")
            rotate_if_needed(log, max_bytes=100, backup_count=7)

        assert (tmp_path / "test.jsonl.1").read_text(encoding="utf-8").startswith("round-2")
        assert (tmp_path / "test.jsonl.2").read_text(encoding="utf-8").startswith("round-1")
        assert (tmp_path / "test.jsonl.3").read_text(encoding="utf-8").startswith("round-0")
        assert not log.exists()

    def test_oldest_backup_deleted(self, tmp_path: Path):
        log = tmp_path / "test.jsonl"

        # Fill up all backup slots (backup_count=2)
        for i in range(3):
            log.write_text(f"round-{i}\n" + "x" * 200, encoding="utf-8")
            rotate_if_needed(log, max_bytes=100, backup_count=2)

        # .1 and .2 should exist, .3 should not
        assert (tmp_path / "test.jsonl.1").exists()
        assert (tmp_path / "test.jsonl.2").exists()
        assert not (tmp_path / "test.jsonl.3").exists()
        # .1 should be the most recent
        assert (tmp_path / "test.jsonl.1").read_text(encoding="utf-8").startswith("round-2")

    def test_default_constants(self):
        assert DEFAULT_MAX_BYTES == 2 * 1024 * 1024
        assert DEFAULT_BACKUP_COUNT == 7

    def test_oserror_returns_false(self, tmp_path: Path):
        """Rotation on a non-writable path should fail gracefully."""
        log = tmp_path / "nonexistent" / "deep" / "test.jsonl"
        assert rotate_if_needed(log) is False


class TestCleanupStalePidFiles:
    def test_removes_stale_pid(self, tmp_path: Path):
        pid_file = tmp_path / "server.99999.pid"
        pid_file.write_text("999999", encoding="utf-8")  # Almost certainly not running
        removed = cleanup_stale_pid_files(tmp_path)
        assert removed == 1
        assert not pid_file.exists()

    def test_keeps_running_pid(self, tmp_path: Path):
        pid_file = tmp_path / f"server.{os.getppid()}.pid"
        pid_file.write_text(str(os.getpid()), encoding="utf-8")  # Our own PID — definitely running
        removed = cleanup_stale_pid_files(tmp_path)
        assert removed == 0
        assert pid_file.exists()

    def test_handles_invalid_pid_content(self, tmp_path: Path):
        pid_file = tmp_path / "server.123.pid"
        pid_file.write_text("not-a-number", encoding="utf-8")
        removed = cleanup_stale_pid_files(tmp_path)
        assert removed == 1
        assert not pid_file.exists()

    def test_empty_directory(self, tmp_path: Path):
        removed = cleanup_stale_pid_files(tmp_path)
        assert removed == 0

    def test_ignores_non_pid_files(self, tmp_path: Path):
        other = tmp_path / "tool_calls.jsonl"
        other.write_text("data", encoding="utf-8")
        removed = cleanup_stale_pid_files(tmp_path)
        assert removed == 0
        assert other.exists()
