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
        assert len(tools) == 11


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

    async def test_output_schemas_present(self, client: Client):
        """Verify that tools with typed return annotations expose output schemas."""
        tools = await client.list_tools()
        tools_by_name = {t.name: t for t in tools}

        # Tools returning Pydantic models should have outputSchema
        for name in ("list_review_comments",):
            tool = tools_by_name[name]
            assert tool.outputSchema is not None, f"{name} should have an outputSchema"
            assert tool.outputSchema.get("type") == "object", f"{name} outputSchema should be object type"


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
        assert "self_improvement" in data["config"]
        assert "diagnostics" in data["config"]

    async def test_reflects_live_config(self, client: Client):
        """show_config returns the currently active config, not a stale snapshot."""
        from codereviewbuddy.config import Config, PRDescriptionsConfig, set_config

        custom = Config(pr_descriptions=PRDescriptionsConfig(enabled=False))
        set_config(custom)
        try:
            result = await client.call_tool("show_config", {})
            import json

            data = json.loads(result.content[0].text)  # type: ignore[unresolved-attribute]
            assert data["config"]["pr_descriptions"]["enabled"] is False
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
