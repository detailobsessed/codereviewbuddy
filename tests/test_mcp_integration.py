"""MCP integration tests using FastMCP Client with in-memory transport.

These tests exercise the full MCP protocol path: schema validation,
tool dispatch, serialization, and error propagation — unlike unit tests
which call tool functions directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastmcp import Client

from codereviewbuddy.server import mcp

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

# ---------------------------------------------------------------------------
# Fixture data (reused from test_comments.py)
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

RESOLVE_SUCCESS = {"data": {"resolveReviewThread": {"thread": {"id": "PRRT_kwDOtest123", "isResolved": True}}}}

REPLY_THREAD_QUERY_RESPONSE = {
    "data": {"node": {"comments": {"nodes": [{"databaseId": 12345}]}}},
}

REPLY_REST_RESPONSE = {"id": 99999, "body": "Fixed!"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def client(mocker: MockerFixture):
    from codereviewbuddy.config import Config

    mocker.patch("codereviewbuddy.server.check_prerequisites")
    mocker.patch("codereviewbuddy.server.load_config", return_value=Config())
    async with Client(mcp) as c:
        yield c


# ---------------------------------------------------------------------------
# Tool registration & schema tests
# ---------------------------------------------------------------------------


class TestToolRegistration:
    EXPECTED_TOOLS = frozenset({
        "diagnose_ci",
        "list_review_comments",
        "list_stack_review_comments",
        "list_recent_unresolved",
        "stack_activity",
        "resolve_comment",
        "resolve_stale_comments",
        "reply_to_comment",
        "create_issue_from_comment",
        "review_pr_descriptions",
        "show_config",
        "summarize_review_status",
        "triage_review_comments",
    })

    async def test_all_tools_registered(self, client: Client):
        tools = await client.list_tools()
        names = {t.name for t in tools}
        assert names == self.EXPECTED_TOOLS

    async def test_tool_count(self, client: Client):
        tools = await client.list_tools()
        assert len(tools) == 13


class TestPromptRegistration:
    EXPECTED_PROMPTS = frozenset({
        "review_stack",
        "pr_review_checklist",
        "ship_stack",
    })

    async def test_all_prompts_registered(self, client: Client):
        prompts = await client.list_prompts()
        names = {p.name for p in prompts}
        assert names == self.EXPECTED_PROMPTS

    async def test_prompt_count(self, client: Client):
        prompts = await client.list_prompts()
        assert len(prompts) == 3

    async def test_review_stack_returns_content(self, client: Client):
        from mcp.types import TextContent

        result = await client.get_prompt("review_stack")
        assert len(result.messages) >= 1
        content = result.messages[0].content
        assert isinstance(content, TextContent)
        assert "summarize_review_status" in content.text
        assert "triage_review_comments" in content.text

    async def test_ship_stack_mentions_activity(self, client: Client):
        from mcp.types import TextContent

        result = await client.get_prompt("ship_stack")
        content = result.messages[0].content
        assert isinstance(content, TextContent)
        assert "stack_activity" in content.text


class TestToolSchemas:
    async def test_list_review_comments_schema(self, client: Client):
        tools = await client.list_tools()
        tool = next(t for t in tools if t.name == "list_review_comments")
        schema = tool.inputSchema
        assert "pr_number" in schema["properties"]
        # pr_number is optional (int | None) — not in required
        assert "pr_number" not in schema.get("required", [])

    async def test_resolve_comment_schema(self, client: Client):
        tools = await client.list_tools()
        tool = next(t for t in tools if t.name == "resolve_comment")
        schema = tool.inputSchema
        assert "thread_id" in schema["properties"]
        assert "pr_number" in schema["properties"]

    async def test_output_schemas_present(self, client: Client):
        """Verify that tools with typed return annotations expose output schemas."""
        tools = await client.list_tools()
        tools_by_name = {t.name: t for t in tools}

        # Tools returning Pydantic models should have outputSchema
        for name in ("list_review_comments", "resolve_stale_comments"):
            tool = tools_by_name[name]
            assert tool.outputSchema is not None, f"{name} should have an outputSchema"
            assert tool.outputSchema.get("type") == "object", f"{name} outputSchema should be object type"

    async def test_resolve_stale_output_schema_fields(self, client: Client):
        """Verify resolve_stale_comments output schema contains expected fields."""
        tools = await client.list_tools()
        tool = next(t for t in tools if t.name == "resolve_stale_comments")
        assert tool.outputSchema is not None
        props = tool.outputSchema.get("properties", {})
        assert "resolved_count" in props
        assert "resolved_thread_ids" in props


# ---------------------------------------------------------------------------
# Tool invocation tests (through MCP protocol)
# ---------------------------------------------------------------------------


class TestListReviewCommentsMCP:
    async def test_returns_serialized_threads(self, client: Client, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", return_value=SAMPLE_GRAPHQL_RESPONSE)
        mocker.patch(
            "codereviewbuddy.tools.comments.gh.rest",
            side_effect=[SAMPLE_COMMITS_RESPONSE, [], []],
        )
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))

        result = await client.call_tool("list_review_comments", {"pr_number": 42})
        assert not result.is_error
        # Result comes back as text content containing the serialized list
        assert len(result.content) > 0

    async def test_with_status_filter(self, client: Client, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", return_value=SAMPLE_GRAPHQL_RESPONSE)
        mocker.patch(
            "codereviewbuddy.tools.comments.gh.rest",
            side_effect=[SAMPLE_COMMITS_RESPONSE, [], []],
        )
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))

        result = await client.call_tool("list_review_comments", {"pr_number": 42, "status": "unresolved"})
        assert not result.is_error

    async def test_with_explicit_repo(self, client: Client, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", return_value=SAMPLE_GRAPHQL_RESPONSE)
        mocker.patch(
            "codereviewbuddy.tools.comments.gh.rest",
            side_effect=[SAMPLE_COMMITS_RESPONSE, [], []],
        )
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))

        result = await client.call_tool("list_review_comments", {"pr_number": 42, "repo": "myorg/myrepo"})
        assert not result.is_error


class TestResolveCommentMCP:
    async def test_success(self, client: Client, mocker: MockerFixture):
        mocker.patch(
            "codereviewbuddy.tools.comments._fetch_thread_detail",
            return_value=("unblocked", "some comment", ["unblocked-ai[bot]", "ichoosetoaccept"]),
        )
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", return_value=RESOLVE_SUCCESS)

        result = await client.call_tool("resolve_comment", {"pr_number": 42, "thread_id": "PRRT_kwDOtest123"})
        assert not result.is_error

    async def test_failure_returns_error_string(self, client: Client, mocker: MockerFixture):
        fail_response = {"data": {"resolveReviewThread": {"thread": {"id": "PRRT_test", "isResolved": False}}}}
        mocker.patch(
            "codereviewbuddy.tools.comments._fetch_thread_detail",
            return_value=("unblocked", "some comment", ["unblocked-ai[bot]", "ichoosetoaccept"]),
        )
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", return_value=fail_response)

        result = await client.call_tool("resolve_comment", {"pr_number": 42, "thread_id": "PRRT_test"})
        assert not result.is_error
        assert "Error resolving PRRT_test" in result.content[0].text  # type: ignore[unresolved-attribute]

    async def test_blocked_by_config(self, client: Client, mocker: MockerFixture):
        mocker.patch(
            "codereviewbuddy.tools.comments._fetch_thread_detail",
            return_value=("devin", "🔴 **Bug: something is broken**", ["devin-ai-integration[bot]", "ichoosetoaccept"]),
        )

        result = await client.call_tool("resolve_comment", {"pr_number": 42, "thread_id": "PRRT_test"})
        # Config enforcement error is caught by server tool and returned as error string
        assert not result.is_error
        assert "Config blocks resolving" in result.content[0].text  # type: ignore[unresolved-attribute]


class TestResolveStaleCommentsMCP:
    async def test_resolves_stale_through_mcp(self, client: Client, mocker: MockerFixture):
        stale_thread = {**SAMPLE_THREAD_NODE, "isOutdated": True}
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
            stale_response,
            {"data": {"t0": {"thread": {"id": "PRRT_kwDOtest123", "isResolved": True}}}},
        ]
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", side_effect=graphql_responses)
        mocker.patch(
            "codereviewbuddy.tools.comments.gh.rest",
            side_effect=[SAMPLE_COMMITS_RESPONSE, [], []],
        )
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


class TestListStackReviewCommentsMCP:
    async def test_returns_grouped_results(self, client: Client, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.comments.gh.graphql", return_value=SAMPLE_GRAPHQL_RESPONSE)
        mocker.patch(
            "codereviewbuddy.tools.comments.gh.rest",
            side_effect=[SAMPLE_COMMITS_RESPONSE, [], []] * 2,
        )
        mocker.patch("codereviewbuddy.tools.comments.gh.get_repo_info", return_value=("owner", "repo"))

        result = await client.call_tool("list_stack_review_comments", {"pr_numbers": [42, 43]})
        assert not result.is_error
        assert len(result.content) > 0


class TestShowConfigMCP:
    async def test_returns_config(self, client: Client):
        result = await client.call_tool("show_config", {})
        assert not result.is_error
        import json

        data = json.loads(result.content[0].text)  # type: ignore[unresolved-attribute]
        assert "config" in data
        assert "source" in data
        # Config should have the expected top-level keys
        assert "reviewers" in data["config"]
        assert "self_improvement" in data["config"]
        assert "diagnostics" in data["config"]

    async def test_reflects_live_config(self, client: Client):
        """show_config returns the currently active config, not a stale snapshot."""
        from codereviewbuddy.config import Config, ReviewerConfig, set_config

        custom = Config(reviewers={"devin": ReviewerConfig(enabled=False)})
        set_config(custom)
        try:
            result = await client.call_tool("show_config", {})
            import json

            data = json.loads(result.content[0].text)  # type: ignore[unresolved-attribute]
            assert data["config"]["reviewers"]["devin"]["enabled"] is False
        finally:
            set_config(Config())


class TestReviewPRDescriptionsMCP:
    async def test_returns_analysis(self, client: Client, mocker: MockerFixture):
        mocker.patch(
            "codereviewbuddy.tools.descriptions._fetch_pr_info",
            return_value={
                "number": 42,
                "title": "feat: test",
                "body": "## Summary\n\nCloses #15\n\nA meaningful description.",
                "url": "https://github.com/owner/repo/pull/42",
            },
        )
        result = await client.call_tool("review_pr_descriptions", {"pr_numbers": [42]})
        assert not result.is_error
