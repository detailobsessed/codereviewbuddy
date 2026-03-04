"""Tests for single-instance enforcement (_instance.py)."""

from __future__ import annotations

import os
import signal
from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    from pathlib import Path

from codereviewbuddy._instance import _PID_DIR, _remove_pid_file, _terminate_existing, enforce_single_instance


class TestEnforceSingleInstance:
    def test_creates_pid_file(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "server.pid"
        enforce_single_instance(pid_file)
        assert pid_file.exists()
        assert int(pid_file.read_text(encoding="utf-8")) == os.getpid()

    def test_returns_pid_file_path(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "server.pid"
        result = enforce_single_instance(pid_file)
        assert result == pid_file

    def test_default_path_scoped_to_ppid(self, tmp_path: Path) -> None:
        expected = _PID_DIR / f"server.{os.getppid()}.pid"
        result = enforce_single_instance()
        try:
            assert result == expected
        finally:
            result.unlink(missing_ok=True)

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "nested" / "deep" / "server.pid"
        enforce_single_instance(pid_file)
        assert pid_file.exists()

    def test_overwrites_stale_pid_file(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "server.pid"
        pid_file.write_text("99999999", encoding="utf-8")
        enforce_single_instance(pid_file)
        assert int(pid_file.read_text(encoding="utf-8")) == os.getpid()

    def test_no_self_sigterm(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "server.pid"
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        with patch("os.kill") as mock_kill:
            enforce_single_instance(pid_file)
        mock_kill.assert_not_called()


class TestTerminateExisting:
    def test_sigterms_running_process(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "server.pid"
        pid_file.write_text("12345", encoding="utf-8")
        with patch("os.kill") as mock_kill, patch("time.sleep"):
            _terminate_existing(pid_file)
        mock_kill.assert_called_once_with(12345, signal.SIGTERM)

    def test_ignores_already_dead_process(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "server.pid"
        pid_file.write_text("12345", encoding="utf-8")
        with patch("os.kill", side_effect=ProcessLookupError), patch("time.sleep"):
            _terminate_existing(pid_file)

    def test_ignores_permission_error(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "server.pid"
        pid_file.write_text("12345", encoding="utf-8")
        with patch("os.kill", side_effect=PermissionError), patch("time.sleep"):
            _terminate_existing(pid_file)

    def test_ignores_invalid_pid_content(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "server.pid"
        pid_file.write_text("not-a-pid", encoding="utf-8")
        _terminate_existing(pid_file)

    def test_skips_own_pid(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "server.pid"
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        with patch("os.kill") as mock_kill:
            _terminate_existing(pid_file)
        mock_kill.assert_not_called()

    def test_missing_pid_file_is_noop(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "missing.pid"
        _terminate_existing(pid_file)


class TestRemovePidFile:
    def test_removes_own_pid_file(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "server.pid"
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        _remove_pid_file(pid_file)
        assert not pid_file.exists()

    def test_does_not_remove_another_pids_file(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "server.pid"
        pid_file.write_text("99999", encoding="utf-8")
        _remove_pid_file(pid_file)
        assert pid_file.exists()

    def test_missing_file_is_noop(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "missing.pid"
        _remove_pid_file(pid_file)
