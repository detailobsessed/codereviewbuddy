"""Reproduce MCP server hang after rapid write tool calls (issue #65).

The server becomes unresponsive after a sequence of rapid write operations
(resolve_stale → reply → resolve → resolve → request_rereview). This test
uses a mock server over stdio transport to isolate the transport-layer bug.

Marked xfail because the hang is believed to originate in the client-side
stdio transport (mcp-go in Windsurf), not in FastMCP's Python server.
However, having this test lets us verify the server side is clean and
gives us a regression test if the root cause turns out to be server-side.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports.stdio import PythonStdioTransport

MOCK_SERVER = Path(__file__).parent / "helpers" / "mock_write_server.py"

# The exact write sequence from issue #65 that triggers the hang in Windsurf
WRITE_SEQUENCE = [
    ("list_review_comments", {"pr_number": 42}),
    ("resolve_stale_comments", {"pr_number": 42}),
    ("reply_to_comment", {"pr_number": 42, "thread_id": "PRRT_test1", "body": "Fixed"}),
    ("resolve_comment", {"pr_number": 42, "thread_id": "PRRT_test1"}),
    ("resolve_comment", {"pr_number": 42, "thread_id": "PRRT_test1"}),
    ("request_rereview", {"pr_number": 42}),
]


@pytest.mark.slow
class TestStdioRapidWrites:
    """Reproduce the rapid-write hang over stdio transport."""

    async def _run_sequence(self, client: Client, sequence: list, rounds: int = 1) -> list[dict]:
        """Fire a sequence of tool calls and record results."""
        results = []
        for round_num in range(rounds):
            for tool_name, args in sequence:
                start = asyncio.get_event_loop().time()
                try:
                    result = await asyncio.wait_for(
                        client.call_tool(tool_name, args),
                        timeout=5.0,
                    )
                    elapsed = asyncio.get_event_loop().time() - start
                    results.append({
                        "round": round_num,
                        "tool": tool_name,
                        "elapsed": elapsed,
                        "success": not result.is_error,
                        "timed_out": False,
                    })
                except TimeoutError:
                    elapsed = asyncio.get_event_loop().time() - start
                    results.append({
                        "round": round_num,
                        "tool": tool_name,
                        "elapsed": elapsed,
                        "success": False,
                        "timed_out": True,
                    })
        return results

    @pytest.mark.xfail(
        reason="Issue #65: MCP server hangs after rapid write tool calls. "
        "Believed to be a client-side stdio transport bug (mcp-go), "
        "but this test verifies the server side is clean.",
        strict=False,
    )
    async def test_rapid_sequential_writes_no_hang(self):
        """Fire the exact write sequence from issue #65 multiple rounds — no call should hang."""
        transport = PythonStdioTransport(MOCK_SERVER)
        async with Client(transport=transport) as client:
            results = await self._run_sequence(client, WRITE_SEQUENCE, rounds=5)

        timeouts = [r for r in results if r["timed_out"]]
        assert not timeouts, f"{len(timeouts)} calls timed out (hung): " + ", ".join(f"round {r['round']} {r['tool']}" for r in timeouts)

    @pytest.mark.xfail(
        reason="Issue #65: concurrent writes through stdio may corrupt JSON framing",
        strict=False,
    )
    async def test_concurrent_writes_no_hang(self):
        """Fire multiple write calls concurrently — tests JSON framing under load."""
        transport = PythonStdioTransport(MOCK_SERVER)
        async with Client(transport=transport) as client:
            all_results = []
            for round_num in range(3):
                tasks = []
                for tool_name, args in WRITE_SEQUENCE:
                    tasks.append(
                        asyncio.wait_for(
                            client.call_tool(tool_name, args),
                            timeout=10.0,
                        )
                    )
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for i, result in enumerate(results):
                    tool_name = WRITE_SEQUENCE[i][0]
                    if isinstance(result, TimeoutError):
                        all_results.append({"round": round_num, "tool": tool_name, "timed_out": True})
                    elif isinstance(result, Exception):
                        all_results.append({"round": round_num, "tool": tool_name, "error": str(result)})
                    else:
                        all_results.append({"round": round_num, "tool": tool_name, "success": True})

        timeouts = [r for r in all_results if r.get("timed_out")]
        errors = [r for r in all_results if r.get("error")]
        assert not timeouts, f"{len(timeouts)} calls timed out"
        assert not errors, f"{len(errors)} calls errored: {errors}"

    @pytest.mark.xfail(
        reason="Issue #65: rapid cancel+retry pattern may leave transport in bad state",
        strict=False,
    )
    async def test_cancel_and_retry_no_hang(self):
        """Cancel a write mid-flight, then immediately retry — transport should recover."""
        transport = PythonStdioTransport(MOCK_SERVER)
        async with Client(transport=transport) as client:
            for round_num in range(5):
                # Start a slow write and cancel it
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        client.call_tool("resolve_stale_comments", {"pr_number": 42}),
                        timeout=0.05,  # 50ms — too short, will cancel mid-flight
                    )

                # Immediately retry with a normal call — should not hang
                try:
                    result = await asyncio.wait_for(
                        client.call_tool("check_for_updates", {}),
                        timeout=5.0,
                    )
                    assert not result.is_error, f"Round {round_num}: post-cancel call returned error"
                except TimeoutError:
                    pytest.fail(f"Round {round_num}: transport hung after cancellation")
