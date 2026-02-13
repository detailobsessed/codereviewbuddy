"""Tests for the raw I/O tap diagnostic module."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

if TYPE_CHECKING:
    import pytest
    from pytest_mock import MockerFixture

from codereviewbuddy.io_tap import (
    _extract_jsonrpc_info,
    _log_entry,
    _should_enable_io_tap,
    _TappedBuffer,
    _TappedStream,
    install_io_tap,
)

# ---------------------------------------------------------------------------
# _log_entry
# ---------------------------------------------------------------------------


class TestExtractJsonrpcInfo:
    def test_extracts_numeric_id(self):
        info = _extract_jsonrpc_info('{"jsonrpc":"2.0","id":42,"method":"tools/call"}')
        assert info["rpc_id"] == "42"
        assert info["rpc_method"] == "tools/call"

    def test_extracts_string_id(self):
        info = _extract_jsonrpc_info('{"jsonrpc":"2.0","id":"abc","result":{}}')
        assert info["rpc_id"] == "abc"

    def test_no_id_or_method(self):
        info = _extract_jsonrpc_info('{"jsonrpc":"2.0"}')
        assert info == {}

    def test_response_without_method(self):
        info = _extract_jsonrpc_info('{"jsonrpc":"2.0","id":7,"result":{}}')
        assert info["rpc_id"] == "7"
        assert "rpc_method" not in info


class TestLogEntry:
    def test_writes_jsonl_entry(self, tmp_path: Path):
        log_file = tmp_path / "tap.jsonl"
        _log_entry(log_file, "stdin", b'{"jsonrpc":"2.0"}')
        lines = log_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["dir"] == "stdin"
        assert entry["direction"] == "stdin"
        assert entry["bytes"] == len(b'{"jsonrpc":"2.0"}')
        assert entry["line"] == '{"jsonrpc":"2.0"}'
        assert "ts" in entry
        assert "mono" in entry
        assert entry["phase"] == "data"

    def test_phase_and_extra(self, tmp_path: Path):
        log_file = tmp_path / "tap.jsonl"
        _log_entry(log_file, "stdout", b"payload", phase="write_done", extra={"written": 7})
        entry = json.loads(log_file.read_text(encoding="utf-8"))
        assert entry["phase"] == "write_done"
        assert entry["written"] == 7

    def test_extracts_jsonrpc_fields(self, tmp_path: Path):
        log_file = tmp_path / "tap.jsonl"
        _log_entry(log_file, "stdin", b'{"jsonrpc":"2.0","id":5,"method":"tools/call"}')
        entry = json.loads(log_file.read_text(encoding="utf-8"))
        assert entry["rpc_id"] == "5"
        assert entry["rpc_method"] == "tools/call"

    def test_skips_empty_data(self, tmp_path: Path):
        log_file = tmp_path / "tap.jsonl"
        _log_entry(log_file, "stdin", b"")
        assert not log_file.exists()

    def test_skips_whitespace_only_data(self, tmp_path: Path):
        log_file = tmp_path / "tap.jsonl"
        _log_entry(log_file, "stdin", b"   \n  ")
        assert not log_file.exists()

    def test_truncates_long_lines(self, tmp_path: Path):
        log_file = tmp_path / "tap.jsonl"
        _log_entry(log_file, "stdout", b"x" * 1000)
        entry = json.loads(log_file.read_text(encoding="utf-8"))
        assert len(entry["line"]) == 500

    def test_appends_multiple_entries(self, tmp_path: Path):
        log_file = tmp_path / "tap.jsonl"
        _log_entry(log_file, "stdin", b"line1")
        _log_entry(log_file, "stdout", b"line2")
        lines = log_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2

    def test_survives_write_error(self, tmp_path: Path):
        """OSError during write should not propagate."""
        bad_path = tmp_path / "nonexistent" / "deep" / "tap.jsonl"
        # Should not raise
        _log_entry(bad_path, "stdin", b"data")

    def test_handles_invalid_utf8(self, tmp_path: Path):
        log_file = tmp_path / "tap.jsonl"
        _log_entry(log_file, "stdin", b"\xff\xfe invalid")
        entry = json.loads(log_file.read_text(encoding="utf-8"))
        assert entry["dir"] == "stdin"
        assert entry["direction"] == "stdin"


# ---------------------------------------------------------------------------
# _TappedBuffer
# ---------------------------------------------------------------------------


class TestTappedBuffer:
    def test_readline_logs_and_returns(self, tmp_path: Path):
        log_file = tmp_path / "tap.jsonl"
        inner = io.BytesIO(b"hello\nworld\n")
        buf = _TappedBuffer(inner, "stdin", log_file)
        result = buf.readline()
        assert result == b"hello\n"
        assert log_file.exists()
        entry = json.loads(log_file.read_text(encoding="utf-8"))
        assert entry["dir"] == "stdin"

    def test_readline_empty_no_log(self, tmp_path: Path):
        log_file = tmp_path / "tap.jsonl"
        inner = io.BytesIO(b"")
        buf = _TappedBuffer(inner, "stdin", log_file)
        result = buf.readline()
        assert result == b""
        assert not log_file.exists()

    def test_read_logs_and_returns(self, tmp_path: Path):
        log_file = tmp_path / "tap.jsonl"
        inner = io.BytesIO(b"payload")
        buf = _TappedBuffer(inner, "stdin", log_file)
        result = buf.read()
        assert result == b"payload"
        assert log_file.exists()

    def test_read_empty_no_log(self, tmp_path: Path):
        log_file = tmp_path / "tap.jsonl"
        inner = io.BytesIO(b"")
        buf = _TappedBuffer(inner, "stdin", log_file)
        result = buf.read()
        assert result == b""
        assert not log_file.exists()

    def test_read1_logs_and_returns(self, tmp_path: Path):
        log_file = tmp_path / "tap.jsonl"
        inner = MagicMock()
        inner.read1.return_value = b"chunk"
        buf = _TappedBuffer(inner, "stdin", log_file)
        result = buf.read1(1024)
        assert result == b"chunk"
        assert log_file.exists()

    def test_read1_empty_no_log(self, tmp_path: Path):
        log_file = tmp_path / "tap.jsonl"
        inner = MagicMock()
        inner.read1.return_value = b""
        buf = _TappedBuffer(inner, "stdin", log_file)
        result = buf.read1(1024)
        assert result == b""
        assert not log_file.exists()

    def test_write_logs_two_phases(self, tmp_path: Path):
        log_file = tmp_path / "tap.jsonl"
        inner = io.BytesIO()
        buf = _TappedBuffer(inner, "stdout", log_file)
        n = buf.write(b"output")
        assert n == 6
        lines = log_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        start = json.loads(lines[0])
        done = json.loads(lines[1])
        assert start["phase"] == "write_start"
        assert start["dir"] == "stdout"
        assert done["phase"] == "write_done"
        assert done["written"] == 6
        assert done["mono"] >= start["mono"]

    def test_write_empty_no_log(self, tmp_path: Path):
        log_file = tmp_path / "tap.jsonl"
        inner = io.BytesIO()
        buf = _TappedBuffer(inner, "stdout", log_file)
        buf.write(b"")
        assert not log_file.exists()

    def test_flush_logs_two_phases(self, tmp_path: Path):
        log_file = tmp_path / "tap.jsonl"
        inner = MagicMock()
        buf = _TappedBuffer(inner, "stdout", log_file)
        buf.flush()
        inner.flush.assert_called_once()
        lines = log_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["phase"] == "flush_start"
        assert json.loads(lines[1])["phase"] == "flush_done"

    def test_getattr_delegates(self):
        inner = MagicMock()
        inner.name = "test_stream"
        buf = _TappedBuffer(inner, "stdin", Path("/dev/null"))
        assert buf.name == "test_stream"

    def test_setattr_delegates(self):
        inner = MagicMock()
        buf = _TappedBuffer(inner, "stdin", Path("/dev/null"))
        buf.custom_attr = "value"
        assert inner.custom_attr == "value"

    def test_iter_delegates(self):
        inner = MagicMock()
        inner.__iter__ = MagicMock(return_value=iter([b"a", b"b"]))
        buf = _TappedBuffer(inner, "stdin", Path("/dev/null"))
        assert list(buf) == [b"a", b"b"]

    def test_next_delegates(self):
        inner = MagicMock()
        inner.__next__ = MagicMock(return_value=b"line")
        buf = _TappedBuffer(inner, "stdin", Path("/dev/null"))
        assert next(buf) == b"line"


# ---------------------------------------------------------------------------
# _TappedStream
# ---------------------------------------------------------------------------


class TestTappedStream:
    def test_buffer_returns_tapped_buffer(self, tmp_path: Path):
        log_file = tmp_path / "tap.jsonl"
        stream = _TappedStream(sys.stdin, "stdin", log_file)
        assert isinstance(stream.buffer, _TappedBuffer)

    def test_buffer_is_same_instance(self, tmp_path: Path):
        log_file = tmp_path / "tap.jsonl"
        stream = _TappedStream(sys.stdin, "stdin", log_file)
        assert stream.buffer is stream.buffer  # same object

    def test_readline_logs_text(self, tmp_path: Path):
        log_file = tmp_path / "tap.jsonl"
        inner = io.StringIO("hello\nworld\n")
        # StringIO has no .buffer, so give it one
        inner.buffer = io.BytesIO(b"hello\nworld\n")  # type: ignore[attr-defined]
        stream = _TappedStream(inner, "stdin", log_file)
        result = stream.readline()
        assert result == "hello\n"
        assert log_file.exists()

    def test_readline_empty_no_log(self, tmp_path: Path):
        log_file = tmp_path / "tap.jsonl"
        inner = io.StringIO("")
        inner.buffer = io.BytesIO(b"")  # type: ignore[attr-defined]
        stream = _TappedStream(inner, "stdin", log_file)
        result = stream.readline()
        assert not result
        assert not log_file.exists()

    def test_read_logs_text(self, tmp_path: Path):
        log_file = tmp_path / "tap.jsonl"
        inner = io.StringIO("content")
        inner.buffer = io.BytesIO(b"content")  # type: ignore[attr-defined]
        stream = _TappedStream(inner, "stdin", log_file)
        result = stream.read()
        assert result == "content"
        assert log_file.exists()

    def test_read_empty_no_log(self, tmp_path: Path):
        log_file = tmp_path / "tap.jsonl"
        inner = io.StringIO("")
        inner.buffer = io.BytesIO(b"")  # type: ignore[attr-defined]
        stream = _TappedStream(inner, "stdin", log_file)
        result = stream.read()
        assert not result
        assert not log_file.exists()

    def test_write_logs_two_phases(self, tmp_path: Path):
        log_file = tmp_path / "tap.jsonl"
        inner = io.StringIO()
        inner.buffer = io.BytesIO()  # type: ignore[attr-defined]
        stream = _TappedStream(inner, "stdout", log_file)
        result = stream.write("output")
        assert result == 6
        lines = log_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["phase"] == "write_start"
        assert json.loads(lines[1])["phase"] == "write_done"
        assert json.loads(lines[1])["written"] == 6

    def test_write_empty_no_log(self, tmp_path: Path):
        log_file = tmp_path / "tap.jsonl"
        inner = io.StringIO()
        inner.buffer = io.BytesIO()  # type: ignore[attr-defined]
        stream = _TappedStream(inner, "stdout", log_file)
        stream.write("")
        assert not log_file.exists()

    def test_getattr_delegates(self):
        inner = MagicMock()
        inner.encoding = "utf-8"
        stream = _TappedStream(inner, "stdin", Path("/dev/null"))
        assert stream.encoding == "utf-8"

    def test_setattr_delegates(self):
        inner = MagicMock()
        stream = _TappedStream(inner, "stdin", Path("/dev/null"))
        stream.custom = "val"
        assert inner.custom == "val"

    def test_iter_delegates(self):
        inner = MagicMock()
        inner.__iter__ = MagicMock(return_value=iter(["a", "b"]))
        stream = _TappedStream(inner, "stdin", Path("/dev/null"))
        assert list(stream) == ["a", "b"]

    def test_next_delegates(self):
        inner = MagicMock()
        inner.__next__ = MagicMock(return_value="line")
        stream = _TappedStream(inner, "stdin", Path("/dev/null"))
        assert next(stream) == "line"


# ---------------------------------------------------------------------------
# install_io_tap
# ---------------------------------------------------------------------------


class TestShouldEnableIoTap:
    def test_env_var_1_enables(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CODEREVIEWBUDDY_IO_TAP", "1")
        assert _should_enable_io_tap() is True

    def test_env_var_0_disables(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CODEREVIEWBUDDY_IO_TAP", "0")
        assert _should_enable_io_tap() is False

    def test_env_var_overrides_config(self, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture):
        """Env var takes precedence even when config says True."""
        monkeypatch.setenv("CODEREVIEWBUDDY_IO_TAP", "0")
        from codereviewbuddy.config import Config, DiagnosticsConfig

        mock_config = Config(diagnostics=DiagnosticsConfig(io_tap=True))
        mocker.patch("codereviewbuddy.config.load_config", return_value=mock_config)
        assert _should_enable_io_tap() is False

    def test_config_enables_when_no_env_var(self, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture):
        monkeypatch.delenv("CODEREVIEWBUDDY_IO_TAP", raising=False)
        from codereviewbuddy.config import Config, DiagnosticsConfig

        mock_config = Config(diagnostics=DiagnosticsConfig(io_tap=True))
        mocker.patch("codereviewbuddy.config.load_config", return_value=mock_config)
        assert _should_enable_io_tap() is True

    def test_defaults_false_when_no_env_and_no_config(self, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture):
        monkeypatch.delenv("CODEREVIEWBUDDY_IO_TAP", raising=False)
        mocker.patch("codereviewbuddy.config.load_config", side_effect=FileNotFoundError)
        assert _should_enable_io_tap() is False


class TestInstallIoTap:
    def test_returns_false_when_env_not_set(self, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture):
        monkeypatch.delenv("CODEREVIEWBUDDY_IO_TAP", raising=False)
        mocker.patch("codereviewbuddy.config.load_config", side_effect=FileNotFoundError)
        assert install_io_tap() is False

    def test_returns_false_when_env_is_zero(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CODEREVIEWBUDDY_IO_TAP", "0")
        assert install_io_tap() is False

    def test_installs_tap_when_enabled(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        monkeypatch.setenv("CODEREVIEWBUDDY_IO_TAP", "1")
        monkeypatch.setattr("codereviewbuddy.io_tap.LOG_DIR", tmp_path)
        monkeypatch.setattr("codereviewbuddy.io_tap.LOG_FILE", tmp_path / "io_tap.jsonl")

        original_stdin = sys.stdin
        original_stdout = sys.stdout
        try:
            result = install_io_tap()
            assert result is True
            assert isinstance(sys.stdin, _TappedStream)
            assert isinstance(sys.stdout, _TappedStream)
        finally:
            sys.stdin = original_stdin
            sys.stdout = original_stdout

    def test_returns_false_on_mkdir_failure(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CODEREVIEWBUDDY_IO_TAP", "1")
        bad_dir = MagicMock()
        bad_dir.mkdir.side_effect = OSError("mocked")
        monkeypatch.setattr("codereviewbuddy.io_tap.LOG_DIR", bad_dir)
        assert install_io_tap() is False

    def test_restores_streams_on_failure(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mocker: MockerFixture):
        monkeypatch.setenv("CODEREVIEWBUDDY_IO_TAP", "1")
        monkeypatch.setattr("codereviewbuddy.io_tap.LOG_DIR", tmp_path)
        monkeypatch.setattr("codereviewbuddy.io_tap.LOG_FILE", tmp_path / "io_tap.jsonl")

        original_stdin = sys.stdin
        original_stdout = sys.stdout

        # Make _TappedStream raise on second call (stdout)
        call_count = 0
        original_tapped_stream = _TappedStream

        def failing_tapped_stream(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError
            return original_tapped_stream(*args, **kwargs)

        mocker.patch("codereviewbuddy.io_tap._TappedStream", side_effect=failing_tapped_stream)

        result = install_io_tap()
        assert result is False
        assert sys.stdin is original_stdin
        assert sys.stdout is original_stdout
