"""FastMCP server for codereviewbuddy.

Exposes tools for managing AI code review comments on GitHub PRs.
Authentication is handled by the `gh` CLI — no tokens needed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context
from fastmcp.server.lifespan import lifespan
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware
from fastmcp.server.middleware.logging import LoggingMiddleware
from fastmcp.server.middleware.ping import PingMiddleware
from fastmcp.server.middleware.timing import TimingMiddleware

from codereviewbuddy import gh
from codereviewbuddy.config import load_config, set_config
from codereviewbuddy.middleware import WriteOperationMiddleware
from codereviewbuddy.models import (
    CreateIssueResult,
    PRDescriptionReviewResult,
    RereviewResult,
    ResolveStaleResult,
    ReviewSummary,
    StackReviewStatusResult,
    TriageResult,
)
from codereviewbuddy.reviewers import apply_config
from codereviewbuddy.tools import comments, descriptions, issues, rereview, stack

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)


def _resolve_pr_number(pr_number: int | None) -> int:
    """Resolve pr_number, auto-detecting from the current branch if not provided."""
    if pr_number is not None:
        return pr_number
    return gh.get_current_pr_number()


@lifespan
async def check_gh_cli(server: FastMCP) -> AsyncIterator[dict[str, object] | None]:  # noqa: ARG001, RUF029
    """Verify gh CLI is installed and authenticated on server startup."""
    check_prerequisites()
    config = load_config()
    set_config(config)
    apply_config(config)
    yield {}


mcp = FastMCP(
    "codereviewbuddy",
    lifespan=check_gh_cli,
    instructions="""\
AI code review buddy — fetch, resolve, and manage PR review comments
across Unblocked, Devin, and CodeRabbit with staleness detection.

## Stack discovery

`list_review_comments` automatically discovers the full PR stack by walking the branch
chain (works with Graphite, Git Town, or manual stacking). The `stack` field in the
response lists all PRs in order, bottom-to-top. This is cached per session — the first
call fetches, subsequent calls reuse. Use `list_stack_review_comments` with the
discovered PR numbers to get full thread details across the stack.

## Typical workflow after pushing a fix

1. Call `summarize_review_status` for a quick stack-wide overview (severity counts,
   no full bodies — saves tokens). It auto-discovers the stack if you omit `pr_numbers`.
2. Call `triage_review_comments` with the PR numbers to get only threads needing action.
   Each item has a pre-classified severity and suggested action (`fix`, `reply`, or
   `create_issue`). This replaces manual filtering of `list_review_comments` output.
3. For threads on files you changed, call `resolve_stale_comments` to batch-resolve them.
4. Reply to non-stale threads with `reply_to_comment` if you addressed them differently.
5. Call `request_rereview` to trigger a fresh review cycle.
6. If you need full thread details (all comments, reviewer statuses), fall back to
   `list_review_comments` for a specific PR.

## Review status detection

`list_review_comments` and `summarize_review_status` automatically detect whether AI
reviewers have finished reviewing the latest push. They compare each reviewer's most
recent comment timestamp against the PR's latest commit timestamp. If a reviewer posted
before the latest push, their status is "pending". Only reviewers that have actually
commented on the PR are tracked — we don't assume which reviewers are installed.

## Responding to review comments

Always reply to bug (🔴) and flagged (🚩) level comments with `reply_to_comment`
explaining what you fixed, the commit hash, and any regression test added. Do not
silently push — reviewers need to see that their finding was acknowledged. For info
(📝) and warning (🟡) comments, a reply is optional but appreciated when you made
changes based on them.

## Reviewer behavior differences

Some reviewers auto-trigger a new review on every push (e.g. Devin, CodeRabbit) while
others require a manual trigger via `request_rereview` (e.g. Unblocked). The trigger
message is configurable per-reviewer via `rereview_message` in `.codereviewbuddy.toml`.

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

## Per-reviewer configuration

The server loads `.codereviewbuddy.toml` from the project root at startup. This config
controls per-reviewer resolve policy:

- **`resolve_levels`**: Which severity levels you're allowed to resolve. If you try to
  resolve a thread whose severity (🔴 bug, 🚩 flagged, 🟡 warning, 📝 info) exceeds
  the allowed levels, the server will **block** the resolve and return an error.
- **`auto_resolve_stale`**: Whether `resolve_stale_comments` touches this reviewer's
  threads at all.
- **`enabled`**: Whether this reviewer's threads appear in results.

With default config, only 📝 info threads from Devin can be resolved. All severity
levels from Unblocked can be resolved. No CodeRabbit threads can be resolved.
If `resolve_comment` or `resolve_stale_comments` returns a "blocked by config" error,
do NOT retry — the config is intentional. Inform the user about the blocked thread
and its severity level instead.

## Tracking useful suggestions

When review comments contain genuinely useful improvement suggestions (not bugs being
fixed in the PR), use `create_issue_from_comment` to create a GitHub issue. Use labels
to classify: type labels (bug, enhancement, documentation) and priority labels (P0-P3).
Don't file issues for nitpicks or things already being addressed.

""",
)

mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=True, transform_errors=True))
mcp.add_middleware(TimingMiddleware())
mcp.add_middleware(LoggingMiddleware(include_payloads=True, max_payload_length=500))
mcp.add_middleware(PingMiddleware(interval_ms=30_000))
mcp.add_middleware(WriteOperationMiddleware())


@mcp.tool
async def list_review_comments(
    pr_number: int | None = None,
    repo: str | None = None,
    status: str | None = None,
) -> ReviewSummary:
    """List all review threads for a PR with reviewer identification and staleness.

    Args:
        pr_number: The PR number to fetch comments for. Auto-detected from current branch if omitted.
        repo: Repository in "owner/repo" format. Auto-detected from git remote if not provided.
        status: Filter by "resolved" or "unresolved". Returns all if not set.

    Returns:
        List of review threads with thread_id, file, line, reviewer, status, is_stale, and comments.
    """
    try:
        pr_number = _resolve_pr_number(pr_number)
        ctx = get_context()
        return await comments.list_review_comments(pr_number, repo=repo, status=status, ctx=ctx)
    except Exception as exc:
        logger.exception("list_review_comments failed for PR #%s", pr_number)
        return ReviewSummary(threads=[], error=f"Error: {exc}")
    except asyncio.CancelledError:
        logger.warning("list_review_comments cancelled for PR #%s", pr_number)
        return ReviewSummary(threads=[], error="Cancelled")


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
    try:
        ctx = get_context()
        return await comments.list_stack_review_comments(pr_numbers, repo=repo, status=status, ctx=ctx)
    except Exception as exc:
        logger.exception("list_stack_review_comments failed")
        return {pr: ReviewSummary(threads=[], error=f"Error: {exc}") for pr in pr_numbers}
    except asyncio.CancelledError:
        logger.warning("list_stack_review_comments cancelled")
        return {pr: ReviewSummary(threads=[], error="Cancelled") for pr in pr_numbers}


@mcp.tool
def resolve_comment(
    thread_id: str,
    pr_number: int | None = None,
) -> str:
    """Resolve a specific review thread by its ID.

    Uses the resolveReviewThread GraphQL mutation (not minimizeComment).
    Thread IDs have the PRRT_ prefix.

    Args:
        pr_number: PR number (for context). Auto-detected from current branch if omitted.
        thread_id: The GraphQL node ID (PRRT_...) of the thread to resolve.
    """
    try:
        pr_number = _resolve_pr_number(pr_number)
        return comments.resolve_comment(pr_number, thread_id)
    except Exception as exc:
        logger.exception("resolve_comment failed for %s on PR #%s", thread_id, pr_number)
        return f"Error resolving {thread_id} on PR #{pr_number}: {exc}"


@mcp.tool
async def resolve_stale_comments(
    pr_number: int | None = None,
    repo: str | None = None,
) -> ResolveStaleResult:
    """Bulk-resolve all unresolved threads on files that changed since the review.

    Compares each comment's file against the PR's current diff. If the file
    has been modified, the comment is considered stale and gets resolved.

    Args:
        pr_number: PR number. Auto-detected from current branch if omitted.
        repo: Repository in "owner/repo" format. Auto-detected if not provided.

    Returns:
        Dict with resolved_count and resolved_thread_ids.
    """
    try:
        pr_number = _resolve_pr_number(pr_number)
        ctx = get_context()
        return await comments.resolve_stale_comments(pr_number, repo=repo, ctx=ctx)
    except Exception as exc:
        logger.exception("resolve_stale_comments failed for PR #%s", pr_number)
        return ResolveStaleResult(resolved_count=0, resolved_thread_ids=[], error=f"Error: {exc}")
    except asyncio.CancelledError:
        logger.warning("resolve_stale_comments cancelled for PR #%s", pr_number)
        return ResolveStaleResult(resolved_count=0, resolved_thread_ids=[], error="Cancelled")


@mcp.tool
def reply_to_comment(
    thread_id: str,
    body: str,
    pr_number: int | None = None,
    repo: str | None = None,
) -> str:
    """Reply to a specific review thread, PR-level review, or bot comment.

    Supports inline review threads (PRRT_ IDs), PR-level reviews (PRR_ IDs),
    and issue comments (IC_ IDs, e.g. bot comments from codecov/netlify).

    Args:
        pr_number: PR number. Auto-detected from current branch if omitted.
        thread_id: The node ID (PRRT_..., PRR_..., or IC_...) to reply to.
        body: Reply text.
        repo: Repository in "owner/repo" format. Auto-detected if not provided.
    """
    try:
        pr_number = _resolve_pr_number(pr_number)
        return comments.reply_to_comment(pr_number, thread_id, body, repo=repo)
    except Exception as exc:
        logger.exception("reply_to_comment failed for %s on PR #%s", thread_id, pr_number)
        return f"Error replying to {thread_id} on PR #{pr_number}: {exc}"


@mcp.tool
async def request_rereview(
    pr_number: int | None = None,
    reviewer: str | None = None,
    repo: str | None = None,
) -> RereviewResult:
    """Trigger a re-review for AI reviewers on a PR.

    Handles per-reviewer differences automatically. Reviewers that need manual
    triggers get a configurable comment posted (see ``rereview_message`` in
    ``.codereviewbuddy.toml``). Reviewers that auto-trigger on push are reported
    as needing no action.

    Args:
        pr_number: PR number. Auto-detected from current branch if omitted.
        reviewer: Specific reviewer to trigger (e.g. "unblocked"). Triggers all if not set.
        repo: Repository in "owner/repo" format. Auto-detected if not provided.

    Returns:
        Dict with "triggered" (manually triggered reviewers) and "auto_triggers" (no action needed).
    """
    try:
        pr_number = _resolve_pr_number(pr_number)
        ctx = get_context()
        return await rereview.request_rereview(pr_number, reviewer=reviewer, repo=repo, ctx=ctx)
    except Exception as exc:
        logger.exception("request_rereview failed for PR #%s", pr_number)
        return RereviewResult(triggered=[], auto_triggers=[], error=f"Error: {exc}")
    except asyncio.CancelledError:
        logger.warning("request_rereview cancelled for PR #%s", pr_number)
        return RereviewResult(triggered=[], auto_triggers=[], error="Cancelled")


@mcp.tool
def create_issue_from_comment(
    thread_id: str,
    title: str,
    pr_number: int | None = None,
    labels: list[str] | None = None,
    repo: str | None = None,
) -> CreateIssueResult:
    """Create a GitHub issue from a noteworthy review comment.

    Use this to track genuinely useful improvement suggestions from AI reviewers
    that aren't bugs being fixed in the current PR.

    Args:
        pr_number: PR number the comment belongs to. Auto-detected from current branch if omitted.
        thread_id: The GraphQL node ID (PRRT_...) of the review thread.
        title: Issue title summarizing the suggestion.
        labels: Optional labels (e.g. ["enhancement", "P2"]). Use repo labels for type and priority.
        repo: Repository in "owner/repo" format. Auto-detected if not provided.

    Returns:
        Created issue number, URL, and title.
    """
    try:
        pr_number = _resolve_pr_number(pr_number)
        return issues.create_issue_from_comment(
            pr_number,
            thread_id,
            title,
            labels=labels,
            repo=repo,
        )
    except Exception as exc:
        logger.exception("create_issue_from_comment failed for %s on PR #%s", thread_id, pr_number)
        return CreateIssueResult(issue_number=0, issue_url="", title=title, error=f"Error: {exc}")


@mcp.tool
async def review_pr_descriptions(
    pr_numbers: list[int],
    repo: str | None = None,
) -> PRDescriptionReviewResult:
    """Review PR descriptions across a stack for quality issues.

    Returns each PR's title, body, linked issues, and missing elements
    (empty body, boilerplate only, no linked issues, too short).

    Args:
        pr_numbers: List of PR numbers to review.
        repo: Repository in "owner/repo" format. Auto-detected from git remote if not provided.

    Returns:
        Analysis results for each PR's description.
    """
    try:
        ctx = get_context()
        return await descriptions.review_pr_descriptions(pr_numbers, repo=repo, ctx=ctx)
    except Exception as exc:
        logger.exception("review_pr_descriptions failed")
        return PRDescriptionReviewResult(error=f"Error: {exc}")
    except asyncio.CancelledError:
        logger.warning("review_pr_descriptions cancelled")
        return PRDescriptionReviewResult(error="Cancelled")


@mcp.tool
async def summarize_review_status(
    pr_numbers: list[int] | None = None,
    repo: str | None = None,
) -> StackReviewStatusResult:
    """Get a lightweight stack-wide review status overview with severity counts.

    Much fewer tokens than full thread data — use this to quickly scan which PRs
    need attention before diving into details with ``list_review_comments``.

    When ``pr_numbers`` is omitted, auto-discovers the stack from the current branch
    using the same branch-chain walking as ``list_review_comments``.

    Args:
        pr_numbers: PR numbers to summarize. Auto-discovers stack if omitted.
        repo: Repository in "owner/repo" format. Auto-detected if not provided.

    Returns:
        Per-PR status with unresolved/resolved counts, severity breakdown
        (bugs, flagged, warnings, info), staleness, and reviewer progress.
    """
    try:
        ctx = get_context()
        return await stack.summarize_review_status(pr_numbers=pr_numbers, repo=repo, ctx=ctx)
    except Exception as exc:
        logger.exception("summarize_review_status failed")
        return StackReviewStatusResult(error=f"Error: {exc}")
    except asyncio.CancelledError:
        logger.warning("summarize_review_status cancelled")
        return StackReviewStatusResult(error="Cancelled")


@mcp.tool
async def triage_review_comments(
    pr_numbers: list[int],
    repo: str | None = None,
    owner_logins: list[str] | None = None,
) -> TriageResult:
    """Show only review threads that need agent action — no noise, no full bodies.

    Filters out PR-level reviews, already-replied threads, and resolved threads.
    Pre-classifies severity and suggests an action for each thread.

    Also flags "noted for followup" replies that forgot to include a GH issue
    reference — these need a ``create_issue_from_comment`` call.

    Args:
        pr_numbers: PR numbers to triage (use stack from ``summarize_review_status``).
        repo: Repository in "owner/repo" format. Auto-detected if not provided.
        owner_logins: GitHub usernames considered "ours" (agent + human).
            Defaults to ["ichoosetoaccept"]. Add your own username if needed.

    Returns:
        TriageResult with actionable items sorted by severity (bugs first),
        plus counts of items needing fixes, replies, or issue creation.
    """
    try:
        ctx = get_context()
        return await comments.triage_review_comments(pr_numbers, repo=repo, owner_logins=owner_logins, ctx=ctx)
    except Exception as exc:
        logger.exception("triage_review_comments failed")
        return TriageResult(error=f"Error: {exc}")
    except asyncio.CancelledError:
        logger.warning("triage_review_comments cancelled")
        return TriageResult(error="Cancelled")


def main() -> None:
    """Run the codereviewbuddy MCP server, or handle CLI subcommands."""
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "init":
        print("'codereviewbuddy init' has been renamed to 'codereviewbuddy config --init'")  # noqa: T201
        _config_cmd(["--init"])
        return

    if len(sys.argv) > 1 and sys.argv[1] == "config":
        _config_cmd(sys.argv[2:])
        return

    from codereviewbuddy.io_tap import install_io_tap

    install_io_tap()
    mcp.run()


def _config_cmd(args: list[str]) -> None:
    """Handle ``codereviewbuddy config [--init | --update | --clean]``."""
    from codereviewbuddy.config import clean_config, init_config, update_config

    if "--init" in args:
        init_config()
    elif "--update" in args:
        update_config()
    elif "--clean" in args:
        clean_config()
    else:
        print("Usage: codereviewbuddy config [--init | --update | --clean]")  # noqa: T201
        print()  # noqa: T201
        print("  --init    Create a new .codereviewbuddy.toml with all defaults")  # noqa: T201
        print("  --update  Add new sections and comment out deprecated keys")  # noqa: T201
        print("  --clean   Remove deprecated keys entirely")  # noqa: T201
        raise SystemExit(1)


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
