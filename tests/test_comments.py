"""Tests for comment tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

from codereviewbuddy.gh import GhError
from codereviewbuddy.tools.comments import (
    _get_changed_files,
    _get_pr_issue_comments,
    _get_pr_reviews,
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

SAMPLE_DIFF_RESPONSE = {
    "data": {
        "repository": {
            "pullRequest": {
                "files": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {"path": "src/codereviewbuddy/gh.py", "additions": 5, "deletions": 2, "changeType": "MODIFIED"},
                        {"path": "README.md", "additions": 1, "deletions": 0, "changeType": "MODIFIED"},
                    ],
                }
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

    def test_staleness_detection(self):
        changed = {"src/codereviewbuddy/gh.py"}
        threads = _parse_threads([SAMPLE_THREAD_NODE], pr_number=42, changed_files=changed)
        assert threads[0].is_stale is True

    def test_not_stale_when_file_unchanged(self):
        changed = {"README.md"}
        threads = _parse_threads([SAMPLE_THREAD_NODE], pr_number=42, changed_files=changed)
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


class TestGetChangedFiles:
    def test_extracts_paths(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", return_value=SAMPLE_DIFF_RESPONSE)
        files = _get_changed_files("owner", "repo", 42)
        assert files == {"src/codereviewbuddy/gh.py", "README.md"}


class TestListReviewComments:
    @pytest.fixture(autouse=True)
    def _mock_gh(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", return_value=SAMPLE_GRAPHQL_RESPONSE)
        mocker.patch("codereviewbuddy.tools.comments.gh.rest", return_value=[])
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))
        mocker.patch("codereviewbuddy.tools.comments._get_changed_files", return_value=set())

    async def test_returns_all_threads(self):
        threads = await list_review_comments(42)
        assert len(threads) == 2

    async def test_filter_unresolved(self):
        threads = await list_review_comments(42, status="unresolved")
        assert len(threads) == 1
        assert threads[0].status == "unresolved"

    async def test_filter_resolved(self):
        threads = await list_review_comments(42, status="resolved")
        assert len(threads) == 1
        assert threads[0].status == "resolved"

    async def test_explicit_repo(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", return_value=SAMPLE_GRAPHQL_RESPONSE)
        mocker.patch("codereviewbuddy.tools.comments.gh.rest", return_value=[])
        mocker.patch("codereviewbuddy.tools.comments._get_changed_files", return_value=set())
        threads = await list_review_comments(42, repo="myorg/myrepo")
        assert len(threads) == 2


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
        mocker.patch("codereviewbuddy.tools.comments.gh.rest", return_value=[])
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))

        threads = await list_review_comments(42)
        assert threads == []

    def test_get_changed_files_returns_empty_for_null_pr(self, mocker: MockerFixture):
        """Regression: pullRequest=null must not crash with AttributeError."""
        null_pr_response = {
            "data": {"repository": {"pullRequest": None}},
        }
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", return_value=null_pr_response)

        result = _get_changed_files("owner", "repo", 99999)
        assert result == set()


class TestResolveComment:
    async def test_success(self, mocker: MockerFixture):
        response = {"data": {"resolveReviewThread": {"thread": {"id": "PRRT_test", "isResolved": True}}}}
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", return_value=response)
        result = resolve_comment(42, "PRRT_test")
        assert "Resolved" in result

    async def test_failure(self, mocker: MockerFixture):
        response = {"data": {"resolveReviewThread": {"thread": {"id": "PRRT_test", "isResolved": False}}}}
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", return_value=response)
        with pytest.raises(GhError, match="Failed to resolve"):
            resolve_comment(42, "PRRT_test")


class TestResolveStaleComments:
    async def test_resolves_stale(self, mocker: MockerFixture):
        stale_thread = SAMPLE_THREAD_NODE.copy()

        graphql_responses = [
            SAMPLE_GRAPHQL_RESPONSE,  # list_review_comments → threads query
            SAMPLE_DIFF_RESPONSE,  # list_review_comments → changed files
            {"data": {"t0": {"thread": {"id": stale_thread["id"], "isResolved": True}}}},  # batch resolve
        ]

        mocker.patch(
            "codereviewbuddy.tools.comments.gh.graphql",
            side_effect=graphql_responses,
        )
        mocker.patch("codereviewbuddy.tools.comments.gh.rest", return_value=[])
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))

        result = await resolve_stale_comments(42)
        assert result.resolved_count == 1
        assert "PRRT_kwDOtest123" in result.resolved_thread_ids

    async def test_skips_auto_resolving_reviewers(self, mocker: MockerFixture):
        """Devin/CodeRabbit threads should be skipped — they auto-resolve themselves."""
        devin_thread = {
            "id": "PRRT_kwDOdevin456",
            "isResolved": False,
            "comments": {
                "nodes": [
                    {
                        "author": {"login": "devin-ai-integration[bot]"},
                        "body": "Consider refactoring this.",
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
                            "nodes": [SAMPLE_THREAD_NODE, devin_thread],
                        },
                    }
                }
            },
        }

        graphql_responses = [
            mixed_response,  # list_review_comments → threads query
            SAMPLE_DIFF_RESPONSE,  # list_review_comments → changed files (gh.py is changed)
            {"data": {"t0": {"thread": {"id": "PRRT_kwDOtest123", "isResolved": True}}}},  # batch resolve (only unblocked)
        ]

        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", side_effect=graphql_responses)
        mocker.patch("codereviewbuddy.tools.comments.gh.rest", return_value=[])
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))

        result = await resolve_stale_comments(42)
        # Only the unblocked thread should be resolved; Devin thread skipped
        assert result.resolved_count == 1
        assert "PRRT_kwDOtest123" in result.resolved_thread_ids
        assert "PRRT_kwDOdevin456" not in result.resolved_thread_ids
        assert result.skipped_count == 1

    async def test_nothing_to_resolve(self, mocker: MockerFixture):
        no_diff = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "files": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [],
                        }
                    }
                }
            }
        }
        mocker.patch(
            "codereviewbuddy.tools.comments.gh.graphql",
            side_effect=[SAMPLE_GRAPHQL_RESPONSE, no_diff],
        )
        mocker.patch("codereviewbuddy.tools.comments.gh.rest", return_value=[])
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


class TestListIncludesPrReviews:
    """Tests that list_review_comments includes PR-level reviews."""

    async def test_includes_pr_reviews_alongside_threads(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", return_value=SAMPLE_GRAPHQL_RESPONSE)
        mocker.patch("codereviewbuddy.tools.comments._get_changed_files", return_value=set())
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))
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
            ],
        )

        threads = await list_review_comments(42)
        # 2 inline threads + 1 PR review
        assert len(threads) == 3
        pr_reviews = [t for t in threads if t.file is None]
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
            side_effect=[page1, page2, SAMPLE_DIFF_RESPONSE],
        )
        mocker.patch("codereviewbuddy.tools.comments.gh.rest", return_value=[])
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))

        threads = await list_review_comments(42)
        assert len(threads) == 2
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
            side_effect=[malformed_page, SAMPLE_DIFF_RESPONSE],
        )
        mocker.patch("codereviewbuddy.tools.comments.gh.rest", return_value=[])
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))

        threads = await list_review_comments(42)
        assert len(threads) == 1


class TestChangedFilesPagination:
    def test_fetches_multiple_pages(self, mocker: MockerFixture):
        diff_page1 = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "files": {
                            "pageInfo": {"hasNextPage": True, "endCursor": "file_cursor_1"},
                            "nodes": [
                                {"path": "src/codereviewbuddy/gh.py", "additions": 5, "deletions": 2, "changeType": "MODIFIED"},
                            ],
                        }
                    }
                }
            },
        }
        diff_page2 = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "files": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {"path": "README.md", "additions": 1, "deletions": 0, "changeType": "MODIFIED"},
                            ],
                        }
                    }
                }
            },
        }
        mocker.patch(
            "codereviewbuddy.tools.comments.gh.graphql",
            side_effect=[diff_page1, diff_page2],
        )

        files = _get_changed_files("owner", "repo", 42)
        assert files == {"src/codereviewbuddy/gh.py", "README.md"}


# ---------------------------------------------------------------------------
# list_stack_review_comments
# ---------------------------------------------------------------------------


class TestListStackReviewComments:
    """Tests for list_stack_review_comments."""

    async def test_returns_threads_grouped_by_pr(self, mocker: MockerFixture):
        """Should call list_review_comments for each PR and group results."""
        from codereviewbuddy.models import CommentStatus, ReviewThread

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
        from unittest.mock import AsyncMock

        mock_list = mocker.patch(
            "codereviewbuddy.tools.comments.list_review_comments",
            new_callable=AsyncMock,
            side_effect=[[thread_10], [thread_11], []],
        )

        result = await list_stack_review_comments([10, 11, 12], repo="owner/repo")

        assert list(result.keys()) == [10, 11, 12]
        assert result[10] == [thread_10]
        assert result[11] == [thread_11]
        assert result[12] == []
        assert mock_list.call_count == 3

    async def test_passes_status_filter(self, mocker: MockerFixture):
        """Should forward status filter to each list_review_comments call."""
        from unittest.mock import AsyncMock

        mock_list = mocker.patch(
            "codereviewbuddy.tools.comments.list_review_comments",
            new_callable=AsyncMock,
            return_value=[],
        )

        await list_stack_review_comments([10, 11], repo="owner/repo", status="unresolved")

        for call in mock_list.call_args_list:
            assert call.kwargs["status"] == "unresolved"

    async def test_empty_pr_list(self, mocker: MockerFixture):
        """Should return empty dict for empty input."""
        from unittest.mock import AsyncMock

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


class TestReplyToComment:
    def test_reply_to_inline_thread(self, mocker: MockerFixture):
        """PRRT_ IDs should use the pull review comments reply API."""
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))
        graphql_response = {"data": {"node": {"comments": {"nodes": [{"databaseId": 12345}]}}}}
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", return_value=graphql_response)
        mock_rest = mocker.patch("codereviewbuddy.tools.comments.gh.rest")

        result = reply_to_comment(42, "PRRT_kwDOtest123", "looks good")

        assert "Replied to thread PRRT_kwDOtest123" in result
        mock_rest.assert_called_once_with(
            "/repos/owner/repo/pulls/42/comments/12345/replies",
            method="POST",
            body="looks good",
            cwd=None,
        )

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

    def test_inline_thread_not_found_raises(self, mocker: MockerFixture):
        """PRRT_ with no comment ID should raise GhError."""
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))
        mocker.patch(
            "codereviewbuddy.tools.comments.gh.graphql",
            return_value={"data": {"node": {"comments": {"nodes": [{}]}}}},
        )

        with pytest.raises(GhError, match="Could not find comment ID"):
            reply_to_comment(42, "PRRT_kwDObad", "test")
