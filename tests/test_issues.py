"""Tests for issue creation tool."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

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
    async def test_creates_issue_with_labels(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.issues.gh.get_repo_info", return_value=("owner", "repo"))
        mock_graphql = mocker.patch(
            "codereviewbuddy.tools.issues.github_api.graphql",
            new=AsyncMock(return_value=SAMPLE_THREAD_RESPONSE),
        )
        mock_rest = mocker.patch(
            "codereviewbuddy.tools.issues.github_api.rest",
            new=AsyncMock(return_value={"number": 42, "html_url": "https://github.com/owner/repo/issues/42"}),
        )

        result = await create_issue_from_comment(
            pr_number=10,
            thread_id="PRRT_kwDOtest123",
            title="Refactor into helper function",
            labels=["enhancement", "P2"],
        )

        assert result.issue_number == 42
        assert result.issue_url == "https://github.com/owner/repo/issues/42"
        assert result.title == "Refactor into helper function"

        # Verify REST was called with correct labels
        mock_rest.assert_called_once()
        assert mock_rest.call_args.kwargs.get("labels") == ["enhancement", "P2"]

        # Verify thread was fetched
        mock_graphql.assert_called_once()

    async def test_creates_issue_without_labels(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.issues.gh.get_repo_info", return_value=("owner", "repo"))
        mocker.patch(
            "codereviewbuddy.tools.issues.github_api.graphql",
            new=AsyncMock(return_value=SAMPLE_THREAD_RESPONSE),
        )
        mock_rest = mocker.patch(
            "codereviewbuddy.tools.issues.github_api.rest",
            new=AsyncMock(return_value={"number": 7, "html_url": "https://github.com/owner/repo/issues/7"}),
        )

        result = await create_issue_from_comment(
            pr_number=10,
            thread_id="PRRT_kwDOtest123",
            title="Track suggestion",
        )

        assert result.issue_number == 7
        mock_rest.assert_called_once()
        assert "labels" not in (mock_rest.call_args.kwargs or {})

    async def test_explicit_repo(self, mocker: MockerFixture):
        mocker.patch(
            "codereviewbuddy.tools.issues.github_api.graphql",
            new=AsyncMock(return_value=SAMPLE_THREAD_RESPONSE),
        )
        mock_rest = mocker.patch(
            "codereviewbuddy.tools.issues.github_api.rest",
            new=AsyncMock(return_value={"number": 1, "html_url": "https://github.com/other/repo/issues/1"}),
        )

        result = await create_issue_from_comment(
            pr_number=5,
            thread_id="PRRT_kwDOtest456",
            title="Test",
            repo="other/repo",
        )

        assert result.issue_number == 1
        mock_rest.assert_called_once()
        assert "/repos/other/repo/issues" in mock_rest.call_args.args[0]

    async def test_thread_not_found_raises(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.issues.gh.get_repo_info", return_value=("owner", "repo"))
        mocker.patch(
            "codereviewbuddy.tools.issues.github_api.graphql",
            new=AsyncMock(return_value={"data": {"node": {"comments": {"nodes": []}}}}),
        )

        with pytest.raises(GhError, match="Could not find comment content"):
            await create_issue_from_comment(
                pr_number=10,
                thread_id="PRRT_kwDObad",
                title="Should fail",
            )

    async def test_issue_body_contains_pr_reference(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.issues.gh.get_repo_info", return_value=("owner", "repo"))
        mocker.patch(
            "codereviewbuddy.tools.issues.github_api.graphql",
            new=AsyncMock(return_value=SAMPLE_THREAD_RESPONSE),
        )
        mock_rest = mocker.patch(
            "codereviewbuddy.tools.issues.github_api.rest",
            new=AsyncMock(return_value={"number": 5, "html_url": "https://github.com/owner/repo/issues/5"}),
        )

        await create_issue_from_comment(
            pr_number=10,
            thread_id="PRRT_kwDOtest123",
            title="Test body content",
        )

        body = mock_rest.call_args.kwargs.get("body", "")
        assert "PR #10" in body
        assert "src/codereviewbuddy/tools/comments.py" in body
        assert "line 42" in body
        assert "unblocked[bot]" in body
        assert "Consider refactoring" in body

    async def test_comment_without_file_path(self, mocker: MockerFixture):
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
        mocker.patch(
            "codereviewbuddy.tools.issues.github_api.graphql",
            new=AsyncMock(return_value=response),
        )
        mock_rest = mocker.patch(
            "codereviewbuddy.tools.issues.github_api.rest",
            new=AsyncMock(return_value={"number": 99, "html_url": "https://github.com/owner/repo/issues/99"}),
        )

        result = await create_issue_from_comment(
            pr_number=3,
            thread_id="PRRT_kwDOtest789",
            title="General suggestion",
        )

        assert result.issue_number == 99
        body = mock_rest.call_args.kwargs.get("body", "")
        assert "**Location:**" not in body
