"""Tests for comment tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

from codereviewbuddy.gh import GhError
from codereviewbuddy.tools.comments import (
    _get_changed_files,
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


class TestGetChangedFiles:
    def test_extracts_paths(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", return_value=SAMPLE_DIFF_RESPONSE)
        files = _get_changed_files("owner", "repo", 42)
        assert files == {"src/codereviewbuddy/gh.py", "README.md"}


class TestListReviewComments:
    @pytest.fixture(autouse=True)
    def _mock_gh(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", return_value=SAMPLE_GRAPHQL_RESPONSE)
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
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))

        result = resolve_stale_comments(42)
        assert result["resolved_count"] == 0


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
