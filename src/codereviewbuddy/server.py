"""FastMCP server for codereviewbuddy.

Exposes tools for managing AI code review comments on GitHub PRs.
Authentication is handled by the `gh` CLI — no tokens needed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastmcp import FastMCP

from codereviewbuddy import gh
from codereviewbuddy.tools import comments, rereview

if TYPE_CHECKING:
    from codereviewbuddy.models import ResolveStaleResult

logger = logging.getLogger(__name__)

mcp = FastMCP(
    "codereviewbuddy",
    instructions=(
        "AI code review buddy — fetch, resolve, and manage PR review comments "
        "across Unblocked, Devin, and CodeRabbit with staleness detection."
    ),
)


@mcp.tool
def list_review_comments(
    pr_number: int,
    repo: str | None = None,
    status: str | None = None,
) -> list[dict]:
    """List all review threads for a PR with reviewer identification and staleness.

    Args:
        pr_number: The PR number to fetch comments for.
        repo: Repository in "owner/repo" format. Auto-detected from git remote if not provided.
        status: Filter by "resolved" or "unresolved". Returns all if not set.

    Returns:
        List of review threads with thread_id, file, line, reviewer, status, is_stale, and comments.
    """
    threads = comments.list_review_comments(pr_number, repo=repo, status=status)
    return [t.model_dump(mode="json") for t in threads]


@mcp.tool
def resolve_comment(
    pr_number: int,
    thread_id: str,
) -> str:
    """Resolve a specific review thread by its ID.

    Uses the resolveReviewThread GraphQL mutation (not minimizeComment).
    Thread IDs have the PRRT_ prefix.

    Args:
        pr_number: PR number (for context).
        thread_id: The GraphQL node ID (PRRT_...) of the thread to resolve.
    """
    return comments.resolve_comment(pr_number, thread_id)


@mcp.tool
def resolve_stale_comments(
    pr_number: int,
    repo: str | None = None,
) -> ResolveStaleResult:
    """Bulk-resolve all unresolved threads on files that changed since the review.

    Compares each comment's file against the PR's current diff. If the file
    has been modified, the comment is considered stale and gets resolved.

    Args:
        pr_number: PR number.
        repo: Repository in "owner/repo" format. Auto-detected if not provided.

    Returns:
        Dict with resolved_count and resolved_thread_ids.
    """
    return comments.resolve_stale_comments(pr_number, repo=repo)


@mcp.tool
def reply_to_comment(
    pr_number: int,
    thread_id: str,
    body: str,
    repo: str | None = None,
) -> str:
    """Reply to a specific review thread.

    Args:
        pr_number: PR number.
        thread_id: The GraphQL node ID (PRRT_...) of the thread to reply to.
        body: Reply text.
        repo: Repository in "owner/repo" format. Auto-detected if not provided.
    """
    return comments.reply_to_comment(pr_number, thread_id, body, repo=repo)


@mcp.tool
def request_rereview(
    pr_number: int,
    reviewer: str | None = None,
    repo: str | None = None,
) -> dict:
    """Trigger a re-review for AI reviewers on a PR.

    Handles per-reviewer differences automatically:
    - Unblocked: posts "@unblocked please re-review" comment (manual trigger needed)
    - Devin: auto-triggers on push (no action needed)
    - CodeRabbit: auto-triggers on push (no action needed)

    Args:
        pr_number: PR number.
        reviewer: Specific reviewer to trigger (e.g. "unblocked"). Triggers all if not set.
        repo: Repository in "owner/repo" format. Auto-detected if not provided.

    Returns:
        Dict with "triggered" (manually triggered reviewers) and "auto_triggers" (no action needed).
    """
    return rereview.request_rereview(pr_number, reviewer=reviewer, repo=repo)


def check_prerequisites() -> None:
    """Verify that gh CLI is installed and authenticated."""
    try:
        username = gh.check_auth()
        logger.info("Authenticated as %s", username)
    except gh.GhNotFoundError:
        logger.exception("gh CLI not found. Install: https://cli.github.com/")
        raise
    except gh.GhNotAuthenticatedError:
        logger.exception("gh CLI not authenticated. Run: gh auth login")
        raise
