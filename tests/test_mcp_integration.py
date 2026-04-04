"""MCP integration tests using FastMCP Client with in-memory transport.

These tests exercise the full MCP protocol path: schema validation,
tool dispatch, serialization, and error propagation — unlike unit tests
which call tool functions directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

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
        "check_ci_status",
        "diagnose_ci",
        "get_thread",
        "list_recent_unresolved",
        "stack_activity",
        "reply_to_comment",
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
        assert len(tools) == 10


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
    async def test_get_thread_schema(self, client: Client):
        tools = await client.list_tools()
        tool = next(t for t in tools if t.name == "get_thread")
        schema = tool.inputSchema
        assert "thread_id" in schema["properties"]
        assert "thread_id" in schema.get("required", [])

    async def test_triage_schema(self, client: Client):
        tools = await client.list_tools()
        tool = next(t for t in tools if t.name == "triage_review_comments")
        schema = tool.inputSchema
        assert "pr_numbers" in schema["properties"]


# ---------------------------------------------------------------------------
# Tool invocation tests (through MCP protocol)
# ---------------------------------------------------------------------------


class TestGetThreadMCP:
    async def test_returns_thread(self, client: Client, mocker: MockerFixture):
        response = {
            "data": {
                "node": {
                    "__typename": "PullRequestReviewThread",
                    "pullRequest": {"number": 42},
                    **SAMPLE_THREAD_NODE,
                },
            },
        }
        mocker.patch(
            "codereviewbuddy.tools.comments.github_api.graphql",
            new_callable=AsyncMock,
            return_value=response,
        )

        result = await client.call_tool("get_thread", {"thread_id": "PRRT_kwDOtest123"})
        assert not result.is_error
        assert len(result.content) > 0


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


class TestSummarizeReviewStatusMCP:
    async def test_returns_status(self, client: Client, mocker: MockerFixture):
        from codereviewbuddy.models import StackReviewStatusResult

        mocker.patch(
            "codereviewbuddy.tools.stack.summarize_review_status",
            return_value=StackReviewStatusResult(),
        )
        result = await client.call_tool("summarize_review_status", {"pr_numbers": [42]})
        assert not result.is_error


class TestTriageReviewCommentsMCP:
    async def test_returns_triage(self, client: Client, mocker: MockerFixture):
        from codereviewbuddy.models import TriageResult

        mocker.patch(
            "codereviewbuddy.tools.comments.triage_review_comments",
            return_value=TriageResult(items=[]),
        )
        result = await client.call_tool("triage_review_comments", {"pr_numbers": [42]})
        assert not result.is_error


class TestDiagnoseCIMCP:
    async def test_returns_diagnosis(self, client: Client, mocker: MockerFixture):
        from codereviewbuddy.models import CIDiagnosisResult

        mocker.patch(
            "codereviewbuddy.tools.ci.diagnose_ci",
            return_value=CIDiagnosisResult(),
        )
        result = await client.call_tool("diagnose_ci", {"pr_number": 42})
        assert not result.is_error


class TestStackActivityMCP:
    async def test_returns_activity(self, client: Client, mocker: MockerFixture):
        from codereviewbuddy.models import StackActivityResult

        mocker.patch(
            "codereviewbuddy.tools.stack.stack_activity",
            return_value=StackActivityResult(events=[]),
        )
        result = await client.call_tool("stack_activity", {"pr_numbers": [42]})
        assert not result.is_error


class TestResourceRegistration:
    async def test_pr_reviews_resource_registered(self, client: Client):
        templates = await client.list_resource_templates()
        names = {t.name for t in templates}
        assert "pr_reviews" in names

    async def test_pr_reviews_resource_readable(self, client: Client, mocker: MockerFixture):
        graphql_response = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "title": "feat: test",
                        "url": "https://github.com/o/r/pull/42",
                        "latestReviews": {"nodes": []},
                        "reviewRequests": {"nodes": []},
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {
                                    "isResolved": False,
                                    "comments": {"nodes": [{"__typename": "PullRequestReviewComment"}]},
                                },
                            ],
                        },
                    }
                }
            },
        }
        mocker.patch(
            "codereviewbuddy.tools.stack.github_api.graphql",
            new_callable=AsyncMock,
            return_value=graphql_response,
        )
        resources = await client.read_resource("pr://owner/repo/42/reviews")
        assert len(resources) > 0


class TestListRecentUnresolvedMCP:
    async def test_returns_results(self, client: Client, mocker: MockerFixture):
        from codereviewbuddy.models import StackReviewStatusResult

        mocker.patch(
            "codereviewbuddy.tools.stack.list_recent_unresolved",
            return_value=StackReviewStatusResult(),
        )
        result = await client.call_tool("list_recent_unresolved", {"repo": "owner/repo"})
        assert not result.is_error
