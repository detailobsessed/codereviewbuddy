"""Tests for triage_review_comments — actionable threads only (#96)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

from codereviewbuddy.models import CommentStatus, ReviewComment, ReviewThread
from codereviewbuddy.tools.comments import (
    _classify_action,
    _extract_title,
    _has_followup_without_issue,
    _has_owner_reply,
    triage_review_comments,
)

# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _thread(
    thread_id: str = "PRRT_1",
    pr_number: int = 42,
    reviewer: str = "ai-reviewer-a[bot]",
    body: str = "🔴 **Bug: Something is broken**\n\nDetails here.",
    author: str = "ai-reviewer-a[bot]",
    file: str = "src/main.py",
    line: int = 10,
    is_pr_review: bool = False,
    extra_comments: list[ReviewComment] | None = None,
) -> ReviewThread:
    comments = [
        ReviewComment(
            author=author,
            body=body,
            created_at=datetime(2026, 2, 6, 10, 0, tzinfo=UTC),
        ),
    ]
    if extra_comments:
        comments.extend(extra_comments)
    return ReviewThread(
        thread_id=thread_id,
        pr_number=pr_number,
        status=CommentStatus.UNRESOLVED,
        file=file,
        line=line,
        reviewer=reviewer,
        comments=comments,
        is_pr_review=is_pr_review,
    )


# ---------------------------------------------------------------------------
# Unit tests for helpers
# ---------------------------------------------------------------------------


class TestExtractTitle:
    def test_bug_format(self):
        assert _extract_title("🔴 **Bug: Missing pagination**\nDetails") == "Missing pagination"

    def test_info_format(self):
        assert _extract_title("📝 **Info: Consider refactoring**") == "Consider refactoring"

    def test_plain_bold(self):
        assert _extract_title("**Some title here**\nBody text") == "Some title here"

    def test_no_bold(self):
        assert not _extract_title("No bold text here")


class TestHasOwnerReply:
    def test_owner_present(self):
        thread = _thread(
            extra_comments=[
                ReviewComment(author="ichoosetoaccept", body="Fixed in abc123"),
            ]
        )
        assert _has_owner_reply(thread, frozenset(["ichoosetoaccept"])) is True

    def test_no_owner(self):
        thread = _thread()
        assert _has_owner_reply(thread, frozenset(["ichoosetoaccept"])) is False

    def test_different_human(self):
        thread = _thread(
            extra_comments=[
                ReviewComment(author="humandev", body="Will look into this"),
            ]
        )
        assert _has_owner_reply(thread, frozenset(["ichoosetoaccept"])) is False

    def test_custom_owner_login(self):
        thread = _thread(
            extra_comments=[
                ReviewComment(author="mybot", body="Addressed"),
            ]
        )
        assert _has_owner_reply(thread, frozenset(["mybot"])) is True


class TestHasFollowupWithoutIssue:
    def test_followup_without_issue(self):
        thread = _thread(
            extra_comments=[
                ReviewComment(author="ichoosetoaccept", body="Noted for followup"),
            ]
        )
        assert _has_followup_without_issue(thread, frozenset(["ichoosetoaccept"])) is True

    def test_followup_with_issue(self):
        thread = _thread(
            extra_comments=[
                ReviewComment(author="ichoosetoaccept", body="Tracked for later in #42"),
            ]
        )
        assert _has_followup_without_issue(thread, frozenset(["ichoosetoaccept"])) is False

    def test_no_followup(self):
        thread = _thread(
            extra_comments=[
                ReviewComment(author="ichoosetoaccept", body="Fixed in abc123"),
            ]
        )
        assert _has_followup_without_issue(thread, frozenset(["ichoosetoaccept"])) is False

    def test_non_owner_followup_ignored(self):
        thread = _thread(
            extra_comments=[
                ReviewComment(author="someone_else", body="Noted for followup"),
            ]
        )
        assert _has_followup_without_issue(thread, frozenset(["ichoosetoaccept"])) is False

    def test_issue_ref_in_separate_reply(self):
        """Regression: issue ref in a later comment should clear the followup flag."""
        thread = _thread(
            extra_comments=[
                ReviewComment(author="ichoosetoaccept", body="Noted for followup"),
                ReviewComment(author="ichoosetoaccept", body="Filed #99"),
            ]
        )
        assert _has_followup_without_issue(thread, frozenset(["ichoosetoaccept"])) is False


class TestClassifyAction:
    def test_bug_needs_fix(self):
        assert _classify_action("bug") == "fix"

    def test_flagged_needs_fix(self):
        assert _classify_action("flagged") == "fix"

    def test_warning_needs_reply(self):
        assert _classify_action("warning") == "reply"

    def test_info_needs_reply(self):
        assert _classify_action("info") == "reply"


# ---------------------------------------------------------------------------
# Integration tests for triage_review_comments
# ---------------------------------------------------------------------------


class TestTriageReviewComments:
    """Integration tests that mock list_review_comments and verify triage logic."""

    def _mock_list(self, mocker: MockerFixture, threads: list[ReviewThread]) -> AsyncMock:
        return mocker.patch(
            "codereviewbuddy.tools.comments._collect_inline_threads_only",
            new_callable=AsyncMock,
            return_value=threads,
        )

    async def test_unreplied_bug_needs_fix(self, mocker: MockerFixture):
        """Unreplied bug thread should appear with action='fix'."""
        bug = _thread(body="🔴 **Bug: Crash on startup**")
        self._mock_list(mocker, [bug])

        result = await triage_review_comments([42], repo="o/r")
        assert result.total == 1
        assert result.needs_fix == 1
        assert result.items[0].severity == "bug"
        assert result.items[0].action == "fix"
        assert result.items[0].title == "Crash on startup"

    async def test_unreplied_info_needs_reply(self, mocker: MockerFixture):
        """Unreplied info thread should appear with action='reply'."""
        info = _thread(body="📝 **Info: Consider refactoring**")
        self._mock_list(mocker, [info])

        result = await triage_review_comments([42], repo="o/r")
        assert result.total == 1
        assert result.needs_reply == 1
        assert result.items[0].severity == "info"
        assert result.items[0].action == "reply"

    async def test_replied_thread_excluded(self, mocker: MockerFixture):
        """Thread with an owner reply should not appear in triage."""
        replied = _thread(
            extra_comments=[
                ReviewComment(author="ichoosetoaccept", body="Fixed in abc123"),
            ]
        )
        self._mock_list(mocker, [replied])

        result = await triage_review_comments([42], repo="o/r", owner_logins=["ichoosetoaccept"])
        assert result.total == 0

    async def test_pr_review_excluded(self, mocker: MockerFixture):
        """PR-level reviews should not appear in triage."""
        pr_review = _thread(is_pr_review=True)
        self._mock_list(mocker, [pr_review])

        result = await triage_review_comments([42], repo="o/r")
        assert result.total == 0

    async def test_followup_without_issue_flagged(self, mocker: MockerFixture):
        """Owner reply with 'noted for followup' but no issue ref should appear as create_issue."""
        thread = _thread(
            extra_comments=[
                ReviewComment(author="ichoosetoaccept", body="Noted for followup"),
            ]
        )
        self._mock_list(mocker, [thread])

        result = await triage_review_comments([42], repo="o/r", owner_logins=["ichoosetoaccept"])
        assert result.total == 1
        assert result.needs_issue == 1
        assert result.items[0].action == "create_issue"

    async def test_followup_with_issue_excluded(self, mocker: MockerFixture):
        """Owner reply with 'noted for followup' AND issue ref should be excluded (already handled)."""
        thread = _thread(
            extra_comments=[
                ReviewComment(author="ichoosetoaccept", body="Tracked for later in #42"),
            ]
        )
        self._mock_list(mocker, [thread])

        result = await triage_review_comments([42], repo="o/r", owner_logins=["ichoosetoaccept"])
        assert result.total == 0

    async def test_sorted_by_severity(self, mocker: MockerFixture):
        """Items should be sorted bugs-first."""
        info = _thread(thread_id="PRRT_info", body="📝 **Info: Minor thing**")
        bug = _thread(thread_id="PRRT_bug", body="🔴 **Bug: Critical crash**")
        warning = _thread(thread_id="PRRT_warn", body="🟡 **Warning: Performance**")
        self._mock_list(mocker, [info, bug, warning])

        result = await triage_review_comments([42], repo="o/r")
        assert result.total == 3
        severities = [item.severity for item in result.items if item.action != "create_issue"]
        assert severities == ["bug", "warning", "info"]

    async def test_multiple_prs(self, mocker: MockerFixture):
        """Should triage across multiple PRs."""
        bug_42 = _thread(thread_id="PRRT_42", pr_number=42, body="🔴 **Bug: Issue A**")
        info_43 = _thread(thread_id="PRRT_43", pr_number=43, body="📝 **Info: Issue B**")

        mock = mocker.patch(
            "codereviewbuddy.tools.comments._collect_inline_threads_only",
            new_callable=AsyncMock,
            side_effect=[
                [bug_42],
                [info_43],
            ],
        )

        result = await triage_review_comments([42, 43], repo="o/r")
        assert result.total == 2
        assert result.needs_fix == 1
        assert result.needs_reply == 1
        assert mock.call_count == 2

    async def test_empty_pr_list(self):
        """Empty PR list should return empty triage."""
        result = await triage_review_comments([], repo="o/r")
        assert result.total == 0
        assert result.items == []

    async def test_custom_owner_logins(self, mocker: MockerFixture):
        """Custom owner_logins should be used for reply detection."""
        thread = _thread(
            extra_comments=[
                ReviewComment(author="mybot", body="Fixed"),
            ]
        )
        self._mock_list(mocker, [thread])

        # With no config and no param — no owner filtering, thread appears
        result = await triage_review_comments([42], repo="o/r")
        assert result.total == 1

        # With custom owner — thread should be excluded
        result = await triage_review_comments([42], repo="o/r", owner_logins=["mybot"])
        assert result.total == 0

    async def test_owner_logins_from_config(self, mocker: MockerFixture):
        """CRB_OWNER_LOGINS config should be used when owner_logins param is omitted."""
        from codereviewbuddy.config import Config, set_config

        thread = _thread(
            extra_comments=[
                ReviewComment(author="myagent", body="Fixed"),
            ]
        )
        self._mock_list(mocker, [thread])

        # Set config with owner_logins
        set_config(Config(owner_logins=["myagent"]))
        try:
            result = await triage_review_comments([42], repo="o/r")
            assert result.total == 0  # myagent reply detected as owner

            # Explicit param overrides config
            result = await triage_review_comments([42], repo="o/r", owner_logins=["someone_else"])
            assert result.total == 1  # myagent isn't "someone_else"

            # Explicit empty list disables filtering even when config has owners
            result = await triage_review_comments([42], repo="o/r", owner_logins=[])
            assert result.total == 1  # empty owners = no filtering
        finally:
            set_config(Config())  # Reset to defaults

    async def test_snippet_truncated(self, mocker: MockerFixture):
        """Snippet should be truncated to 200 chars."""
        long_body = "🔴 **Bug: Long issue**\n" + "x" * 300
        thread = _thread(body=long_body)
        self._mock_list(mocker, [thread])

        result = await triage_review_comments([42], repo="o/r")
        assert len(result.items[0].snippet) == 200
