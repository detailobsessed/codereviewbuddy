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


class TestClassifyAction:
    def test_bug_marker_returns_fix(self):
        assert _classify_action(_thread(body="🔴 **Bug: Missing null check**")) == "fix"

    def test_critical_keyword_returns_fix(self):
        assert _classify_action(_thread(body="**Critical: This will crash in production**")) == "fix"

    def test_flagged_marker_returns_fix(self):
        assert _classify_action(_thread(body="🚩 **Flagged: Security issue**")) == "fix"

    def test_info_marker_returns_acknowledge(self):
        assert _classify_action(_thread(body="📝 **Info: Consider using a constant**")) == "acknowledge"

    def test_nit_keyword_returns_acknowledge(self):
        assert _classify_action(_thread(body="**Nit: trailing whitespace**")) == "acknowledge"

    def test_style_keyword_returns_acknowledge(self):
        assert _classify_action(_thread(body="Style suggestion: use snake_case here")) == "acknowledge"

    def test_consider_keyword_returns_acknowledge(self):
        assert _classify_action(_thread(body="Consider extracting this into a helper")) == "acknowledge"

    def test_vague_comment_returns_ambiguous(self):
        assert _classify_action(_thread(body="This could be improved")) == "ambiguous"

    def test_empty_body_returns_ambiguous(self):
        assert _classify_action(_thread(body="")) == "ambiguous"

    def test_no_comments_returns_ambiguous(self):
        thread = _thread()
        thread.comments = []
        assert _classify_action(thread) == "ambiguous"


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


# ---------------------------------------------------------------------------
# Integration tests for triage_review_comments
# ---------------------------------------------------------------------------


class TestTriageReviewComments:
    """Integration tests that mock _get_inline_threads and verify triage logic."""

    def _mock_list(self, mocker: MockerFixture, threads: list[ReviewThread]) -> AsyncMock:
        return mocker.patch(
            "codereviewbuddy.tools.comments._get_inline_threads",
            new_callable=AsyncMock,
            return_value=threads,
        )

    async def test_unreplied_thread_appears(self, mocker: MockerFixture):
        """Unreplied thread should appear in triage."""
        bug = _thread(body="🔴 **Bug: Crash on startup**")
        self._mock_list(mocker, [bug])

        result = await triage_review_comments([42], repo="o/r")
        assert result.total == 1
        assert result.items[0].title == "Crash on startup"

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

    async def test_multiple_prs(self, mocker: MockerFixture):
        """Should triage across multiple PRs."""
        bug_42 = _thread(thread_id="PRRT_42", pr_number=42, body="🔴 **Bug: Issue A**")
        info_43 = _thread(thread_id="PRRT_43", pr_number=43, body="📝 **Info: Issue B**")

        mock = mocker.patch(
            "codereviewbuddy.tools.comments._get_inline_threads",
            new_callable=AsyncMock,
            side_effect=[
                [bug_42],
                [info_43],
            ],
        )

        result = await triage_review_comments([42, 43], repo="o/r")
        assert result.total == 2
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

    async def test_auto_detects_repo(self, mocker: MockerFixture):
        """When repo is omitted, auto-detect from cwd."""
        thread = _thread(body="**Fix this**")
        self._mock_list(mocker, [thread])
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("o", "r"))

        result = await triage_review_comments([42])
        assert result.total == 1

    async def test_empty_result_message(self, mocker: MockerFixture):
        """Empty triage should return a helpful message."""
        self._mock_list(mocker, [])

        result = await triage_review_comments([42], repo="o/r")
        assert result.total == 0
        assert "No actionable threads" in result.message


# ---------------------------------------------------------------------------
# Narrow integration test — mocks at API boundary only (#147)
# ---------------------------------------------------------------------------

GRAPHQL_RESPONSE_WITH_THREADS = {
    "data": {
        "repository": {
            "pullRequest": {
                "title": "Test PR",
                "url": "https://github.com/o/r/pull/42",
                "reviewThreads": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {
                            "id": "PRRT_inline1",
                            "isResolved": False,
                            "comments": {
                                "nodes": [
                                    {
                                        "author": {"login": "ai-reviewer[bot]"},
                                        "body": "**Bug: null check missing**\nDetails.",
                                        "createdAt": "2026-02-06T10:00:00Z",
                                        "path": "src/main.py",
                                        "line": 10,
                                    }
                                ]
                            },
                        },
                        {
                            "id": "PRRT_resolved",
                            "isResolved": True,
                            "comments": {
                                "nodes": [
                                    {
                                        "author": {"login": "ai-reviewer[bot]"},
                                        "body": "**Info: looks good**",
                                        "createdAt": "2026-02-06T10:00:00Z",
                                        "path": "src/ok.py",
                                        "line": 1,
                                    }
                                ]
                            },
                        },
                    ],
                },
            }
        }
    },
}


class TestTriageNarrowIntegration:
    """Integration test that mocks only at the API boundary (ISM-147).

    Exercises the real pipeline: triage → _get_inline_threads → _fetch_raw_threads
    + _parse_threads, with only inline review threads.
    """

    async def test_real_pipeline_filters_to_unresolved_inline(self, mocker: MockerFixture):
        mocker.patch(
            "codereviewbuddy.tools.comments.github_api.graphql",
            new_callable=AsyncMock,
            return_value=GRAPHQL_RESPONSE_WITH_THREADS,
        )

        result = await triage_review_comments([42], repo="o/r", owner_logins=[])

        # Only 1 unresolved inline thread — resolved one is filtered out
        assert result.total == 1

        ids = {item.thread_id for item in result.items}
        assert "PRRT_inline1" in ids  # unresolved inline
        assert "PRRT_resolved" not in ids  # resolved — filtered out

        # Verify title was extracted from bold text
        inline = result.items[0]
        assert inline.title == "null check missing"
        assert inline.file == "src/main.py"
        assert inline.line == 10
        # "Bug" keyword → action should be "fix"
        assert inline.action == "fix"
