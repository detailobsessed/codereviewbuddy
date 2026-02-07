"""Tests for comment tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

from codereviewbuddy.gh import GhError
from codereviewbuddy.tools.comments import (
    _get_changed_files,
    _get_pr_reviews,
    _parse_threads,
    list_review_comments,
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

    def test_returns_all_threads(self):
        threads = list_review_comments(42)
        assert len(threads) == 2

    def test_filter_unresolved(self):
        threads = list_review_comments(42, status="unresolved")
        assert len(threads) == 1
        assert threads[0].status == "unresolved"

    def test_filter_resolved(self):
        threads = list_review_comments(42, status="resolved")
        assert len(threads) == 1
        assert threads[0].status == "resolved"

    def test_explicit_repo(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", return_value=SAMPLE_GRAPHQL_RESPONSE)
        mocker.patch("codereviewbuddy.tools.comments.gh.rest", return_value=[])
        mocker.patch("codereviewbuddy.tools.comments._get_changed_files", return_value=set())
        threads = list_review_comments(42, repo="myorg/myrepo")
        assert len(threads) == 2


class TestNonExistentPR:
    def test_list_comments_returns_empty_for_null_pr(self, mocker: MockerFixture):
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

        threads = list_review_comments(42)
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
    def test_success(self, mocker: MockerFixture):
        response = {"data": {"resolveReviewThread": {"thread": {"id": "PRRT_test", "isResolved": True}}}}
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", return_value=response)
        result = resolve_comment(42, "PRRT_test")
        assert "Resolved" in result

    def test_failure(self, mocker: MockerFixture):
        response = {"data": {"resolveReviewThread": {"thread": {"id": "PRRT_test", "isResolved": False}}}}
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", return_value=response)
        with pytest.raises(GhError, match="Failed to resolve"):
            resolve_comment(42, "PRRT_test")


class TestResolveStaleComments:
    def test_resolves_stale(self, mocker: MockerFixture):
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

        result = resolve_stale_comments(42)
        assert result["resolved_count"] == 1
        assert "PRRT_kwDOtest123" in result["resolved_thread_ids"]

    def test_nothing_to_resolve(self, mocker: MockerFixture):
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

        result = resolve_stale_comments(42)
        assert result["resolved_count"] == 0


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

    def test_includes_pr_reviews_alongside_threads(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", return_value=SAMPLE_GRAPHQL_RESPONSE)
        mocker.patch("codereviewbuddy.tools.comments._get_changed_files", return_value=set())
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))
        mocker.patch(
            "codereviewbuddy.tools.comments.gh.rest",
            return_value=[
                {
                    "node_id": "PRR_devin",
                    "user": {"login": "devin-ai-integration[bot]"},
                    "state": "COMMENTED",
                    "body": "2 potential issues found.",
                    "submitted_at": "2026-02-07T10:00:00Z",
                },
            ],
        )

        threads = list_review_comments(42)
        # 2 inline threads + 1 PR review
        assert len(threads) == 3
        pr_reviews = [t for t in threads if t.file is None]
        assert len(pr_reviews) == 1
        assert pr_reviews[0].reviewer == "devin"


class TestThreadsPagination:
    def test_fetches_multiple_pages(self, mocker: MockerFixture):
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

        threads = list_review_comments(42)
        assert len(threads) == 2
        # Verify cursor was passed on second call
        second_call = mock_graphql.call_args_list[1]
        variables = second_call.kwargs.get("variables", {})
        assert variables.get("cursor") == "cursor_abc"

    def test_stops_on_null_end_cursor(self, mocker: MockerFixture):
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

        threads = list_review_comments(42)
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
