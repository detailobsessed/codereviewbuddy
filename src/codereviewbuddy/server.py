"""FastMCP server for codereviewbuddy.

Exposes tools for managing AI code review comments on GitHub PRs.
Authentication is handled by the `gh` CLI — no tokens needed.
"""

import logging

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware
from fastmcp.server.middleware.logging import LoggingMiddleware
from fastmcp.server.middleware.timing import TimingMiddleware

from codereviewbuddy import gh
from codereviewbuddy.models import (  # noqa: TC001 - runtime imports needed for FastMCP schema generation
    CreateIssueResult,
    RereviewResult,
    ResolveStaleResult,
    ReviewSummary,
    UpdateCheckResult,
)
from codereviewbuddy.tools import comments, issues, rereview, version, wait

logger = logging.getLogger(__name__)

mcp = FastMCP(
    "codereviewbuddy",
    instructions="""\
AI code review buddy — fetch, resolve, and manage PR review comments
across Unblocked, Devin, and CodeRabbit with staleness detection.

## Typical workflow after pushing a fix

1. Call `list_review_comments` to see all threads with staleness info.
   - Check `reviews_in_progress` — if true, reviewers haven't finished yet.
   - Check `reviewer_statuses` for per-reviewer detail.
2. If reviews are still pending, call `wait_for_reviews` to poll until they complete.
3. For threads on files you changed, call `resolve_stale_comments` to batch-resolve them.
4. Reply to non-stale threads with `reply_to_comment` if you addressed them differently.
5. Call `request_rereview` to trigger a fresh review cycle.

## Review status detection

`list_review_comments` automatically detects whether AI reviewers have finished reviewing
the latest push. It compares each reviewer's most recent comment timestamp against the
PR's latest commit timestamp. If a reviewer posted before the latest push, their status
is "pending". Only reviewers that have actually commented on the PR are tracked — we
don't assume which reviewers are installed.

## Reviewer behavior differences

- **Unblocked**: Does NOT auto-review on new pushes. You MUST call `request_rereview`
  (which posts "@unblocked please re-review") after pushing fixes.
- **Devin**: Auto-triggers a new review on every push. No action needed, but you can
  still call `request_rereview` if you want to force one.
- **CodeRabbit**: Auto-triggers on push. Same as Devin — no action needed.

## Staleness

A comment is "stale" when the file it references has been modified in the latest push.
Stale comments are safe to batch-resolve with `resolve_stale_comments` since the code
they reviewed has changed.

## Auto-resolving reviewers

`resolve_stale_comments` automatically skips threads from reviewers that auto-resolve
their own comments when they detect a fix (Devin, CodeRabbit). Only threads from
reviewers that do NOT auto-resolve (e.g. Unblocked) are batch-resolved. The result
includes a `skipped_count` field showing how many threads were left for the reviewer
to handle.

## Tracking useful suggestions

When review comments contain genuinely useful improvement suggestions (not bugs being
fixed in the PR), use `create_issue_from_comment` to create a GitHub issue. Use labels
to classify: type labels (bug, enhancement, documentation) and priority labels (P0-P3).
Don't file issues for nitpicks or things already being addressed.

## Updates

Call `check_for_updates` periodically to see if a newer version is available on PyPI.
If an update is found, suggest the user run the upgrade command and restart their
MCP client.
""",
)

mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=True, transform_errors=True))
mcp.add_middleware(TimingMiddleware())
mcp.add_middleware(LoggingMiddleware(include_payloads=True, max_payload_length=500))


@mcp.tool
async def list_review_comments(
    pr_number: int,
    repo: str | None = None,
    status: str | None = None,
) -> ReviewSummary:
    """List all review threads for a PR with reviewer identification and staleness.

    Args:
        pr_number: The PR number to fetch comments for.
        repo: Repository in "owner/repo" format. Auto-detected from git remote if not provided.
        status: Filter by "resolved" or "unresolved". Returns all if not set.

    Returns:
        List of review threads with thread_id, file, line, reviewer, status, is_stale, and comments.
    """
    ctx = get_context()
    return await comments.list_review_comments(pr_number, repo=repo, status=status, ctx=ctx)


@mcp.tool
async def list_stack_review_comments(
    pr_numbers: list[int],
    repo: str | None = None,
    status: str | None = None,
) -> dict[int, ReviewSummary]:
    """List review threads for multiple PRs in a stack, grouped by PR number.

    Collapses N tool calls into 1 for the common stacked-PR review workflow.
    Gives the agent a full picture of the review state before deciding what to fix.

    Args:
        pr_numbers: List of PR numbers to fetch comments for.
        repo: Repository in "owner/repo" format. Auto-detected from git remote if not provided.
        status: Filter by "resolved" or "unresolved". Returns all if not set.

    Returns:
        Dict mapping each PR number to its list of review threads.
    """
    ctx = get_context()
    return await comments.list_stack_review_comments(pr_numbers, repo=repo, status=status, ctx=ctx)


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
async def resolve_stale_comments(
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
    ctx = get_context()
    return await comments.resolve_stale_comments(pr_number, repo=repo, ctx=ctx)


@mcp.tool
def reply_to_comment(
    pr_number: int,
    thread_id: str,
    body: str,
    repo: str | None = None,
) -> str:
    """Reply to a specific review thread, PR-level review, or bot comment.

    Supports inline review threads (PRRT_ IDs), PR-level reviews (PRR_ IDs),
    and issue comments (IC_ IDs, e.g. bot comments from codecov/netlify).

    Args:
        pr_number: PR number.
        thread_id: The node ID (PRRT_..., PRR_..., or IC_...) to reply to.
        body: Reply text.
        repo: Repository in "owner/repo" format. Auto-detected if not provided.
    """
    return comments.reply_to_comment(pr_number, thread_id, body, repo=repo)


@mcp.tool
async def request_rereview(
    pr_number: int,
    reviewer: str | None = None,
    repo: str | None = None,
) -> RereviewResult:
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
    ctx = get_context()
    return await rereview.request_rereview(pr_number, reviewer=reviewer, repo=repo, ctx=ctx)


@mcp.tool
def create_issue_from_comment(
    pr_number: int,
    thread_id: str,
    title: str,
    labels: list[str] | None = None,
    repo: str | None = None,
) -> CreateIssueResult:
    """Create a GitHub issue from a noteworthy review comment.

    Use this to track genuinely useful improvement suggestions from AI reviewers
    that aren't bugs being fixed in the current PR.

    Args:
        pr_number: PR number the comment belongs to.
        thread_id: The GraphQL node ID (PRRT_...) of the review thread.
        title: Issue title summarizing the suggestion.
        labels: Optional labels (e.g. ["enhancement", "P2"]). Use repo labels for type and priority.
        repo: Repository in "owner/repo" format. Auto-detected if not provided.

    Returns:
        Created issue number, URL, and title.
    """
    return issues.create_issue_from_comment(
        pr_number,
        thread_id,
        title,
        labels=labels,
        repo=repo,
    )


@mcp.tool
async def wait_for_reviews(
    pr_number: int,
    repo: str | None = None,
    timeout: int = 300,
    poll_interval: int = 30,
) -> ReviewSummary:
    """Wait for AI reviewers to finish reviewing the latest push on a PR.

    Polls review status at regular intervals and returns when all known reviewers
    have completed, or when the timeout is reached. Only tracks reviewers that have
    previously commented on this PR.

    Args:
        pr_number: PR number to monitor.
        repo: Repository in "owner/repo" format. Auto-detected if not provided.
        timeout: Maximum seconds to wait (default 300 = 5 minutes).
        poll_interval: Seconds between polls (default 30).

    Returns:
        Final ReviewSummary. Check reviews_in_progress — if still true, timeout was reached.
    """
    ctx = get_context()
    return await wait.wait_for_reviews(
        pr_number,
        repo=repo,
        timeout=timeout,
        poll_interval=poll_interval,
        ctx=ctx,
    )


@mcp.tool
async def check_for_updates() -> UpdateCheckResult:
    """Check if a newer version of codereviewbuddy is available on PyPI.

    Compares the running server version against the latest published release.
    If an update is available, returns the upgrade command for the user.
    Useful for long-running MCP sessions where the server may fall behind.

    Returns:
        Current version, latest version, whether an update is available, and upgrade command.
    """
    return await version.check_for_updates()


def main() -> None:
    """Run the codereviewbuddy MCP server."""
    check_prerequisites()
    mcp.run()


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
