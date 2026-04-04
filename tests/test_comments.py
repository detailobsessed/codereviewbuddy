"""Tests for comment tools."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

from codereviewbuddy.gh import GhError
from codereviewbuddy.github_api import GitHubError
from codereviewbuddy.tools.comments import (
    _node_to_review_thread,
    _parse_threads,
    _strip_comment_body,
    get_thread,
    reply_to_comment,
)

# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------

SAMPLE_THREAD_NODE = {
    "id": "PRRT_kwDOtest123",
    "isResolved": False,
    "comments": {
        "nodes": [
            {
                "author": {"login": "ai-reviewer-a[bot]"},
                "body": "Consider adding error handling here.",
                "createdAt": "2026-02-06T10:00:00Z",
                "path": "src/codereviewbuddy/gh.py",
                "line": 42,
            }
        ]
    },
}

SAMPLE_RESOLVED_THREAD = {
    "id": "PRRT_kwDOresolved",
    "isResolved": True,
    "comments": {
        "nodes": [
            {
                "author": {"login": "ai-reviewer-b[bot]"},
                "body": "Looks good now.",
                "createdAt": "2026-02-06T11:00:00Z",
                "path": "main.py",
                "line": 5,
            }
        ]
    },
}

SAMPLE_GRAPHQL_RESPONSE = {
    "data": {
        "repository": {
            "pullRequest": {
                "title": "Test PR",
                "url": "https://github.com/owner/repo/pull/42",
                "reviewThreads": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [SAMPLE_THREAD_NODE, SAMPLE_RESOLVED_THREAD],
                },
            }
        }
    },
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestStripCommentBody:
    def test_removes_html_comments(self):
        body = "Before <!-- ai-reviewer-badge-begin --><img src='badge.svg'><!-- ai-reviewer-badge-end --> After"
        result = _strip_comment_body(body)
        assert "badge" not in result
        assert "Before" in result
        assert "After" in result

    def test_collapses_details_blocks(self):
        body = "Issue found\n<details><summary>Root cause analysis</summary>\nLong explanation here...</details>"
        result = _strip_comment_body(body)
        assert "[details: Root cause analysis]" in result
        assert "Long explanation" not in result

    def test_strips_html_tags(self):
        body = "<p>Some <strong>bold</strong> text</p>"
        result = _strip_comment_body(body)
        assert result == "Some bold text"

    def test_truncates_long_bodies(self):
        body = "x" * 3000
        result = _strip_comment_body(body)
        assert len(result) < 2100
        assert result.endswith("… [truncated]")

    def test_collapses_blank_lines(self):
        body = "Line 1\n\n\n\n\nLine 2"
        result = _strip_comment_body(body)
        assert result == "Line 1\n\nLine 2"

    def test_preserves_plain_markdown(self):
        body = "🔴 **Bug: missing null check**\n\n`foo.bar` can be None here."
        result = _strip_comment_body(body)
        assert result == body

    def test_empty_body(self):
        assert not _strip_comment_body("")


class TestParseThreads:
    def test_basic_parsing(self):
        threads = _parse_threads([SAMPLE_THREAD_NODE], pr_number=42)
        assert len(threads) == 1
        t = threads[0]
        assert t.thread_id == "PRRT_kwDOtest123"
        assert t.pr_number == 42
        assert t.status == "unresolved"
        assert t.file == "src/codereviewbuddy/gh.py"
        assert t.line == 42
        assert t.reviewer == "ai-reviewer-a[bot]"
        assert len(t.comments) == 1
        assert t.comments[0].author == "ai-reviewer-a[bot]"

    def test_empty_comments_skipped(self):
        node = {"id": "PRRT_empty", "isResolved": False, "comments": {"nodes": []}}
        threads = _parse_threads([node], pr_number=42)
        assert len(threads) == 0

    def test_resolved_status(self):
        threads = _parse_threads([SAMPLE_RESOLVED_THREAD], pr_number=42)
        assert threads[0].status == "resolved"
        assert threads[0].reviewer == "ai-reviewer-b[bot]"

    def test_null_author_does_not_crash(self):
        """Regression: author=null (ghost/deleted user) must not raise AttributeError."""
        node = {
            "id": "PRRT_ghost",
            "isResolved": False,
            "comments": {
                "nodes": [
                    {
                        "author": None,
                        "body": "Ghost comment",
                        "createdAt": "2026-02-07T10:00:00Z",
                        "path": "main.py",
                        "line": 1,
                    }
                ]
            },
        }
        threads = _parse_threads([node], pr_number=42)
        assert len(threads) == 1
        assert threads[0].comments[0].author == "unknown"


class TestNodeToReviewThread:
    """Tests for _node_to_review_thread — GraphQL node response parsing."""

    def test_inline_thread(self):
        node = {
            "__typename": "PullRequestReviewThread",
            "pullRequest": {"number": 42},
            **SAMPLE_THREAD_NODE,
        }
        thread = _node_to_review_thread(node, "PRRT_kwDOtest123")
        assert thread.thread_id == "PRRT_kwDOtest123"
        assert thread.pr_number == 42
        assert thread.reviewer == "ai-reviewer-a[bot]"
        assert thread.file == "src/codereviewbuddy/gh.py"
        assert thread.status == "unresolved"
        assert len(thread.comments) == 1

    def test_pr_review(self):
        node = {
            "__typename": "PullRequestReview",
            "id": "PRR_kwDOtest456",
            "state": "CHANGES_REQUESTED",
            "body": "3 issues found.",
            "author": {"login": "ai-reviewer-b[bot]"},
            "submittedAt": "2026-02-07T10:00:00Z",
            "url": "https://github.com/o/r/pull/42#pullrequestreview-123",
            "pullRequest": {"number": 42},
        }
        thread = _node_to_review_thread(node, "PRR_kwDOtest456")
        assert thread.thread_id == "PRR_kwDOtest456"
        assert thread.pr_number == 42
        assert thread.reviewer == "ai-reviewer-b[bot]"
        assert thread.status == "unresolved"
        assert thread.is_pr_review is True
        assert "3 issues found" in thread.comments[0].body

    def test_issue_comment(self):
        node = {
            "__typename": "IssueComment",
            "id": "IC_kwDOtest789",
            "body": "## Coverage Report\n95% coverage",
            "author": {"login": "codecov[bot]"},
            "createdAt": "2026-02-08T10:00:00Z",
            "url": "https://github.com/o/r/pull/42#issuecomment-123",
            "issue": {"number": 42},
        }
        thread = _node_to_review_thread(node, "IC_kwDOtest789")
        assert thread.thread_id == "IC_kwDOtest789"
        assert thread.pr_number == 42
        assert thread.reviewer == "codecov[bot]"
        assert thread.is_pr_review is True
        assert "Coverage Report" in thread.comments[0].body

    def test_unknown_typename_raises(self):
        node = {"__typename": "Unknown"}
        with pytest.raises(GhError, match="Unexpected node type"):
            _node_to_review_thread(node, "X_123")

    def test_empty_inline_thread_raises(self):
        node = {
            "__typename": "PullRequestReviewThread",
            "id": "PRRT_empty",
            "isResolved": False,
            "comments": {"nodes": []},
        }
        with pytest.raises(GhError, match="has no comments"):
            _node_to_review_thread(node, "PRRT_empty")


class TestGetThread:
    async def test_fetches_inline_thread(self, mocker: MockerFixture):
        response = {
            "data": {
                "node": {
                    "__typename": "PullRequestReviewThread",
                    "pullRequest": {"number": 42},
                    **SAMPLE_THREAD_NODE,
                },
            },
        }
        mocker.patch("codereviewbuddy.tools.comments.github_api.graphql", new_callable=AsyncMock, return_value=response)

        thread = await get_thread("PRRT_kwDOtest123")
        assert thread.thread_id == "PRRT_kwDOtest123"
        assert thread.pr_number == 42
        assert thread.reviewer == "ai-reviewer-a[bot]"

    async def test_not_found_raises(self, mocker: MockerFixture):
        response = {"data": {"node": None}}
        mocker.patch("codereviewbuddy.tools.comments.github_api.graphql", new_callable=AsyncMock, return_value=response)

        with pytest.raises(GhError, match="not found"):
            await get_thread("PRRT_nonexistent")

    async def test_graphql_error_raises(self, mocker: MockerFixture):
        mocker.patch(
            "codereviewbuddy.tools.comments.github_api.graphql",
            new_callable=AsyncMock,
            side_effect=GitHubError("GraphQL error: Could not resolve to a node"),
        )

        with pytest.raises(GitHubError, match="Could not resolve"):
            await get_thread("PRRT_bad")


class TestGraphQLErrorChecks:
    """Tests for GraphQL error propagation on query paths (#145)."""

    async def test_fetch_raw_threads_raises_on_graphql_error(self, mocker: MockerFixture):
        """_fetch_raw_threads should raise GitHubError when GraphQL call raises."""
        from codereviewbuddy.tools.comments import _fetch_raw_threads

        mocker.patch(
            "codereviewbuddy.tools.comments.github_api.graphql",
            new=AsyncMock(side_effect=GitHubError("Something went wrong")),
        )

        with pytest.raises(GitHubError, match="Something went wrong"):
            await _fetch_raw_threads("owner", "repo", 42, cwd=None, ctx=None)

    async def test_fetch_raw_threads_ok_without_errors(self, mocker: MockerFixture):
        """_fetch_raw_threads should work normally when no GraphQL errors."""
        from codereviewbuddy.tools.comments import _fetch_raw_threads

        mocker.patch(
            "codereviewbuddy.tools.comments.github_api.graphql",
            new_callable=AsyncMock,
            return_value=SAMPLE_GRAPHQL_RESPONSE,
        )

        result = await _fetch_raw_threads("owner", "repo", 42, cwd=None, ctx=None)
        assert len(result) == 2


class TestReplyToComment:
    async def test_reply_to_inline_thread(self, mocker: MockerFixture):
        """PRRT_ IDs should use GraphQL addPullRequestReviewThreadReply mutation."""
        # No get_repo_info mock needed — PRRT_ path short-circuits before repo lookup
        mock_graphql = mocker.patch(
            "codereviewbuddy.tools.comments.github_api.graphql",
            new_callable=AsyncMock,
            return_value={"data": {"addPullRequestReviewThreadReply": {"comment": {"id": "C_123"}}}},
        )

        result = await reply_to_comment(42, "PRRT_kwDOtest123", "looks good")

        assert "Replied to thread PRRT_kwDOtest123" in result
        assert "on PR #42" in result
        mock_graphql.assert_called_once()
        call_args = mock_graphql.call_args
        assert call_args.kwargs["variables"] == {"threadId": "PRRT_kwDOtest123", "body": "looks good"}
        assert "addPullRequestReviewThreadReply" in call_args.args[0]

    async def test_reply_to_inline_thread_without_pr_number(self, mocker: MockerFixture):
        """PRRT_ replies work without pr_number — regression for workspace detection bug."""
        mock_graphql = mocker.patch(
            "codereviewbuddy.tools.comments.github_api.graphql",
            new_callable=AsyncMock,
            return_value={"data": {"addPullRequestReviewThreadReply": {"comment": {"id": "C_456"}}}},
        )

        result = await reply_to_comment(None, "PRRT_kwDOtest123", "noted")

        assert "Replied to thread PRRT_kwDOtest123" in result
        assert "on PR #" not in result
        mock_graphql.assert_called_once()
        call_args = mock_graphql.call_args
        assert call_args.kwargs["variables"] == {"threadId": "PRRT_kwDOtest123", "body": "noted"}

    async def test_reply_to_unknown_prefix_raises(self):
        """Unknown thread ID prefixes should raise GhError, not silently fall through."""
        with pytest.raises(GhError, match="Unsupported thread ID prefix"):
            await reply_to_comment(42, "XYZ_unknownthread", "hello")

    async def test_reply_to_pr_review(self, mocker: MockerFixture):
        """PRR_ IDs should use the issues comments API."""
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))
        mock_rest = mocker.patch("codereviewbuddy.tools.comments.github_api.rest", new_callable=AsyncMock)

        result = await reply_to_comment(42, "PRR_kwDOtest456", "addressed in next PR")

        assert "Replied to PR-level review" in result
        mock_rest.assert_called_once_with(
            "/repos/owner/repo/issues/42/comments",
            method="POST",
            body="addressed in next PR",
        )

    async def test_reply_to_pr_review_with_explicit_repo(self, mocker: MockerFixture):
        """PRR_ with explicit repo should not call get_repo_info."""
        mock_rest = mocker.patch("codereviewbuddy.tools.comments.github_api.rest", new_callable=AsyncMock)

        result = await reply_to_comment(10, "PRR_kwDOtest789", "noted", repo="other/repo")

        assert "Replied to PR-level review" in result
        mock_rest.assert_called_once_with(
            "/repos/other/repo/issues/10/comments",
            method="POST",
            body="noted",
        )

    async def test_reply_to_bot_issue_comment(self, mocker: MockerFixture):
        """IC_ IDs (bot issue comments) should use the issues comments API."""
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))
        mock_rest = mocker.patch("codereviewbuddy.tools.comments.github_api.rest", new_callable=AsyncMock)

        result = await reply_to_comment(42, "IC_kwDOtest001", "thanks for the coverage report")

        assert "Replied to bot comment" in result
        mock_rest.assert_called_once_with(
            "/repos/owner/repo/issues/42/comments",
            method="POST",
            body="thanks for the coverage report",
        )

    async def test_inline_thread_graphql_error_raises(self, mocker: MockerFixture):
        """GitHubError when replying to PRRT_ should propagate."""
        mocker.patch(
            "codereviewbuddy.tools.comments.github_api.graphql",
            new=AsyncMock(side_effect=GitHubError("Could not resolve to a node")),
        )

        with pytest.raises(GitHubError, match="Could not resolve to a node"):
            await reply_to_comment(42, "PRRT_kwDObad", "test")
