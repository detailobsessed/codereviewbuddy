"""Tests for stack_activity — chronological event feed (#98)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

from codereviewbuddy.tools.stack import (
    _parse_timeline_events,
    stack_activity,
)

# ---------------------------------------------------------------------------
# Unit tests for _parse_timeline_events
# ---------------------------------------------------------------------------


class TestParseTimelineEvents:
    def test_review_event(self):
        raw = [
            {
                "event": "reviewed",
                "submitted_at": "2026-02-13T10:30:00Z",
                "user": {"login": "devin-ai-integration[bot]"},
                "state": "CHANGES_REQUESTED",
            }
        ]
        events = _parse_timeline_events(raw, pr_number=42)
        assert len(events) == 1
        assert events[0].event_type == "review"
        assert events[0].actor == "devin-ai-integration[bot]"
        assert events[0].detail == "changes_requested"
        assert events[0].pr_number == 42

    def test_push_event(self):
        raw = [
            {
                "event": "head_ref_force_pushed",
                "created_at": "2026-02-13T10:32:00Z",
                "actor": {"login": "ichoosetoaccept"},
            }
        ]
        events = _parse_timeline_events(raw, pr_number=42)
        assert len(events) == 1
        assert events[0].event_type == "push"
        assert events[0].actor == "ichoosetoaccept"

    def test_commit_event(self):
        raw = [
            {
                "event": "committed",
                "committer": {"date": "2026-02-13T10:35:00Z", "name": "Alice"},
                "message": "fix: resolve bug in parser",
            }
        ]
        events = _parse_timeline_events(raw, pr_number=42)
        assert len(events) == 1
        assert events[0].event_type == "commit"
        assert events[0].detail == "fix: resolve bug in parser"

    def test_labeled_event(self):
        raw = [
            {
                "event": "labeled",
                "created_at": "2026-02-13T10:36:00Z",
                "actor": {"login": "ichoosetoaccept"},
                "label": {"name": "ready-to-merge"},
            }
        ]
        events = _parse_timeline_events(raw, pr_number=42)
        assert len(events) == 1
        assert events[0].event_type == "labeled"
        assert events[0].detail == "ready-to-merge"

    def test_unknown_events_skipped(self):
        raw = [
            {"event": "assigned", "created_at": "2026-02-13T10:00:00Z"},
            {"event": "milestoned", "created_at": "2026-02-13T10:01:00Z"},
        ]
        events = _parse_timeline_events(raw, pr_number=42)
        assert events == []

    def test_missing_timestamp_skipped(self):
        raw = [{"event": "reviewed", "user": {"login": "bot"}}]
        events = _parse_timeline_events(raw, pr_number=42)
        assert events == []

    def test_multiple_events_sorted_by_caller(self):
        """Events are returned in input order — caller sorts."""
        raw = [
            {
                "event": "reviewed",
                "submitted_at": "2026-02-13T10:32:00Z",
                "user": {"login": "devin"},
                "state": "APPROVED",
            },
            {
                "event": "head_ref_force_pushed",
                "created_at": "2026-02-13T10:30:00Z",
                "actor": {"login": "dev"},
            },
        ]
        events = _parse_timeline_events(raw, pr_number=42)
        assert len(events) == 2
        assert events[0].event_type == "review"
        assert events[1].event_type == "push"


# ---------------------------------------------------------------------------
# Integration tests for stack_activity
# ---------------------------------------------------------------------------


def _make_timeline_events(event_type: str, minutes_ago: int, actor: str = "dev") -> dict:
    """Create a raw timeline event dict."""
    from datetime import timedelta

    base_time = datetime(2026, 2, 13, 11, 0, tzinfo=UTC)
    ts = base_time - timedelta(minutes=minutes_ago)
    base = {"event": event_type, "created_at": ts.isoformat()}
    if event_type == "reviewed":
        base["submitted_at"] = ts.isoformat()
        base["user"] = {"login": actor}
        base["state"] = "APPROVED"
    elif event_type == "head_ref_force_pushed":
        base["actor"] = {"login": actor}
    elif event_type == "commented":
        base["user"] = {"login": actor}
    return base


class TestStackActivity:
    def _mock_timeline(self, mocker: MockerFixture, events_by_pr: dict[int, list[dict]]) -> None:
        def side_effect(_owner, _repo, pr_number, cwd=None):  # noqa: ARG001
            return events_by_pr.get(pr_number, [])

        mocker.patch(
            "codereviewbuddy.tools.stack._fetch_timeline",
            side_effect=side_effect,
        )

    async def test_single_pr_events(self, mocker: MockerFixture):
        events = [
            _make_timeline_events("head_ref_force_pushed", minutes_ago=30),
            _make_timeline_events("reviewed", minutes_ago=25, actor="devin"),
        ]
        self._mock_timeline(mocker, {42: events})

        result = await stack_activity(pr_numbers=[42], repo="o/r")
        assert len(result.events) == 2
        assert result.events[0].event_type == "push"
        assert result.events[1].event_type == "review"
        assert result.last_activity is not None

    async def test_multiple_prs_merged_and_sorted(self, mocker: MockerFixture):
        pr42_events = [_make_timeline_events("head_ref_force_pushed", minutes_ago=30)]
        pr43_events = [_make_timeline_events("reviewed", minutes_ago=25, actor="devin")]
        self._mock_timeline(mocker, {42: pr42_events, 43: pr43_events})

        result = await stack_activity(pr_numbers=[42, 43], repo="o/r")
        assert len(result.events) == 2
        # Should be chronologically sorted
        assert result.events[0].time < result.events[1].time
        assert result.events[0].pr_number == 42
        assert result.events[1].pr_number == 43

    async def test_settled_flag(self, mocker: MockerFixture):
        """Settled = push + review exist and >10 min since last activity."""
        events = [
            _make_timeline_events("head_ref_force_pushed", minutes_ago=30),
            _make_timeline_events("reviewed", minutes_ago=25, actor="devin"),
        ]
        self._mock_timeline(mocker, {42: events})

        result = await stack_activity(pr_numbers=[42], repo="o/r")
        assert result.settled is True
        assert result.minutes_since_last_activity is not None
        assert result.minutes_since_last_activity >= 10

    async def test_not_settled_without_review(self, mocker: MockerFixture):
        """Not settled if there's no review event."""
        events = [_make_timeline_events("head_ref_force_pushed", minutes_ago=30)]
        self._mock_timeline(mocker, {42: events})

        result = await stack_activity(pr_numbers=[42], repo="o/r")
        assert result.settled is False

    async def test_empty_pr_list(self):
        result = await stack_activity(pr_numbers=[], repo="o/r")
        assert result.error == "No PRs to fetch activity for"

    async def test_no_events(self, mocker: MockerFixture):
        self._mock_timeline(mocker, {42: []})

        result = await stack_activity(pr_numbers=[42], repo="o/r")
        assert result.events == []
        assert result.last_activity is None
        assert result.settled is False
