"""Minimal FastMCP server that simulates codereviewbuddy's write tools.

Used by test_stdio_rapid_writes.py to reproduce the hang from issue #65.
Each tool adds a small delay to simulate the gh CLI subprocess calls that
the real tools make (GraphQL mutations, REST API calls).
"""

from __future__ import annotations

import asyncio

from fastmcp import FastMCP

mcp = FastMCP("mock-write-server")


@mcp.tool
async def list_review_comments(pr_number: int, repo: str | None = None, status: str | None = None) -> str:  # noqa: ARG001
    """Simulate listing review comments (read operation, ~100ms gh call)."""
    await asyncio.sleep(0.1)
    return '{"threads": [{"thread_id": "PRRT_test1", "status": "unresolved"}]}'


@mcp.tool
async def resolve_stale_comments(pr_number: int, repo: str | None = None) -> str:  # noqa: ARG001
    """Simulate bulk-resolving stale comments (write operation, ~200ms of gh calls)."""
    await asyncio.sleep(0.2)
    return '{"resolved_count": 1, "resolved_thread_ids": ["PRRT_test1"]}'


@mcp.tool
def resolve_comment(pr_number: int, thread_id: str) -> str:  # noqa: ARG001
    """Simulate resolving a single thread (sync write, ~100ms gh call)."""
    import time

    time.sleep(0.1)
    return f"Resolved {thread_id}"


@mcp.tool
def reply_to_comment(pr_number: int, thread_id: str, body: str, repo: str | None = None) -> str:  # noqa: ARG001
    """Simulate replying to a thread (sync write, ~150ms of gh calls)."""
    import time

    time.sleep(0.15)
    return f"Replied to {thread_id}"


@mcp.tool
async def check_for_updates() -> str:
    """Simulate version check (read, fast)."""
    await asyncio.sleep(0.01)
    return '{"current_version": "1.0.0", "latest_version": "1.0.0", "update_available": false}'


if __name__ == "__main__":
    mcp.run()
