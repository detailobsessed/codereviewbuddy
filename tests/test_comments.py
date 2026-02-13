"""Tests for comment tools."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

from codereviewbuddy.gh import GhError
from codereviewbuddy.tools.comments import (
    _build_reviewer_statuses,
    _get_pr_issue_comments,
    _get_pr_reviews,
    _latest_push_time_from_commits,
    _parse_threads,
    list_review_comments,
    list_stack_review_comments,
    reply_to_comment,
    resolve_comment,
    resolve_stale_comments,
)

# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------

SAMPLE_THREAD_NODE = {
    "id": "PRRT_kwDOtest123",
    "isResolved": False,
    "isOutdated": False,
    "comments": {
        "nodes": [
            {
                "author": {"login": "unblocked[bot]"},
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
    "isOutdated": False,
    "comments": {
        "nodes": [
            {
                "author": {"login": "devin-ai-integration[bot]"},
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
        assert t.reviewer == "unblocked"
        assert len(t.comments) == 1
        assert t.comments[0].author == "unblocked[bot]"

    def test_maps_is_outdated_to_is_stale(self):
        """_parse_threads maps GraphQL isOutdated to is_stale."""
        outdated_node = {**SAMPLE_THREAD_NODE, "isOutdated": True}
        threads = _parse_threads([outdated_node], pr_number=42)
        assert threads[0].is_stale is True

    def test_not_outdated_maps_to_not_stale(self):
        threads = _parse_threads([SAMPLE_THREAD_NODE], pr_number=42)
        assert threads[0].is_stale is False

    def test_empty_comments_skipped(self):
        node = {"id": "PRRT_empty", "isResolved": False, "comments": {"nodes": []}}
        threads = _parse_threads([node], pr_number=42)
        assert len(threads) == 0

    def test_resolved_status(self):
        threads = _parse_threads([SAMPLE_RESOLVED_THREAD], pr_number=42)
        assert threads[0].status == "resolved"
        assert threads[0].reviewer == "devin"

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


class TestGetPrCommits:
    """Tests for _get_pr_commits — pagination regression (#95)."""

    def test_passes_paginate_flag(self, mocker: MockerFixture):
        """_get_pr_commits must use paginate=True so PRs with >100 commits work."""
        from codereviewbuddy.tools.comments import _get_pr_commits

        mock_rest = mocker.patch("codereviewbuddy.tools.comments.gh.rest", return_value=[{"sha": "abc"}])
        result = _get_pr_commits("owner", "repo", 42)
        assert result == [{"sha": "abc"}]
        mock_rest.assert_called_once_with(
            "/repos/owner/repo/pulls/42/commits?per_page=100",
            cwd=None,
            paginate=True,
        )

    def test_returns_empty_list_on_none(self, mocker: MockerFixture):
        """gh.rest returning None should be normalised to an empty list."""
        from codereviewbuddy.tools.comments import _get_pr_commits

        mocker.patch("codereviewbuddy.tools.comments.gh.rest", return_value=None)
        assert _get_pr_commits("owner", "repo", 42) == []


SAMPLE_COMMITS_RESPONSE = [
    {
        "sha": "abc123",
        "commit": {
            "committer": {
                "date": "2026-02-06T12:00:00Z",
            }
        },
    },
]


class TestListReviewComments:
    @pytest.fixture(autouse=True)
    def _mock_gh(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", return_value=SAMPLE_GRAPHQL_RESPONSE)
        mocker.patch(
            "codereviewbuddy.tools.comments.gh.rest",
            side_effect=[
                [],  # _get_pr_reviews
                [],  # _get_pr_issue_comments
                SAMPLE_COMMITS_RESPONSE,  # _get_pr_commits
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
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", return_value=SAMPLE_GRAPHQL_RESPONSE)
        mocker.patch(
            "codereviewbuddy.tools.comments.gh.rest",
            side_effect=[[], [], SAMPLE_COMMITS_RESPONSE],
        )
        summary = await list_review_comments(42, repo="myorg/myrepo")
        assert len(summary.threads) == 2

    async def test_discover_stack_failure_preserves_threads(self, mocker: MockerFixture):
        """Regression: discover_stack failure must not discard fetched thread data."""
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", return_value=SAMPLE_GRAPHQL_RESPONSE)
        mocker.patch(
            "codereviewbuddy.tools.comments.gh.rest",
            side_effect=[[], [], SAMPLE_COMMITS_RESPONSE],
        )
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))
        mocker.patch("codereviewbuddy.tools.stack._fetch_open_prs", side_effect=RuntimeError("network error"))

        summary = await list_review_comments(42)
        assert len(summary.threads) == 2  # threads preserved despite stack failure
        assert summary.stack == []  # stack gracefully empty

    async def test_returns_reviewer_statuses(self):
        summary = await list_review_comments(42)
        # Should have statuses for unblocked and devin (both present in SAMPLE threads)
        reviewer_names = {s.reviewer for s in summary.reviewer_statuses}
        assert "unblocked" in reviewer_names
        assert "devin" in reviewer_names

    async def test_reviews_in_progress_when_pushed_after_review(self):
        """Commit at 12:00, reviews at 10:00/11:00 → both pending."""
        summary = await list_review_comments(42)
        assert summary.reviews_in_progress is True
        for s in summary.reviewer_statuses:
            assert s.status == "pending"

    async def test_disabled_reviewer_threads_filtered(self, mocker: MockerFixture):
        """Threads from disabled reviewers should not appear in results."""
        from codereviewbuddy.config import Config, ReviewerConfig

        custom = Config(reviewers={"devin": ReviewerConfig(enabled=False)})
        mocker.patch("codereviewbuddy.tools.comments.get_config", return_value=custom)

        summary = await list_review_comments(42)
        reviewers_in_results = {t.reviewer for t in summary.threads}
        assert "devin" not in reviewers_in_results
        assert "unblocked" in reviewers_in_results


class TestNonExistentPR:
    async def test_list_comments_returns_empty_for_null_pr(self, mocker: MockerFixture):
        """Regression: pullRequest=null must not crash with AttributeError."""
        null_pr_response = {
            "data": {"repository": {"pullRequest": None}},
        }
        mocker.patch(
            "codereviewbuddy.tools.comments.gh.graphql",
            return_value=null_pr_response,
        )
        mocker.patch(
            "codereviewbuddy.tools.comments.gh.rest",
            side_effect=[[], [], []],
        )
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))
        mocker.patch("codereviewbuddy.tools.stack._fetch_open_prs", return_value=[])

        summary = await list_review_comments(42)
        assert summary.threads == []


class TestResolveComment:
    async def test_success(self, mocker: MockerFixture):
        response = {"data": {"resolveReviewThread": {"thread": {"id": "PRRT_test", "isResolved": True}}}}
        mocker.patch("codereviewbuddy.tools.comments._fetch_thread_detail", return_value=("unblocked", "some comment"))
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", return_value=response)
        result = resolve_comment(42, "PRRT_test")
        assert "Resolved" in result

    async def test_failure(self, mocker: MockerFixture):
        response = {"data": {"resolveReviewThread": {"thread": {"id": "PRRT_test", "isResolved": False}}}}
        mocker.patch("codereviewbuddy.tools.comments._fetch_thread_detail", return_value=("unblocked", "some comment"))
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", return_value=response)
        with pytest.raises(GhError, match="Failed to resolve"):
            resolve_comment(42, "PRRT_test")

    async def test_blocked_by_config(self, mocker: MockerFixture):
        """Resolving a Devin bug thread should be blocked by default config."""
        mocker.patch(
            "codereviewbuddy.tools.comments._fetch_thread_detail",
            return_value=("devin", "🔴 **Bug: something is broken**"),
        )
        with pytest.raises(GhError, match="Config blocks resolving"):
            resolve_comment(42, "PRRT_test")

    async def test_allowed_devin_info(self, mocker: MockerFixture):
        """Resolving a Devin info thread should be allowed by default config."""
        response = {"data": {"resolveReviewThread": {"thread": {"id": "PRRT_test", "isResolved": True}}}}
        mocker.patch(
            "codereviewbuddy.tools.comments._fetch_thread_detail",
            return_value=("devin", "📝 **Info: something informational**"),
        )
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", return_value=response)
        result = resolve_comment(42, "PRRT_test")
        assert "Resolved" in result

    async def test_unknown_reviewer_allowed(self, mocker: MockerFixture):
        """Unknown reviewer (empty string from lookup failure) should not block."""
        response = {"data": {"resolveReviewThread": {"thread": {"id": "PRRT_test", "isResolved": True}}}}
        mocker.patch("codereviewbuddy.tools.comments._fetch_thread_detail", return_value=("", ""))
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", return_value=response)
        result = resolve_comment(42, "PRRT_test")
        assert "Resolved" in result


def _mark_stale_thread_node():
    """Return SAMPLE_THREAD_NODE with isOutdated=True for stale tests."""
    return {**SAMPLE_THREAD_NODE, "isOutdated": True}


class TestResolveStaleComments:
    @pytest.fixture(autouse=True)
    def _mock_stack(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.stack._fetch_open_prs", return_value=[])

    async def test_resolves_stale(self, mocker: MockerFixture):
        stale_thread = _mark_stale_thread_node()
        stale_response = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "title": "Test PR",
                        "url": "https://github.com/owner/repo/pull/42",
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [stale_thread, SAMPLE_RESOLVED_THREAD],
                        },
                    }
                }
            },
        }

        graphql_responses = [
            stale_response,  # list_review_comments → threads query
            {"data": {"t0": {"thread": {"id": stale_thread["id"], "isResolved": True}}}},  # batch resolve
        ]

        mocker.patch(
            "codereviewbuddy.tools.comments.gh.graphql",
            side_effect=graphql_responses,
        )
        mocker.patch(
            "codereviewbuddy.tools.comments.gh.rest",
            side_effect=[[], [], SAMPLE_COMMITS_RESPONSE],
        )
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))

        result = await resolve_stale_comments(42)
        assert result.resolved_count == 1
        assert "PRRT_kwDOtest123" in result.resolved_thread_ids

    async def test_skips_auto_resolving_reviewers(self, mocker: MockerFixture):
        """Devin/CodeRabbit threads should be skipped — they auto-resolve themselves."""
        devin_thread = {
            "id": "PRRT_kwDOdevin456",
            "isResolved": False,
            "isOutdated": True,
            "comments": {
                "nodes": [
                    {
                        "author": {"login": "devin-ai-integration[bot]"},
                        "body": "🔴 **Bug: Consider refactoring this.**",
                        "createdAt": "2026-02-06T10:00:00Z",
                        "path": "src/codereviewbuddy/gh.py",
                        "line": 10,
                    }
                ]
            },
        }
        mixed_response = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "title": "Test PR",
                        "url": "https://github.com/owner/repo/pull/42",
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [_mark_stale_thread_node(), devin_thread],
                        },
                    }
                }
            },
        }

        graphql_responses = [
            mixed_response,  # list_review_comments → threads query
            {"data": {"t0": {"thread": {"id": "PRRT_kwDOtest123", "isResolved": True}}}},  # batch resolve (only unblocked)
        ]

        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", side_effect=graphql_responses)
        mocker.patch(
            "codereviewbuddy.tools.comments.gh.rest",
            side_effect=[[], [], SAMPLE_COMMITS_RESPONSE],
        )
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))

        result = await resolve_stale_comments(42)
        # Only the unblocked thread should be resolved; Devin thread skipped
        assert result.resolved_count == 1
        assert "PRRT_kwDOtest123" in result.resolved_thread_ids
        assert "PRRT_kwDOdevin456" not in result.resolved_thread_ids
        assert result.skipped_count == 1

    async def test_resolves_devin_info_threads(self, mocker: MockerFixture):
        """Devin info threads (📝) should be resolved — Devin won't auto-resolve them."""
        devin_info_thread = {
            "id": "PRRT_kwDOdevin_info",
            "isResolved": False,
            "isOutdated": True,
            "comments": {
                "nodes": [
                    {
                        "author": {"login": "devin-ai-integration[bot]"},
                        "body": "📝 **Info: This is an informational comment**\n\nSome analysis details.",
                        "createdAt": "2026-02-06T10:00:00Z",
                        "path": "src/codereviewbuddy/gh.py",
                        "line": 15,
                    }
                ]
            },
        }
        mixed_response = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "title": "Test PR",
                        "url": "https://github.com/owner/repo/pull/42",
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [_mark_stale_thread_node(), devin_info_thread],
                        },
                    }
                }
            },
        }

        graphql_responses = [
            mixed_response,
            # Both threads resolved: unblocked + devin info
            {
                "data": {
                    "t0": {"thread": {"id": "PRRT_kwDOtest123", "isResolved": True}},
                    "t1": {"thread": {"id": "PRRT_kwDOdevin_info", "isResolved": True}},
                }
            },
        ]

        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", side_effect=graphql_responses)
        mocker.patch(
            "codereviewbuddy.tools.comments.gh.rest",
            side_effect=[[], [], SAMPLE_COMMITS_RESPONSE],
        )
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))

        result = await resolve_stale_comments(42)
        assert result.resolved_count == 2
        assert "PRRT_kwDOtest123" in result.resolved_thread_ids
        assert "PRRT_kwDOdevin_info" in result.resolved_thread_ids
        assert result.skipped_count == 0

    async def test_nothing_to_resolve(self, mocker: MockerFixture):
        mocker.patch(
            "codereviewbuddy.tools.comments.gh.graphql",
            return_value=SAMPLE_GRAPHQL_RESPONSE,
        )
        mocker.patch(
            "codereviewbuddy.tools.comments.gh.rest",
            side_effect=[[], [], SAMPLE_COMMITS_RESPONSE],
        )
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))

        result = await resolve_stale_comments(42)
        assert result.resolved_count == 0


class TestGetPrReviews:
    """Tests for _get_pr_reviews — PR-level review summaries from AI reviewers."""

    def test_returns_devin_review(self, mocker: MockerFixture):
        reviews = [
            {
                "node_id": "PRR_devin_123",
                "user": {"login": "devin-ai-integration[bot]"},
                "state": "COMMENTED",
                "body": "**Devin Review** found 2 potential issues.",
                "submitted_at": "2026-02-07T10:00:00Z",
            },
        ]
        mocker.patch("codereviewbuddy.tools.comments.gh.rest", return_value=reviews)

        result = _get_pr_reviews("owner", "repo", 42)
        assert len(result) == 1
        assert result[0].reviewer == "devin"
        assert result[0].thread_id == "PRR_devin_123"
        assert result[0].file is None
        assert result[0].line is None
        assert result[0].status == "unresolved"
        assert "2 potential issues" in result[0].comments[0].body
        assert result[0].is_pr_review is True

    def test_returns_unblocked_review(self, mocker: MockerFixture):
        reviews = [
            {
                "node_id": "PRR_unblocked_456",
                "user": {"login": "unblocked[bot]"},
                "state": "COMMENTED",
                "body": "2 issues found.",
                "submitted_at": "2026-02-07T09:00:00Z",
            },
        ]
        mocker.patch("codereviewbuddy.tools.comments.gh.rest", return_value=reviews)

        result = _get_pr_reviews("owner", "repo", 42)
        assert len(result) == 1
        assert result[0].reviewer == "unblocked"

    def test_skips_unknown_reviewers(self, mocker: MockerFixture):
        reviews = [
            {
                "node_id": "PRR_human",
                "user": {"login": "humanuser"},
                "state": "APPROVED",
                "body": "LGTM!",
                "submitted_at": "2026-02-07T11:00:00Z",
            },
        ]
        mocker.patch("codereviewbuddy.tools.comments.gh.rest", return_value=reviews)

        result = _get_pr_reviews("owner", "repo", 42)
        assert result == []

    def test_skips_empty_bodies(self, mocker: MockerFixture):
        reviews = [
            {
                "node_id": "PRR_empty",
                "user": {"login": "unblocked[bot]"},
                "state": "COMMENTED",
                "body": "",
                "submitted_at": "2026-02-07T09:00:00Z",
            },
        ]
        mocker.patch("codereviewbuddy.tools.comments.gh.rest", return_value=reviews)

        result = _get_pr_reviews("owner", "repo", 42)
        assert result == []

    def test_maps_approved_to_resolved(self, mocker: MockerFixture):
        reviews = [
            {
                "node_id": "PRR_approved",
                "user": {"login": "devin-ai-integration[bot]"},
                "state": "APPROVED",
                "body": "No Issues Found",
                "submitted_at": "2026-02-07T10:00:00Z",
            },
        ]
        mocker.patch("codereviewbuddy.tools.comments.gh.rest", return_value=reviews)

        result = _get_pr_reviews("owner", "repo", 42)
        assert len(result) == 1
        assert result[0].status == "resolved"

    def test_maps_changes_requested_to_unresolved(self, mocker: MockerFixture):
        reviews = [
            {
                "node_id": "PRR_changes",
                "user": {"login": "unblocked[bot]"},
                "state": "CHANGES_REQUESTED",
                "body": "3 issues found.",
                "submitted_at": "2026-02-07T10:00:00Z",
            },
        ]
        mocker.patch("codereviewbuddy.tools.comments.gh.rest", return_value=reviews)

        result = _get_pr_reviews("owner", "repo", 42)
        assert len(result) == 1
        assert result[0].status == "unresolved"

    def test_handles_empty_response(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.comments.gh.rest", return_value=None)

        result = _get_pr_reviews("owner", "repo", 42)
        assert result == []

    def test_handles_null_user(self, mocker: MockerFixture):
        reviews = [
            {
                "node_id": "PRR_null_user",
                "user": None,
                "state": "COMMENTED",
                "body": "Some review",
                "submitted_at": "2026-02-07T10:00:00Z",
            },
        ]
        mocker.patch("codereviewbuddy.tools.comments.gh.rest", return_value=reviews)

        result = _get_pr_reviews("owner", "repo", 42)
        assert result == []

    def test_passes_paginate_flag(self, mocker: MockerFixture):
        """_get_pr_reviews must use paginate=True (#111)."""
        mock_rest = mocker.patch("codereviewbuddy.tools.comments.gh.rest", return_value=[])
        _get_pr_reviews("owner", "repo", 42)
        mock_rest.assert_called_once_with(
            "/repos/owner/repo/pulls/42/reviews?per_page=100",
            cwd=None,
            paginate=True,
        )


class TestListIncludesPrReviews:
    """Tests that list_review_comments includes PR-level reviews."""

    async def test_includes_pr_reviews_alongside_threads(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", return_value=SAMPLE_GRAPHQL_RESPONSE)
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))
        mocker.patch("codereviewbuddy.tools.stack._fetch_open_prs", return_value=[])
        mocker.patch(
            "codereviewbuddy.tools.comments.gh.rest",
            side_effect=[
                # _get_pr_reviews call
                [
                    {
                        "node_id": "PRR_devin",
                        "user": {"login": "devin-ai-integration[bot]"},
                        "state": "COMMENTED",
                        "body": "2 potential issues found.",
                        "submitted_at": "2026-02-07T10:00:00Z",
                    },
                ],
                # _get_pr_issue_comments call
                [],
                # _get_pr_commits call
                SAMPLE_COMMITS_RESPONSE,
            ],
        )

        summary = await list_review_comments(42)
        # 2 inline threads + 1 PR review
        assert len(summary.threads) == 3
        pr_reviews = [t for t in summary.threads if t.file is None]
        assert len(pr_reviews) == 1
        assert pr_reviews[0].reviewer == "devin"


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
            "codereviewbuddy.tools.comments.gh.graphql",
            side_effect=[page1, page2],
        )
        mocker.patch(
            "codereviewbuddy.tools.comments.gh.rest",
            side_effect=[[], [], SAMPLE_COMMITS_RESPONSE],
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
            "codereviewbuddy.tools.comments.gh.graphql",
            return_value=malformed_page,
        )
        mocker.patch(
            "codereviewbuddy.tools.comments.gh.rest",
            side_effect=[[], [], SAMPLE_COMMITS_RESPONSE],
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
            reviewer="devin",
            comments=[],
            is_stale=False,
        )
        thread_11 = ReviewThread(
            thread_id="PRRT_11",
            pr_number=11,
            status=CommentStatus.RESOLVED,
            file="b.py",
            line=5,
            reviewer="coderabbit",
            comments=[],
            is_stale=False,
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


class TestGetPrIssueComments:
    def test_returns_bot_comments(self, mocker: MockerFixture):
        """Bot comments (type=Bot or [bot] suffix) should be included."""
        mocker.patch(
            "codereviewbuddy.tools.comments.gh.rest",
            return_value=[
                {
                    "node_id": "IC_kwDOtest001",
                    "user": {"login": "codecov[bot]", "type": "Bot"},
                    "body": "## Coverage Report\n\nAll files 95%",
                    "created_at": "2026-02-08T10:00:00Z",
                },
            ],
        )

        result = _get_pr_issue_comments("owner", "repo", 42)

        assert len(result) == 1
        assert result[0].thread_id == "IC_kwDOtest001"
        assert result[0].reviewer == "codecov[bot]"
        assert result[0].is_pr_review is True
        assert "Coverage Report" in result[0].comments[0].body

    def test_skips_human_comments(self, mocker: MockerFixture):
        """Non-bot comments should be excluded."""
        mocker.patch(
            "codereviewbuddy.tools.comments.gh.rest",
            return_value=[
                {
                    "node_id": "IC_kwDOtest002",
                    "user": {"login": "humanuser", "type": "User"},
                    "body": "LGTM",
                    "created_at": "2026-02-08T10:00:00Z",
                },
            ],
        )

        result = _get_pr_issue_comments("owner", "repo", 42)
        assert result == []

    def test_skips_empty_bodies(self, mocker: MockerFixture):
        mocker.patch(
            "codereviewbuddy.tools.comments.gh.rest",
            return_value=[
                {
                    "node_id": "IC_kwDOtest003",
                    "user": {"login": "netlify[bot]", "type": "Bot"},
                    "body": "",
                    "created_at": "2026-02-08T10:00:00Z",
                },
            ],
        )

        result = _get_pr_issue_comments("owner", "repo", 42)
        assert result == []

    def test_handles_empty_response(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.comments.gh.rest", return_value=[])
        result = _get_pr_issue_comments("owner", "repo", 42)
        assert result == []

    def test_known_reviewer_uses_reviewer_name(self, mocker: MockerFixture):
        """Known AI reviewers should be identified by their reviewer name."""
        mocker.patch(
            "codereviewbuddy.tools.comments.gh.rest",
            return_value=[
                {
                    "node_id": "IC_kwDOtest004",
                    "user": {"login": "unblocked[bot]", "type": "Bot"},
                    "body": "@unblocked please re-review",
                    "created_at": "2026-02-08T10:00:00Z",
                },
            ],
        )

        result = _get_pr_issue_comments("owner", "repo", 42)
        assert len(result) == 1
        assert result[0].reviewer == "unblocked"

    def test_passes_paginate_flag(self, mocker: MockerFixture):
        """_get_pr_issue_comments must use paginate=True (#111)."""
        mock_rest = mocker.patch("codereviewbuddy.tools.comments.gh.rest", return_value=[])
        _get_pr_issue_comments("owner", "repo", 42)
        mock_rest.assert_called_once_with(
            "/repos/owner/repo/issues/42/comments?per_page=100",
            cwd=None,
            paginate=True,
        )


class TestReplyToComment:
    def test_reply_to_inline_thread(self, mocker: MockerFixture):
        """PRRT_ IDs should use GraphQL addPullRequestReviewThreadReply mutation."""
        # No get_repo_info mock needed — PRRT_ path short-circuits before repo lookup
        mock_graphql = mocker.patch(
            "codereviewbuddy.tools.comments.gh.graphql",
            return_value={"data": {"addPullRequestReviewThreadReply": {"comment": {"id": "C_123"}}}},
        )

        result = reply_to_comment(42, "PRRT_kwDOtest123", "looks good")

        assert "Replied to thread PRRT_kwDOtest123" in result
        mock_graphql.assert_called_once()
        call_args = mock_graphql.call_args
        assert call_args.kwargs["variables"] == {"threadId": "PRRT_kwDOtest123", "body": "looks good"}
        assert "addPullRequestReviewThreadReply" in call_args.args[0]

    def test_reply_to_pr_review(self, mocker: MockerFixture):
        """PRR_ IDs should use the issues comments API."""
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))
        mock_rest = mocker.patch("codereviewbuddy.tools.comments.gh.rest")

        result = reply_to_comment(42, "PRR_kwDOtest456", "addressed in next PR")

        assert "Replied to PR-level review" in result
        mock_rest.assert_called_once_with(
            "/repos/owner/repo/issues/42/comments",
            method="POST",
            body="addressed in next PR",
            cwd=None,
        )

    def test_reply_to_pr_review_with_explicit_repo(self, mocker: MockerFixture):
        """PRR_ with explicit repo should not call get_repo_info."""
        mock_rest = mocker.patch("codereviewbuddy.tools.comments.gh.rest")

        result = reply_to_comment(10, "PRR_kwDOtest789", "noted", repo="other/repo")

        assert "Replied to PR-level review" in result
        mock_rest.assert_called_once_with(
            "/repos/other/repo/issues/10/comments",
            method="POST",
            body="noted",
            cwd=None,
        )

    def test_reply_to_bot_issue_comment(self, mocker: MockerFixture):
        """IC_ IDs (bot issue comments) should use the issues comments API."""
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))
        mock_rest = mocker.patch("codereviewbuddy.tools.comments.gh.rest")

        result = reply_to_comment(42, "IC_kwDOtest001", "thanks for the coverage report")

        assert "Replied to bot comment" in result
        mock_rest.assert_called_once_with(
            "/repos/owner/repo/issues/42/comments",
            method="POST",
            body="thanks for the coverage report",
            cwd=None,
        )

    def test_resolve_rejects_prr_id(self):
        """resolve_comment should reject PRR_ IDs with a clear error."""
        with pytest.raises(GhError, match="only inline review threads"):
            resolve_comment(42, "PRR_kwDOtest123")

    def test_resolve_rejects_ic_id(self):
        """resolve_comment should reject IC_ IDs with a clear error."""
        with pytest.raises(GhError, match="only inline review threads"):
            resolve_comment(42, "IC_kwDOtest001")

    def test_inline_thread_graphql_error_raises(self, mocker: MockerFixture):
        """GraphQL errors when replying to PRRT_ should raise GhError."""
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))
        mocker.patch(
            "codereviewbuddy.tools.comments.gh.graphql",
            return_value={"errors": [{"message": "Could not resolve to a node"}]},
        )

        with pytest.raises(GhError, match="GraphQL error"):
            reply_to_comment(42, "PRRT_kwDObad", "test")


# ---------------------------------------------------------------------------
# Reviewer status detection (#46)
# ---------------------------------------------------------------------------


class TestLatestPushTimeFromCommits:
    def test_returns_last_commit_date(self):
        result = _latest_push_time_from_commits(SAMPLE_COMMITS_RESPONSE)
        assert result is not None
        assert result.year == 2026
        assert result.month == 2
        assert result.day == 6
        assert result.hour == 12

    def test_returns_none_for_empty_list(self):
        result = _latest_push_time_from_commits([])
        assert result is None


class TestBuildReviewerStatuses:
    def test_completed_when_review_after_push(self):
        from datetime import UTC, datetime

        from codereviewbuddy.models import CommentStatus, ReviewComment, ReviewThread

        threads = [
            ReviewThread(
                thread_id="PRRT_1",
                pr_number=42,
                status=CommentStatus.UNRESOLVED,
                reviewer="unblocked",
                comments=[
                    ReviewComment(
                        author="unblocked[bot]",
                        body="issue",
                        created_at=datetime(2026, 2, 6, 14, 0, tzinfo=UTC),
                    ),
                ],
            ),
        ]
        push_at = datetime(2026, 2, 6, 12, 0, tzinfo=UTC)

        statuses = _build_reviewer_statuses(threads, push_at)
        assert len(statuses) == 1
        assert statuses[0].reviewer == "unblocked"
        assert statuses[0].status == "completed"

    def test_pending_when_push_after_review(self):
        from datetime import UTC, datetime

        from codereviewbuddy.models import CommentStatus, ReviewComment, ReviewThread

        threads = [
            ReviewThread(
                thread_id="PRRT_1",
                pr_number=42,
                status=CommentStatus.UNRESOLVED,
                reviewer="devin",
                comments=[
                    ReviewComment(
                        author="devin-ai-integration[bot]",
                        body="issue",
                        created_at=datetime(2026, 2, 6, 10, 0, tzinfo=UTC),
                    ),
                ],
            ),
        ]
        push_at = datetime(2026, 2, 6, 12, 0, tzinfo=UTC)

        statuses = _build_reviewer_statuses(threads, push_at)
        assert len(statuses) == 1
        assert statuses[0].reviewer == "devin"
        assert statuses[0].status == "pending"

    def test_skips_unknown_reviewers(self):
        from datetime import UTC, datetime

        from codereviewbuddy.models import CommentStatus, ReviewComment, ReviewThread

        threads = [
            ReviewThread(
                thread_id="IC_1",
                pr_number=42,
                status=CommentStatus.UNRESOLVED,
                reviewer="codecov[bot]",
                comments=[
                    ReviewComment(
                        author="codecov[bot]",
                        body="coverage report",
                        created_at=datetime(2026, 2, 6, 10, 0, tzinfo=UTC),
                    ),
                ],
            ),
        ]
        push_at = datetime(2026, 2, 6, 12, 0, tzinfo=UTC)

        statuses = _build_reviewer_statuses(threads, push_at)
        assert statuses == []

    def test_ignores_human_replies_in_ai_threads(self):
        """Human replies in AI threads should not inflate last_review_at."""
        from datetime import UTC, datetime

        from codereviewbuddy.models import CommentStatus, ReviewComment, ReviewThread

        threads = [
            ReviewThread(
                thread_id="PRRT_1",
                pr_number=42,
                status=CommentStatus.UNRESOLVED,
                reviewer="unblocked",
                comments=[
                    ReviewComment(
                        author="unblocked[bot]",
                        body="issue found",
                        created_at=datetime(2026, 2, 6, 10, 0, tzinfo=UTC),
                    ),
                    ReviewComment(
                        author="humandev",
                        body="Fixed in abc123",
                        created_at=datetime(2026, 2, 6, 16, 0, tzinfo=UTC),
                    ),
                ],
            ),
        ]
        push_at = datetime(2026, 2, 6, 12, 0, tzinfo=UTC)

        statuses = _build_reviewer_statuses(threads, push_at)
        assert len(statuses) == 1
        assert statuses[0].reviewer == "unblocked"
        # Should be pending: the bot's comment (10:00) is before push (12:00).
        # The human reply (16:00) should NOT count as a reviewer comment.
        assert statuses[0].status == "pending"

    def test_empty_threads_returns_empty(self):
        from datetime import UTC, datetime

        statuses = _build_reviewer_statuses([], datetime(2026, 2, 6, 12, 0, tzinfo=UTC))
        assert statuses == []

    def test_no_push_time_assumes_completed(self):
        from datetime import UTC, datetime

        from codereviewbuddy.models import CommentStatus, ReviewComment, ReviewThread

        threads = [
            ReviewThread(
                thread_id="PRRT_1",
                pr_number=42,
                status=CommentStatus.UNRESOLVED,
                reviewer="unblocked",
                comments=[
                    ReviewComment(
                        author="unblocked[bot]",
                        body="issue",
                        created_at=datetime(2026, 2, 6, 10, 0, tzinfo=UTC),
                    ),
                ],
            ),
        ]

        statuses = _build_reviewer_statuses(threads, None)
        assert len(statuses) == 1
        assert statuses[0].status == "completed"
