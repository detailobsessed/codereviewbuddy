"""MCP integration tests using FastMCP Client with in-memory transport.

These tests exercise the full MCP protocol path: schema validation,
tool dispatch, serialization, and error propagation — unlike unit tests
which call tool functions directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from codereviewbuddy.server import mcp

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

# ---------------------------------------------------------------------------
# Fixture data (reused from test_comments.py)
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
                    ],
                }
            }
        }
    },
}

RESOLVE_SUCCESS = {"data": {"resolveReviewThread": {"thread": {"id": "PRRT_kwDOtest123", "isResolved": True}}}}

REPLY_THREAD_QUERY_RESPONSE = {
    "data": {"node": {"comments": {"nodes": [{"databaseId": 12345}]}}},
}

REPLY_REST_RESPONSE = {"id": 99999, "body": "Fixed!"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def client():
    async with Client(mcp) as c:
        yield c


# ---------------------------------------------------------------------------
# Tool registration & schema tests
# ---------------------------------------------------------------------------


class TestToolRegistration:
    EXPECTED_TOOLS = frozenset({
        "list_review_comments",
        "resolve_comment",
        "resolve_stale_comments",
        "reply_to_comment",
        "request_rereview",
    })

    async def test_all_tools_registered(self, client: Client):
        tools = await client.list_tools()
        names = {t.name for t in tools}
        assert names == self.EXPECTED_TOOLS

    async def test_tool_count(self, client: Client):
        tools = await client.list_tools()
        assert len(tools) == 5

    async def test_list_review_comments_schema(self, client: Client):
        tools = await client.list_tools()
        tool = next(t for t in tools if t.name == "list_review_comments")
        schema = tool.inputSchema
        assert "pr_number" in schema["properties"]
        assert schema["properties"]["pr_number"]["type"] == "integer"
        assert "pr_number" in schema["required"]

    async def test_resolve_comment_schema(self, client: Client):
        tools = await client.list_tools()
        tool = next(t for t in tools if t.name == "resolve_comment")
        schema = tool.inputSchema
        assert "thread_id" in schema["properties"]
        assert "pr_number" in schema["properties"]

    async def test_request_rereview_schema(self, client: Client):
        tools = await client.list_tools()
        tool = next(t for t in tools if t.name == "request_rereview")
        schema = tool.inputSchema
        # reviewer and repo are optional
        required = schema.get("required", [])
        assert "pr_number" in required
        assert "reviewer" not in required
        assert "repo" not in required


# ---------------------------------------------------------------------------
# Tool invocation tests (through MCP protocol)
# ---------------------------------------------------------------------------


class TestListReviewCommentsMCP:
    async def test_returns_serialized_threads(self, client: Client, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", return_value=SAMPLE_GRAPHQL_RESPONSE)
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))
        mocker.patch("codereviewbuddy.tools.comments._get_changed_files", return_value=set())

        result = await client.call_tool("list_review_comments", {"pr_number": 42})
        assert not result.is_error
        # Result comes back as text content containing the serialized list
        assert len(result.content) > 0

    async def test_with_status_filter(self, client: Client, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", return_value=SAMPLE_GRAPHQL_RESPONSE)
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))
        mocker.patch("codereviewbuddy.tools.comments._get_changed_files", return_value=set())

        result = await client.call_tool("list_review_comments", {"pr_number": 42, "status": "unresolved"})
        assert not result.is_error

    async def test_with_explicit_repo(self, client: Client, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", return_value=SAMPLE_GRAPHQL_RESPONSE)
        mocker.patch("codereviewbuddy.tools.comments._get_changed_files", return_value=set())

        result = await client.call_tool("list_review_comments", {"pr_number": 42, "repo": "myorg/myrepo"})
        assert not result.is_error


class TestResolveCommentMCP:
    async def test_success(self, client: Client, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", return_value=RESOLVE_SUCCESS)

        result = await client.call_tool("resolve_comment", {"pr_number": 42, "thread_id": "PRRT_kwDOtest123"})
        assert not result.is_error

    async def test_failure_propagates(self, client: Client, mocker: MockerFixture):
        fail_response = {"data": {"resolveReviewThread": {"thread": {"id": "PRRT_test", "isResolved": False}}}}
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", return_value=fail_response)

        with pytest.raises(ToolError, match="Failed to resolve"):
            await client.call_tool("resolve_comment", {"pr_number": 42, "thread_id": "PRRT_test"})


class TestResolveStaleCommentsMCP:
    async def test_resolves_stale_through_mcp(self, client: Client, mocker: MockerFixture):
        graphql_responses = [
            SAMPLE_GRAPHQL_RESPONSE,
            SAMPLE_DIFF_RESPONSE,
            {"data": {"t0": {"thread": {"id": "PRRT_kwDOtest123", "isResolved": True}}}},
        ]
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", side_effect=graphql_responses)
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))

        result = await client.call_tool("resolve_stale_comments", {"pr_number": 42})
        assert not result.is_error


class TestReplyToCommentMCP:
    async def test_success(self, client: Client, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", return_value=REPLY_THREAD_QUERY_RESPONSE)
        mocker.patch("codereviewbuddy.tools.comments.gh.rest", return_value=REPLY_REST_RESPONSE)
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))

        result = await client.call_tool(
            "reply_to_comment",
            {"pr_number": 42, "thread_id": "PRRT_kwDOtest123", "body": "Fixed!"},
        )
        assert not result.is_error


class TestRequestRereviewMCP:
    async def test_trigger_unblocked(self, client: Client, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.rereview.gh.get_repo_info", return_value=("owner", "repo"))
        mocker.patch("codereviewbuddy.tools.rereview.gh.run_gh")

        result = await client.call_tool("request_rereview", {"pr_number": 42, "reviewer": "unblocked"})
        assert not result.is_error

    async def test_trigger_all(self, client: Client, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.rereview.gh.get_repo_info", return_value=("owner", "repo"))
        mocker.patch("codereviewbuddy.tools.rereview.gh.run_gh")

        result = await client.call_tool("request_rereview", {"pr_number": 42})
        assert not result.is_error

    async def test_unknown_reviewer_error(self, client: Client, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.rereview.gh.get_repo_info", return_value=("owner", "repo"))

        with pytest.raises(ToolError, match="Unknown reviewer"):
            await client.call_tool("request_rereview", {"pr_number": 42, "reviewer": "nonexistent"})
