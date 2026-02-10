"""Tests for WriteOperationMiddleware."""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from codereviewbuddy.middleware import WRITE_TOOLS, WriteOperationMiddleware


@pytest.fixture
def tmp_log_dir(tmp_path: Path) -> Path:
    """Provide a temporary log directory."""
    log_dir = tmp_path / ".codereviewbuddy"
    log_dir.mkdir()
    return log_dir


@pytest.fixture
def middleware(tmp_log_dir: Path) -> WriteOperationMiddleware:
    """Create a middleware instance with a temporary log directory."""
    return WriteOperationMiddleware(log_dir=tmp_log_dir)


class TestWriteToolClassification:
    def test_write_tools_are_classified(self):
        expected = {
            "resolve_comment",
            "resolve_stale_comments",
            "reply_to_comment",
            "request_rereview",
            "create_issue_from_comment",
            "update_pr_description",
        }
        assert expected == WRITE_TOOLS

    def test_read_tools_not_in_write_set(self):
        read_tools = {"list_review_comments", "list_stack_review_comments", "check_for_updates"}
        assert not read_tools & WRITE_TOOLS


class TestLogFile:
    def test_append_log_creates_file(self, middleware: WriteOperationMiddleware, tmp_log_dir: Path):
        middleware._append_log({"tool": "test", "write": False})
        log_file = tmp_log_dir / "tool_calls.jsonl"
        assert log_file.exists()
        lines = log_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["tool"] == "test"
        assert entry["write"] is False

    def test_append_log_multiple_entries(self, middleware: WriteOperationMiddleware, tmp_log_dir: Path):
        for i in range(5):
            middleware._append_log({"tool": f"tool-{i}"})
        log_file = tmp_log_dir / "tool_calls.jsonl"
        lines = log_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 5

    def test_truncate_keeps_last_n_lines(self, middleware: WriteOperationMiddleware, tmp_log_dir: Path):
        log_file = tmp_log_dir / "tool_calls.jsonl"
        # Write more than MAX_LOG_LINES
        with log_file.open("w", encoding="utf-8") as f:
            for i in range(1100):
                f.write(json.dumps({"i": i}) + "\n")
        middleware._truncate_log_if_needed()
        lines = log_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1000
        # Should keep the last entries
        assert json.loads(lines[0])["i"] == 100
        assert json.loads(lines[-1])["i"] == 1099

    def test_append_log_survives_missing_dir(self, tmp_path: Path):
        """If the log directory can't be created, middleware still works."""
        mw = WriteOperationMiddleware(log_dir=tmp_path / "nonexistent" / "deep" / "path")
        # Should not raise
        mw._append_log({"tool": "test"})


class TestRapidWriteDetection:
    def test_no_warning_below_threshold(self, middleware: WriteOperationMiddleware):
        now = time.time()
        for i in range(3):
            result = middleware._check_rapid_writes(now + i * 0.1)
        assert result is None

    def test_warning_at_threshold(self, middleware: WriteOperationMiddleware):
        now = time.time()
        for i in range(3):
            middleware._check_rapid_writes(now + i * 0.1)
        result = middleware._check_rapid_writes(now + 0.3)
        assert result is not None
        assert "Rapid write sequence detected" in result
        assert "issue #65" in result

    def test_warning_includes_count(self, middleware: WriteOperationMiddleware):
        now = time.time()
        for i in range(5):
            result = middleware._check_rapid_writes(now + i * 0.1)
        assert result is not None
        assert "5 writes" in result

    def test_old_entries_expire(self, middleware: WriteOperationMiddleware):
        now = time.time()
        # Fire 3 writes
        for i in range(3):
            middleware._check_rapid_writes(now + i * 0.1)
        # Wait beyond the window (2s)
        result = middleware._check_rapid_writes(now + 3.0)
        assert result is None  # Only 1 write in the current window

    def test_custom_threshold(self, tmp_log_dir: Path):
        mw = WriteOperationMiddleware(log_dir=tmp_log_dir, rapid_threshold=2, rapid_window=1.0)
        now = time.time()
        mw._check_rapid_writes(now)
        result = mw._check_rapid_writes(now + 0.1)
        assert result is not None
        assert "2 writes" in result


def _make_context(tool_name: str) -> MagicMock:
    """Create a mock MiddlewareContext with the given tool name."""
    ctx = MagicMock()
    ctx.message = MagicMock()
    ctx.message.name = tool_name
    return ctx


class TestTwoPhaseLogging:
    """Tests for two-phase (started/completed) logging.

    This is critical for debugging transport hangs (issue #65): if call_next
    never returns, we need the 'started' entry on disk as evidence.
    """

    async def test_started_entry_written_before_call_next(self, middleware: WriteOperationMiddleware, tmp_log_dir: Path):
        """Verify a 'started' entry exists on disk before call_next returns."""
        log_file = tmp_log_dir / "tool_calls.jsonl"
        entries_during_call: list[dict[str, Any]] = []

        async def call_next(_ctx: Any) -> list[Any]:
            # Snapshot the log file DURING the call — before call_next returns
            if log_file.exists():
                entries_during_call.extend(json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines())
            await asyncio.sleep(0)
            return []

        await middleware.on_call_tool(_make_context("list_review_comments"), call_next)  # type: ignore[arg-type]

        # During the call, there should have been exactly 1 entry: the started one
        assert len(entries_during_call) == 1
        assert entries_during_call[0]["phase"] == "started"
        assert entries_during_call[0]["tool"] == "list_review_comments"

        # After the call, there should be 2 entries: started + completed
        all_entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
        assert len(all_entries) == 2
        assert all_entries[0]["phase"] == "started"
        assert all_entries[1]["phase"] == "completed"
        assert all_entries[0]["call_id"] == all_entries[1]["call_id"]

    async def test_hung_call_leaves_only_started_entry(self, middleware: WriteOperationMiddleware, tmp_log_dir: Path):
        """Simulate a hung call — only 'started' should be on disk."""
        log_file = tmp_log_dir / "tool_calls.jsonl"

        hung = asyncio.Event()

        async def call_next_hangs(_ctx: Any) -> list[Any]:
            hung.set()
            await asyncio.sleep(999)  # simulate hang
            return []  # never reached

        task = asyncio.create_task(
            middleware.on_call_tool(_make_context("list_review_comments"), call_next_hangs)  # type: ignore[arg-type]
        )

        # Wait for the call to be in-flight
        await asyncio.wait_for(hung.wait(), timeout=2.0)
        # Give the started entry a moment to flush
        await asyncio.sleep(0.01)

        # The log should have exactly 1 entry: started (no completed)
        entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
        assert len(entries) == 1
        assert entries[0]["phase"] == "started"
        assert entries[0]["tool"] == "list_review_comments"

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_completed_entry_has_duration(self, middleware: WriteOperationMiddleware, tmp_log_dir: Path):
        """Verify the completed entry includes duration_ms."""
        log_file = tmp_log_dir / "tool_calls.jsonl"

        async def call_next(_ctx: Any) -> list[Any]:
            await asyncio.sleep(0.01)
            return []

        await middleware.on_call_tool(_make_context("resolve_comment"), call_next)  # type: ignore[arg-type]

        entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
        completed = [e for e in entries if e["phase"] == "completed"]
        assert len(completed) == 1
        assert "duration_ms" in completed[0]
        assert completed[0]["duration_ms"] >= 10  # at least 10ms from the sleep

    async def test_error_logged_with_both_phases(self, middleware: WriteOperationMiddleware, tmp_log_dir: Path):
        """Verify that errors still produce both started and completed entries."""
        log_file = tmp_log_dir / "tool_calls.jsonl"

        async def call_next_raises(_ctx: Any) -> list[Any]:
            await asyncio.sleep(0)
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            await middleware.on_call_tool(_make_context("resolve_comment"), call_next_raises)  # type: ignore[arg-type]

        entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
        assert len(entries) == 2
        assert entries[0]["phase"] == "started"
        assert entries[1]["phase"] == "completed"
        assert entries[1]["error"] is True
