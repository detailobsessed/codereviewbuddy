"""MCP tool for waiting until AI reviews complete."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from codereviewbuddy.models import ReviewSummary  # noqa: TC001 - runtime needed
from codereviewbuddy.tools.comments import list_review_comments

if TYPE_CHECKING:
    from fastmcp.server.context import Context


async def wait_for_reviews(
    pr_number: int,
    repo: str | None = None,
    timeout: int = 300,
    poll_interval: int = 30,
    cwd: str | None = None,
    ctx: Context | None = None,
) -> ReviewSummary:
    """Poll until all known AI reviewers have reviewed the latest push.

    Repeatedly calls list_review_comments and checks reviews_in_progress.
    Returns when all reviewers have completed or timeout is reached.

    Args:
        pr_number: PR number to monitor.
        repo: Repository in "owner/repo" format. Auto-detected if not provided.
        timeout: Maximum seconds to wait (default 300 = 5 minutes).
        poll_interval: Seconds between polls (default 30).
        cwd: Working directory for git operations.
        ctx: FastMCP context for progress reporting.

    Returns:
        Final ReviewSummary (reviews may still be in progress if timeout was reached).
    """
    if poll_interval <= 0:
        msg = f"poll_interval must be positive, got {poll_interval}"
        raise ValueError(msg)
    if timeout < 0:
        msg = f"timeout must be non-negative, got {timeout}"
        raise ValueError(msg)

    elapsed = 0
    poll_count = 0
    max_polls = timeout // poll_interval + 1  # +1 for the initial poll at t=0

    while True:
        poll_count += 1
        if ctx:
            await ctx.report_progress(progress=poll_count, total=max_polls)
            await ctx.info(f"Polling for reviews on PR #{pr_number} ({poll_count}/{max_polls})")

        summary = await list_review_comments(pr_number, repo=repo, cwd=cwd, ctx=None)

        if not summary.reviews_in_progress:
            if ctx:
                await ctx.info(f"All reviews complete on PR #{pr_number}")
            return summary

        if elapsed + poll_interval > timeout:
            if ctx:
                pending = [s.reviewer for s in summary.reviewer_statuses if s.status == "pending"]
                await ctx.warning(
                    f"Timeout after {elapsed}s waiting for reviews on PR #{pr_number}. Still pending: {', '.join(pending)}",
                )
            return summary

        if ctx:
            pending = [s.reviewer for s in summary.reviewer_statuses if s.status == "pending"]
            await ctx.info(f"Waiting {poll_interval}s... pending: {', '.join(pending)}")

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
