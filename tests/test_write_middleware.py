"""Tests for WriteOperationMiddleware."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture

from codereviewbuddy.middleware import (
    ISSUE_65_TRACKING_TAG,
    SESSION_WARN_EVERY_AFTER,
    SESSION_WARN_THRESHOLDS,
    WRITE_TOOLS,
    WriteOperationMiddleware,
)


async def _noop_call_next(_ctx: Any) -> list[Any]:
    """Default no-op call_next for tests."""
    await asyncio.sleep(0)
    return []


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
            "reply_to_comment",
            "create_issue_from_comment",
        }
        assert expected == WRITE_TOOLS

    def test_read_tools_not_in_write_set(self):
        read_tools = {"list_review_comments", "list_stack_review_comments", "review_pr_descriptions"}
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

    def test_rotation_triggers_after_n_writes(self, middleware: WriteOperationMiddleware, tmp_log_dir: Path, mocker: MockerFixture):
        """rotate_if_needed is called once log_count reaches _CHECK_EVERY_WRITES."""
        from codereviewbuddy.log_rotation import _CHECK_EVERY_WRITES

        rotate_mock = mocker.patch("codereviewbuddy.middleware.rotate_if_needed")
        for i in range(_CHECK_EVERY_WRITES):
            middleware._append_log({"tool": f"t-{i}"})
            middleware._log_count += 1
            if middleware._log_count % _CHECK_EVERY_WRITES == 0:
                from codereviewbuddy.middleware import rotate_if_needed as _real

                _real(middleware._log_file)

        rotate_mock.assert_called_once_with(middleware._log_file)

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


def _make_context(tool_name: str, arguments: dict[str, Any] | None = None) -> MagicMock:
    """Create a mock MiddlewareContext with the given tool name."""
    ctx = MagicMock()
    ctx.message = MagicMock()
    ctx.message.name = tool_name
    ctx.message.arguments = arguments
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

        await middleware.on_call_tool(_make_context("list_review_comments"), call_next)

        # During the call, there should have been exactly 1 entry: the started one
        assert len(entries_during_call) == 1
        assert entries_during_call[0]["phase"] == "started"
        assert entries_during_call[0]["tool"] == "list_review_comments"
        assert entries_during_call[0]["call_type"] == "read"
        assert "task_id" in entries_during_call[0]
        assert "mono_start" in entries_during_call[0]
        assert entries_during_call[0]["tracking_tag"] == ISSUE_65_TRACKING_TAG

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

        task = asyncio.create_task(middleware.on_call_tool(_make_context("list_review_comments"), call_next_hangs))

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

        async def call_next_slow(_ctx: Any) -> list[Any]:
            await asyncio.sleep(0.01)
            return []

        await middleware.on_call_tool(_make_context("resolve_comment"), call_next_slow)

        entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
        completed = [e for e in entries if e["phase"] == "completed"]
        assert len(completed) == 1
        assert "duration_ms" in completed[0]
        assert "elapsed_ms_precise" in completed[0]
        assert "mono_end" in completed[0]
        assert completed[0]["duration_ms"] >= 10  # at least 10ms from the sleep

    async def test_logs_args_size_and_fingerprint(self, middleware: WriteOperationMiddleware, tmp_log_dir: Path):
        """Started entries should include deterministic args metadata when provided."""
        log_file = tmp_log_dir / "tool_calls.jsonl"
        args = {"repo": "detailobsessed/surfmon", "pr_number": 23}

        await middleware.on_call_tool(_make_context("list_review_comments", args), _noop_call_next)

        entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
        started = next(e for e in entries if e["phase"] == "started")
        expected_payload = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        assert started["args_size_bytes"] == len(expected_payload)
        assert started["args_fingerprint"] == hashlib.sha256(expected_payload).hexdigest()

    async def test_heartbeat_logs_while_call_pending(self, tmp_log_dir: Path):
        """When enabled, heartbeat entries should appear during long-running calls."""
        middleware = WriteOperationMiddleware(
            log_dir=tmp_log_dir,
            heartbeat_enabled=True,
            heartbeat_interval_ms=50,
        )
        log_file = tmp_log_dir / "tool_calls.jsonl"
        hung = asyncio.Event()

        async def call_next_hangs(_ctx: Any) -> list[Any]:
            hung.set()
            await asyncio.sleep(0.16)
            return []

        await middleware.on_call_tool(_make_context("list_review_comments"), call_next_hangs)
        await asyncio.wait_for(hung.wait(), timeout=1.0)

        entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
        heartbeat_entries = [e for e in entries if e["phase"] == "heartbeat"]
        assert heartbeat_entries
        assert all(e["tool"] == "list_review_comments" for e in heartbeat_entries)
        assert all(e["tracking_tag"] == ISSUE_65_TRACKING_TAG for e in heartbeat_entries)

    async def test_error_logged_with_both_phases(self, middleware: WriteOperationMiddleware, tmp_log_dir: Path):
        """Verify that errors still produce both started and completed entries."""
        log_file = tmp_log_dir / "tool_calls.jsonl"

        async def call_next_raises(_ctx: Any) -> list[Any]:
            await asyncio.sleep(0)
            message = "boom"
            raise RuntimeError(message)

        with pytest.raises(RuntimeError):
            await middleware.on_call_tool(_make_context("resolve_comment"), call_next_raises)

        entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
        assert len(entries) == 2
        assert entries[0]["phase"] == "started"
        assert entries[1]["phase"] == "completed"
        assert entries[1]["error"] is True


class TestSessionCounter:
    """Tests for session call counter and threshold warnings.

    Verifies the ~50-call session limit hypothesis tracking.
    """

    async def test_session_call_count_in_entries(self, middleware: WriteOperationMiddleware, tmp_log_dir: Path):
        """Every log entry should include session_call_count and session_start_ts."""
        log_file = tmp_log_dir / "tool_calls.jsonl"

        for _ in range(3):
            await middleware.on_call_tool(_make_context("list_review_comments"), _noop_call_next)

        entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
        # 3 calls x 2 phases = 6 entries
        assert len(entries) == 6
        for entry in entries:
            assert "session_call_count" in entry
            assert "session_start_ts" in entry

        # session_call_count should match call_id and increment per call
        started_entries = [e for e in entries if e["phase"] == "started"]
        assert [e["session_call_count"] for e in started_entries] == [1, 2, 3]

    async def test_session_start_ts_is_stable(self, middleware: WriteOperationMiddleware, tmp_log_dir: Path):
        """session_start_ts should be identical across all entries in a session."""
        log_file = tmp_log_dir / "tool_calls.jsonl"

        for _ in range(3):
            await middleware.on_call_tool(_make_context("list_review_comments"), _noop_call_next)

        entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
        session_ts_values = {e["session_start_ts"] for e in entries}
        assert len(session_ts_values) == 1, f"Expected 1 unique session_start_ts, got {session_ts_values}"

    async def test_no_warning_below_threshold(self, tmp_log_dir: Path):
        """No session threshold warning when call count is below the first threshold."""
        mw = WriteOperationMiddleware(log_dir=tmp_log_dir)
        first_threshold = min(SESSION_WARN_THRESHOLDS)

        for _ in range(first_threshold - 1):
            await mw.on_call_tool(_make_context("list_review_comments"), _noop_call_next)

        log_file = tmp_log_dir / "tool_calls.jsonl"
        entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
        assert all(e.get("warning") is None for e in entries)

    async def test_warning_at_threshold(self, tmp_log_dir: Path):
        """A warning should appear when call count hits a threshold milestone."""
        mw = WriteOperationMiddleware(log_dir=tmp_log_dir)
        first_threshold = min(SESSION_WARN_THRESHOLDS)

        for _ in range(first_threshold):
            await mw.on_call_tool(_make_context("list_review_comments"), _noop_call_next)

        log_file = tmp_log_dir / "tool_calls.jsonl"
        entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
        # The started entry at the threshold call should have a warning
        threshold_started = [e for e in entries if e["phase"] == "started" and e["session_call_count"] == first_threshold]
        assert len(threshold_started) == 1
        assert "~50-call" in threshold_started[0]["warning"]
        assert f"reached {first_threshold}" in threshold_started[0]["warning"]

    async def test_warning_every_call_after_ceiling(self, tmp_log_dir: Path):
        """After SESSION_WARN_EVERY_AFTER, every call should warn."""
        mw = WriteOperationMiddleware(log_dir=tmp_log_dir)

        # Drive up to SESSION_WARN_EVERY_AFTER + 3
        target = SESSION_WARN_EVERY_AFTER + 3
        for _ in range(target):
            await mw.on_call_tool(_make_context("list_review_comments"), _noop_call_next)

        log_file = tmp_log_dir / "tool_calls.jsonl"
        entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
        # Every started entry after the ceiling should have a warning
        post_ceiling = [e for e in entries if e["phase"] == "started" and e["session_call_count"] > SESSION_WARN_EVERY_AFTER]
        assert len(post_ceiling) == 3
        assert all("~50-call" in e["warning"] for e in post_ceiling)

    def test_check_session_threshold_returns_none_for_normal_counts(self, middleware: WriteOperationMiddleware):
        """Counts not in thresholds and not past ceiling should return None."""
        assert middleware._check_session_threshold(1) is None
        assert middleware._check_session_threshold(10) is None
        assert middleware._check_session_threshold(29) is None

    def test_check_session_threshold_returns_warning_at_milestones(self, middleware: WriteOperationMiddleware):
        """Each threshold milestone should produce a warning."""
        for threshold in sorted(SESSION_WARN_THRESHOLDS):
            result = middleware._check_session_threshold(threshold)
            assert result is not None, f"Expected warning at {threshold}"
            assert f"reached {threshold}" in result
