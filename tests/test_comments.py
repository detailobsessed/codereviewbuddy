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
    _check_graphql_errors,
    _get_pr_issue_comments,
    _get_pr_reviews,
    _parse_threads,
    _strip_comment_body,
    list_review_comments,
    list_stack_review_comments,
    reply_to_comment,
)

# ---------------------------------------------------------------------------
# GraphQL error checking
# ---------------------------------------------------------------------------


class TestCheckGraphqlErrors:
    def test_no_errors_passes(self):
        _check_graphql_errors({"data": {"node": {}}}, "test")

    def test_raises_on_errors(self):
        result = {"errors": [{"message": "Not found"}]}
        with pytest.raises(GhError, match="Not found"):
            _check_graphql_errors(result, "fetching threads")


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


class TestListReviewComments:
    @pytest.fixture(autouse=True)
    def _mock_gh(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.comments.github_api.graphql", new_callable=AsyncMock, return_value=SAMPLE_GRAPHQL_RESPONSE)
        mocker.patch(
            "codereviewbuddy.tools.comments.github_api.rest",
            new_callable=AsyncMock,
            side_effect=[
                [],  # _get_pr_reviews
                [],  # _get_pr_issue_comments
            ],
        )
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))
        mocker.patch("codereviewbuddy.tools.stack._fetch_open_prs", return_value=[])

    async def test_returns_all_threads(self):
        summary = await list_review_comments(42)
        assert len(summary.threads) == 2

    async def test_filter_unresolved(self):
        summary = await list_review_comments(42, status="unresolved")
        assert len(summary.threads) == 1
        assert summary.threads[0].status == "unresolved"

    async def test_filter_resolved(self):
        summary = await list_review_comments(42, status="resolved")
        assert len(summary.threads) == 1
        assert summary.threads[0].status == "resolved"

    async def test_explicit_repo(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.comments.github_api.graphql", new_callable=AsyncMock, return_value=SAMPLE_GRAPHQL_RESPONSE)
        mocker.patch(
            "codereviewbuddy.tools.comments.github_api.rest",
            new_callable=AsyncMock,
            side_effect=[[], []],
        )
        summary = await list_review_comments(42, repo="myorg/myrepo")
        assert len(summary.threads) == 2

    async def test_discover_stack_failure_preserves_threads(self, mocker: MockerFixture):
        """Regression: discover_stack failure must not discard fetched thread data."""
        mocker.patch("codereviewbuddy.tools.comments.github_api.graphql", new_callable=AsyncMock, return_value=SAMPLE_GRAPHQL_RESPONSE)
        mocker.patch(
            "codereviewbuddy.tools.comments.github_api.rest",
            new_callable=AsyncMock,
            side_effect=[[], []],
        )
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))
        mocker.patch("codereviewbuddy.tools.stack._fetch_open_prs", side_effect=RuntimeError("network error"))

        summary = await list_review_comments(42)
        assert len(summary.threads) == 2  # threads preserved despite stack failure
        assert summary.stack == []  # stack gracefully empty


class TestNonExistentPR:
    async def test_list_comments_returns_empty_for_null_pr(self, mocker: MockerFixture):
        """Regression: pullRequest=null must not crash with AttributeError."""
        null_pr_response = {
            "data": {"repository": {"pullRequest": None}},
        }
        mocker.patch(
            "codereviewbuddy.tools.comments.github_api.graphql",
            new_callable=AsyncMock,
            return_value=null_pr_response,
        )
        mocker.patch(
            "codereviewbuddy.tools.comments.github_api.rest",
            new_callable=AsyncMock,
            side_effect=[[], []],
        )
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))
        mocker.patch("codereviewbuddy.tools.stack._fetch_open_prs", return_value=[])

        summary = await list_review_comments(42)
        assert summary.threads == []


class TestGetPrReviews:
    """Tests for _get_pr_reviews — PR-level review summaries from AI reviewers."""

    async def test_returns_pr_level_review(self, mocker: MockerFixture):
        reviews = [
            {
                "node_id": "PRR_bot_123",
                "user": {"login": "ai-reviewer-a[bot]"},
                "state": "COMMENTED",
                "body": "**AI Review** found 2 potential issues.",
                "submitted_at": "2026-02-07T10:00:00Z",
            },
        ]
        mocker.patch("codereviewbuddy.tools.comments.github_api.rest", new_callable=AsyncMock, return_value=reviews)

        result = await _get_pr_reviews("owner", "repo", 42)
        assert len(result) == 1
        assert result[0].reviewer == "ai-reviewer-a[bot]"
        assert result[0].thread_id == "PRR_bot_123"
        assert result[0].file is None
        assert result[0].line is None
        assert result[0].status == "unresolved"
        assert "2 potential issues" in result[0].comments[0].body

        assert result[0].is_pr_review is True

    async def test_returns_second_pr_level_review(self, mocker: MockerFixture):
        reviews = [
            {
                "node_id": "PRR_bot_456",
                "user": {"login": "ai-reviewer-b[bot]"},
                "state": "COMMENTED",
                "body": "2 issues found.",
                "submitted_at": "2026-02-07T09:00:00Z",
            },
        ]
        mocker.patch("codereviewbuddy.tools.comments.github_api.rest", new_callable=AsyncMock, return_value=reviews)

        result = await _get_pr_reviews("owner", "repo", 42)
        assert len(result) == 1
        assert result[0].reviewer == "ai-reviewer-b[bot]"

    async def test_includes_all_non_empty_reviews(self, mocker: MockerFixture):
        reviews = [
            {
                "node_id": "PRR_human",
                "user": {"login": "humanuser"},
                "state": "APPROVED",
                "body": "LGTM!",
                "submitted_at": "2026-02-07T11:00:00Z",
            },
        ]
        mocker.patch("codereviewbuddy.tools.comments.github_api.rest", new_callable=AsyncMock, return_value=reviews)

        result = await _get_pr_reviews("owner", "repo", 42)
        assert len(result) == 1
        assert result[0].reviewer == "humanuser"

    async def test_skips_empty_bodies(self, mocker: MockerFixture):
        reviews = [
            {
                "node_id": "PRR_empty",
                "user": {"login": "ai-reviewer-a[bot]"},
                "state": "COMMENTED",
                "body": "",
                "submitted_at": "2026-02-07T09:00:00Z",
            },
        ]
        mocker.patch("codereviewbuddy.tools.comments.github_api.rest", new_callable=AsyncMock, return_value=reviews)

        result = await _get_pr_reviews("owner", "repo", 42)
        assert result == []

    async def test_maps_approved_to_resolved(self, mocker: MockerFixture):
        reviews = [
            {
                "node_id": "PRR_approved",
                "user": {"login": "ai-reviewer-a[bot]"},
                "state": "APPROVED",
                "body": "No Issues Found",
                "submitted_at": "2026-02-07T10:00:00Z",
            },
        ]
        mocker.patch("codereviewbuddy.tools.comments.github_api.rest", new_callable=AsyncMock, return_value=reviews)

        result = await _get_pr_reviews("owner", "repo", 42)
        assert len(result) == 1
        assert result[0].status == "resolved"

    async def test_maps_changes_requested_to_unresolved(self, mocker: MockerFixture):
        reviews = [
            {
                "node_id": "PRR_changes",
                "user": {"login": "ai-reviewer-b[bot]"},
                "state": "CHANGES_REQUESTED",
                "body": "3 issues found.",
                "submitted_at": "2026-02-07T10:00:00Z",
            },
        ]
        mocker.patch("codereviewbuddy.tools.comments.github_api.rest", new_callable=AsyncMock, return_value=reviews)

        result = await _get_pr_reviews("owner", "repo", 42)
        assert len(result) == 1
        assert result[0].status == "unresolved"

    async def test_handles_empty_response(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.comments.github_api.rest", new_callable=AsyncMock, return_value=None)

        result = await _get_pr_reviews("owner", "repo", 42)
        assert result == []

    async def test_handles_null_user(self, mocker: MockerFixture):
        reviews = [
            {
                "node_id": "PRR_null_user",
                "user": None,
                "state": "COMMENTED",
                "body": "Some review",
                "submitted_at": "2026-02-07T10:00:00Z",
            },
        ]
        mocker.patch("codereviewbuddy.tools.comments.github_api.rest", new_callable=AsyncMock, return_value=reviews)

        result = await _get_pr_reviews("owner", "repo", 42)
        assert len(result) == 1
        assert result[0].reviewer == "unknown"

    async def test_passes_paginate_flag(self, mocker: MockerFixture):
        """_get_pr_reviews must use paginate=True (#111)."""
        mock_rest = mocker.patch("codereviewbuddy.tools.comments.github_api.rest", new_callable=AsyncMock, return_value=[])
        await _get_pr_reviews("owner", "repo", 42)
        mock_rest.assert_called_once_with(
            "/repos/owner/repo/pulls/42/reviews?per_page=100",
            paginate=True,
        )


class TestListIncludesPrReviews:
    """Tests that list_review_comments includes PR-level reviews."""

    async def test_includes_pr_reviews_alongside_threads(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.comments.github_api.graphql", new_callable=AsyncMock, return_value=SAMPLE_GRAPHQL_RESPONSE)
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))
        mocker.patch("codereviewbuddy.tools.stack._fetch_open_prs", return_value=[])
        mocker.patch(
            "codereviewbuddy.tools.comments.github_api.rest",
            new_callable=AsyncMock,
            side_effect=[
                # _get_pr_reviews call
                [
                    {
                        "node_id": "PRR_bot",
                        "user": {"login": "ai-reviewer-a[bot]"},
                        "state": "COMMENTED",
                        "body": "2 potential issues found.",
                        "submitted_at": "2026-02-07T10:00:00Z",
                    },
                ],
                # _get_pr_issue_comments call
                [],
            ],
        )

        summary = await list_review_comments(42)
        # 2 inline threads + 1 PR review
        assert len(summary.threads) == 3
        pr_reviews = [t for t in summary.threads if t.file is None]
        assert len(pr_reviews) == 1
        assert pr_reviews[0].reviewer == "ai-reviewer-a[bot]"


class TestThreadsPagination:
    async def test_fetches_multiple_pages(self, mocker: MockerFixture):
        page1 = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "title": "Test PR",
                        "url": "https://github.com/owner/repo/pull/42",
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": True, "endCursor": "cursor_abc"},
                            "nodes": [SAMPLE_THREAD_NODE],
                        },
                    }
                }
            },
        }
        page2 = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "title": "Test PR",
                        "url": "https://github.com/owner/repo/pull/42",
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [SAMPLE_RESOLVED_THREAD],
                        },
                    }
                }
            },
        }
        mock_graphql = mocker.patch(
            "codereviewbuddy.tools.comments.github_api.graphql",
            new_callable=AsyncMock,
            side_effect=[page1, page2],
        )
        mocker.patch(
            "codereviewbuddy.tools.comments.github_api.rest",
            new_callable=AsyncMock,
            side_effect=[[], []],
        )
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))
        mocker.patch("codereviewbuddy.tools.stack._fetch_open_prs", return_value=[])

        summary = await list_review_comments(42)
        assert len(summary.threads) == 2
        # Verify cursor was passed on second call
        second_call = mock_graphql.call_args_list[1]
        variables = second_call.kwargs.get("variables", {})
        assert variables.get("cursor") == "cursor_abc"

    async def test_stops_on_null_end_cursor(self, mocker: MockerFixture):
        """Regression: hasNextPage=true + endCursor=null must not infinite-loop."""
        malformed_page = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "title": "Test PR",
                        "url": "https://github.com/owner/repo/pull/42",
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": True, "endCursor": None},
                            "nodes": [SAMPLE_THREAD_NODE],
                        },
                    }
                }
            },
        }
        mocker.patch(
            "codereviewbuddy.tools.comments.github_api.graphql",
            new_callable=AsyncMock,
            return_value=malformed_page,
        )
        mocker.patch(
            "codereviewbuddy.tools.comments.github_api.rest",
            new_callable=AsyncMock,
            side_effect=[[], []],
        )
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))
        mocker.patch("codereviewbuddy.tools.stack._fetch_open_prs", return_value=[])

        summary = await list_review_comments(42)
        assert len(summary.threads) == 1


# ---------------------------------------------------------------------------
# list_stack_review_comments
# ---------------------------------------------------------------------------


class TestListStackReviewComments:
    """Tests for list_stack_review_comments."""

    async def test_returns_summaries_grouped_by_pr(self, mocker: MockerFixture):
        """Should call list_review_comments for each PR and group results."""
        from codereviewbuddy.models import CommentStatus, ReviewSummary, ReviewThread

        thread_10 = ReviewThread(
            thread_id="PRRT_10",
            pr_number=10,
            status=CommentStatus.UNRESOLVED,
            file="a.py",
            line=1,
            reviewer="ai-reviewer-a[bot]",
            comments=[],
        )
        thread_11 = ReviewThread(
            thread_id="PRRT_11",
            pr_number=11,
            status=CommentStatus.RESOLVED,
            file="b.py",
            line=5,
            reviewer="ai-reviewer-b[bot]",
            comments=[],
        )
        summary_10 = ReviewSummary(threads=[thread_10])
        summary_11 = ReviewSummary(threads=[thread_11])
        summary_12 = ReviewSummary()

        mock_list = mocker.patch(
            "codereviewbuddy.tools.comments.list_review_comments",
            new_callable=AsyncMock,
            side_effect=[summary_10, summary_11, summary_12],
        )

        result = await list_stack_review_comments([10, 11, 12], repo="owner/repo")

        assert list(result.keys()) == [10, 11, 12]
        assert result[10].threads == [thread_10]
        assert result[11].threads == [thread_11]
        assert result[12].threads == []
        assert mock_list.call_count == 3

    async def test_passes_status_filter(self, mocker: MockerFixture):
        """Should forward status filter to each list_review_comments call."""
        from codereviewbuddy.models import ReviewSummary

        mock_list = mocker.patch(
            "codereviewbuddy.tools.comments.list_review_comments",
            new_callable=AsyncMock,
            return_value=ReviewSummary(),
        )

        await list_stack_review_comments([10, 11], repo="owner/repo", status="unresolved")

        for call in mock_list.call_args_list:
            assert call.kwargs["status"] == "unresolved"

    async def test_empty_pr_list(self, mocker: MockerFixture):
        """Should return empty dict for empty input."""
        mocker.patch("codereviewbuddy.tools.comments.list_review_comments", new_callable=AsyncMock)

        result = await list_stack_review_comments([], repo="owner/repo")

        assert result == {}


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


class TestGetPrIssueComments:
    async def test_returns_bot_comments(self, mocker: MockerFixture):
        """Bot comments (type=Bot or [bot] suffix) should be included."""
        mocker.patch(
            "codereviewbuddy.tools.comments.github_api.rest",
            new_callable=AsyncMock,
            return_value=[
                {
                    "node_id": "IC_kwDOtest001",
                    "user": {"login": "codecov[bot]", "type": "Bot"},
                    "body": "## Coverage Report\n\nAll files 95%",
                    "created_at": "2026-02-08T10:00:00Z",
                },
            ],
        )

        result = await _get_pr_issue_comments("owner", "repo", 42)

        assert len(result) == 1
        assert result[0].thread_id == "IC_kwDOtest001"
        assert result[0].reviewer == "codecov[bot]"
        assert result[0].is_pr_review is True
        assert "Coverage Report" in result[0].comments[0].body

    async def test_skips_human_comments(self, mocker: MockerFixture):
        """Non-bot comments should be excluded."""
        mocker.patch(
            "codereviewbuddy.tools.comments.github_api.rest",
            new_callable=AsyncMock,
            return_value=[
                {
                    "node_id": "IC_kwDOtest002",
                    "user": {"login": "humanuser", "type": "User"},
                    "body": "LGTM",
                    "created_at": "2026-02-08T10:00:00Z",
                },
            ],
        )

        result = await _get_pr_issue_comments("owner", "repo", 42)
        assert result == []

    async def test_skips_empty_bodies(self, mocker: MockerFixture):
        mocker.patch(
            "codereviewbuddy.tools.comments.github_api.rest",
            new_callable=AsyncMock,
            return_value=[
                {
                    "node_id": "IC_kwDOtest003",
                    "user": {"login": "netlify[bot]", "type": "Bot"},
                    "body": "",
                    "created_at": "2026-02-08T10:00:00Z",
                },
            ],
        )

        result = await _get_pr_issue_comments("owner", "repo", 42)
        assert result == []

    async def test_handles_empty_response(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.comments.github_api.rest", new_callable=AsyncMock, return_value=[])
        result = await _get_pr_issue_comments("owner", "repo", 42)
        assert result == []

    async def test_reviewer_is_raw_login(self, mocker: MockerFixture):
        """reviewer field should be the raw GitHub login."""
        mocker.patch(
            "codereviewbuddy.tools.comments.github_api.rest",
            new_callable=AsyncMock,
            return_value=[
                {
                    "node_id": "IC_kwDOtest004",
                    "user": {"login": "ai-reviewer-a[bot]", "type": "Bot"},
                    "body": "please re-review",
                    "created_at": "2026-02-08T10:00:00Z",
                },
            ],
        )

        result = await _get_pr_issue_comments("owner", "repo", 42)
        assert len(result) == 1
        assert result[0].reviewer == "ai-reviewer-a[bot]"

    async def test_passes_paginate_flag(self, mocker: MockerFixture):
        """_get_pr_issue_comments must use paginate=True (#111)."""
        mock_rest = mocker.patch("codereviewbuddy.tools.comments.github_api.rest", new_callable=AsyncMock, return_value=[])
        await _get_pr_issue_comments("owner", "repo", 42)
        mock_rest.assert_called_once_with(
            "/repos/owner/repo/issues/42/comments?per_page=100",
            paginate=True,
        )


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
