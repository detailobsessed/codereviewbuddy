"""Tests for issue creation tool."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

from codereviewbuddy.gh import GhError
from codereviewbuddy.tools.issues import create_issue_from_comment

SAMPLE_THREAD_RESPONSE = {
    "data": {
        "node": {
            "comments": {
                "nodes": [
                    {
                        "body": "Consider refactoring this into a helper function.",
                        "path": "src/codereviewbuddy/tools/comments.py",
                        "line": 42,
                        "author": {"login": "unblocked[bot]"},
                        "url": "https://github.com/owner/repo/pull/10#discussion_r123",
                    }
                ]
            }
        }
    },
}


class TestCreateIssueFromComment:
    def test_creates_issue_with_labels(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.issues.gh.get_repo_info", return_value=("owner", "repo"))
        mock_graphql = mocker.patch("codereviewbuddy.tools.issues.gh.graphql", return_value=SAMPLE_THREAD_RESPONSE)
        mock_run_gh = mocker.patch(
            "codereviewbuddy.tools.issues.gh.run_gh",
            return_value="https://github.com/owner/repo/issues/42\n",
        )

        result = create_issue_from_comment(
            pr_number=10,
            thread_id="PRRT_kwDOtest123",
            title="Refactor into helper function",
            labels=["enhancement", "P2"],
        )

        assert result.issue_number == 42
        assert result.issue_url == "https://github.com/owner/repo/issues/42"
        assert result.title == "Refactor into helper function"

        # Verify gh issue create was called with correct args
        call_args = mock_run_gh.call_args[0]
        assert "issue" in call_args
        assert "create" in call_args
        assert "--label" in call_args
        assert "enhancement" in call_args
        assert "P2" in call_args

        # Verify thread was fetched
        mock_graphql.assert_called_once()

    def test_creates_issue_without_labels(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.issues.gh.get_repo_info", return_value=("owner", "repo"))
        mocker.patch("codereviewbuddy.tools.issues.gh.graphql", return_value=SAMPLE_THREAD_RESPONSE)
        mock_run_gh = mocker.patch(
            "codereviewbuddy.tools.issues.gh.run_gh",
            return_value="https://github.com/owner/repo/issues/7\n",
        )

        result = create_issue_from_comment(
            pr_number=10,
            thread_id="PRRT_kwDOtest123",
            title="Track suggestion",
        )

        assert result.issue_number == 7
        call_args = mock_run_gh.call_args[0]
        assert "--label" not in call_args

    def test_explicit_repo(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.issues.gh.graphql", return_value=SAMPLE_THREAD_RESPONSE)
        mock_run_gh = mocker.patch(
            "codereviewbuddy.tools.issues.gh.run_gh",
            return_value="https://github.com/other/repo/issues/1\n",
        )

        result = create_issue_from_comment(
            pr_number=5,
            thread_id="PRRT_kwDOtest456",
            title="Test",
            repo="other/repo",
        )

        assert result.issue_number == 1
        call_args = mock_run_gh.call_args[0]
        assert "other/repo" in call_args

    def test_thread_not_found_raises(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.issues.gh.get_repo_info", return_value=("owner", "repo"))
        mocker.patch(
            "codereviewbuddy.tools.issues.gh.graphql",
            return_value={"data": {"node": {"comments": {"nodes": []}}}},
        )

        with pytest.raises(GhError, match="Could not find comment content"):
            create_issue_from_comment(
                pr_number=10,
                thread_id="PRRT_kwDObad",
                title="Should fail",
            )

    def test_issue_body_contains_pr_reference(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.issues.gh.get_repo_info", return_value=("owner", "repo"))
        mocker.patch("codereviewbuddy.tools.issues.gh.graphql", return_value=SAMPLE_THREAD_RESPONSE)
        mock_run_gh = mocker.patch(
            "codereviewbuddy.tools.issues.gh.run_gh",
            return_value="https://github.com/owner/repo/issues/5\n",
        )

        create_issue_from_comment(
            pr_number=10,
            thread_id="PRRT_kwDOtest123",
            title="Test body content",
        )

        # Extract the --body argument
        call_args = mock_run_gh.call_args[0]
        body_idx = list(call_args).index("--body") + 1
        body = call_args[body_idx]

        assert "PR #10" in body
        assert "src/codereviewbuddy/tools/comments.py" in body
        assert "line 42" in body
        assert "unblocked[bot]" in body
        assert "Consider refactoring" in body

    def test_comment_without_file_path(self, mocker: MockerFixture):
        """PR-level comments may not have a file path."""
        response = {
            "data": {
                "node": {
                    "comments": {
                        "nodes": [
                            {
                                "body": "General improvement suggestion.",
                                "path": None,
                                "line": None,
                                "author": {"login": "devin-ai-integration[bot]"},
                                "url": "https://github.com/owner/repo/pull/3#discussion_r789",
                            }
                        ]
                    }
                }
            },
        }
        mocker.patch("codereviewbuddy.tools.issues.gh.get_repo_info", return_value=("owner", "repo"))
        mocker.patch("codereviewbuddy.tools.issues.gh.graphql", return_value=response)
        mock_run_gh = mocker.patch(
            "codereviewbuddy.tools.issues.gh.run_gh",
            return_value="https://github.com/owner/repo/issues/99\n",
        )

        result = create_issue_from_comment(
            pr_number=3,
            thread_id="PRRT_kwDOtest789",
            title="General suggestion",
        )

        assert result.issue_number == 99
        # Body should not contain file location
        call_args = mock_run_gh.call_args[0]
        body_idx = list(call_args).index("--body") + 1
        body = call_args[body_idx]
        assert "**Location:**" not in body
