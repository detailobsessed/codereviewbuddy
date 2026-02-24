"""FastMCP server for codereviewbuddy.

Exposes tools for managing AI code review comments on GitHub PRs.
Authentication is handled by the `gh` CLI — no tokens needed.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import os
from typing import TYPE_CHECKING, Annotated, Literal

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context
from fastmcp.server.lifespan import lifespan
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware
from fastmcp.server.middleware.logging import LoggingMiddleware
from fastmcp.server.middleware.ping import PingMiddleware
from fastmcp.server.middleware.timing import TimingMiddleware
from fastmcp.utilities.async_utils import call_sync_fn_in_threadpool
from pydantic import Field

from codereviewbuddy import gh
from codereviewbuddy.config import get_config, load_config, set_config
from codereviewbuddy.middleware import WriteOperationMiddleware
from codereviewbuddy.models import (
    CIDiagnosisResult,
    ConfigInfo,
    CreateIssueResult,
    PRDescriptionReviewResult,
    ResolveStaleResult,
    ReviewSummary,
    StackActivityResult,
    StackReviewStatusResult,
    TriageResult,
)
from codereviewbuddy.tools import ci, comments, descriptions, issues, stack

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastmcp.server.context import Context

logger = logging.getLogger(__name__)
_FASTMCP_TASK_ROUTING_MODULE = "fastmcp.server.tasks.routing"
write_operation_middleware = WriteOperationMiddleware()


_WORKSPACE_HELP = (
    "Workspace not detected: your MCP client did not provide workspace roots "
    "and CRB_WORKSPACE is not set. The server cannot reliably auto-detect "
    "your repository or branch.\n"
    "Fix: pass `repo` and `pr_number` (or `pr_numbers`) explicitly."
)


async def _get_workspace_cwd(ctx: Context | None = None) -> str | None:
    """Resolve the user's workspace directory for ``gh`` CLI commands.

    Priority:
    1. MCP roots — per-window, protocol-correct (works with multi-window setups).
    2. ``CRB_WORKSPACE`` env var — fallback for clients that don't send roots.
    3. ``None`` — falls back to the server's process cwd.

    See #142, #174.
    """
    # 1. MCP roots (per-window — correct for multi-window setups)
    if ctx is not None:
        try:
            roots = await ctx.list_roots()
            if roots:
                from urllib.parse import unquote, urlparse  # noqa: PLC0415

                parsed = urlparse(str(roots[0].uri))
                if parsed.scheme == "file" and parsed.path:
                    path = unquote(parsed.path)
                    logger.info("Workspace from MCP roots: %s", path)
                    return path
                logger.warning(
                    "MCP root URI has unsupported scheme %r (expected 'file')",
                    parsed.scheme,
                )
            else:
                logger.warning("MCP client returned empty roots list")
        except Exception as exc:
            logger.warning(
                "MCP roots request failed: %s: %s",
                type(exc).__name__,
                exc,
            )

    # 2. CRB_WORKSPACE env var (fallback for clients without MCP roots)
    env_ws = os.environ.get("CRB_WORKSPACE")
    if env_ws:
        logger.debug("Workspace from CRB_WORKSPACE: %s", env_ws)
        return env_ws

    # 3. Process cwd (weakest — almost certainly wrong in MCP context)
    if ctx is not None:
        logger.warning(
            "Workspace not detected (MCP roots unavailable, CRB_WORKSPACE not set). "
            "Auto-detection of repo/branch will use the server's process directory "
            "and may produce wrong results.",
        )
    return None


def _check_auto_detect_prerequisites(
    cwd: str | None,
    *,
    has_pr: bool,
    has_repo: bool,
) -> None:
    """Raise if workspace not detected and auto-detection parameters are missing.

    This prevents confusing errors like "no pull requests found for branch main"
    when the server's process cwd happens to be a different git repo.
    See #174.
    """
    if cwd is not None:
        return  # workspace detected — auto-detection is safe

    if has_pr and has_repo:
        return  # all params explicit — no auto-detection needed

    missing = []
    if not has_pr:
        missing.append("`pr_number`/`pr_numbers`")
    if not has_repo:
        missing.append("`repo`")

    msg = f"{_WORKSPACE_HELP}\nMissing: {', '.join(missing)}"
    raise gh.GhError(msg)


def _resolve_pr_number(pr_number: int | None, cwd: str | None = None) -> int:
    """Resolve pr_number, auto-detecting from the current branch if not provided."""
    if pr_number is not None:
        return pr_number
    return gh.get_current_pr_number(cwd=cwd)


@lifespan
async def check_gh_cli(server: FastMCP) -> AsyncIterator[dict[str, object] | None]:  # noqa: ARG001, RUF029
    """Verify gh CLI is installed and authenticated on server startup."""
    check_fastmcp_runtime()
    check_prerequisites()
    config = load_config()
    set_config(config)
    write_operation_middleware.configure_diagnostics(
        heartbeat_enabled=config.diagnostics.tool_call_heartbeat,
        heartbeat_interval_ms=config.diagnostics.heartbeat_interval_ms,
        include_args_fingerprint=config.diagnostics.include_args_fingerprint,
    )
    yield {}


mcp = FastMCP(
    "codereviewbuddy",
    lifespan=check_gh_cli,
    instructions="""\
AI code review buddy — fetch, resolve, and manage PR review comments
across Unblocked, Devin, CodeRabbit, and Greptile with staleness detection.

## Stack discovery

`list_review_comments` automatically discovers the full PR stack by walking the branch
chain (works with Graphite, Git Town, or manual stacking). The `stack` field in the
response lists all PRs in order, bottom-to-top. This is cached per session — the first
call fetches, subsequent calls reuse. Use `list_stack_review_comments` with the
discovered PR numbers to get full thread details across the stack.

## Review workflow — step by step

Follow this exact sequence when reviewing or responding to AI review comments:

1. **Summarize** — `summarize_review_status()` (omit `pr_numbers` → auto-discover stack).
   Check which PRs have unresolved threads and their severity breakdown.
2. **Triage** — `triage_review_comments(pr_numbers)` with the discovered PR numbers.
   Returns only actionable threads, pre-classified by severity with a suggested action.
3. **Process by severity** — work through findings **bugs first**, then flagged, then
   warnings, then info. Never process in file order — severity order matters.
4. **Fix** — for each `action: "fix"` item (🔴 bug, 🚩 flagged), implement the fix.
5. **Reply** — call `reply_to_comment` for every bug and flagged thread you fixed,
   explaining what you changed and the commit hash. **Never silently resolve bug (🔴)
   or flagged (🚩) threads** — always reply first.
6. **Resolve stale** — `resolve_stale_comments` to batch-resolve threads on changed files.
7. **File issues** — for `action: "create_issue"` items, call `create_issue_from_comment`.
8. **Re-review** — after pushing fixes, trigger re-reviews for manual-trigger reviewers
   (see "Re-review triggers" section below).
9. **Verify** — `summarize_review_status()` again to confirm all bugs are addressed.

For full thread details (all comments, reviewer statuses), fall back to
`list_review_comments` for a specific PR — but prefer the triage workflow above.

## Responding to review comments

Always reply to bug (🔴) and flagged (🚩) level comments with `reply_to_comment`
explaining what you fixed, the commit hash, and any regression test added. Do not
silently push — reviewers need to see that their finding was acknowledged. For info
(📝) and warning (🟡) comments, a reply is optional but appreciated when you made
changes based on them.

## Re-review triggers

After pushing fixes, some reviewers need a manual trigger to start a new review.
Post a PR comment using `gh pr comment <number> --repo <owner/repo> --body "<message>"`:

| Reviewer   | Auto-reviews on push? | Trigger comment                   |
|------------|----------------------|-----------------------------------|
| Devin      | ✅ Yes                | —                                 |
| CodeRabbit | ✅ Yes                | —                                 |
| Unblocked  | ❌ No                 | `@unblocked please re-review`     |
| Greptile   | ❌ No                 | `@greptileai review`              |

Greptile does NOT re-review on force push despite documentation suggesting otherwise.

## Staleness

A comment is "stale" when the file it references has been modified in the latest push.
Stale comments are safe to batch-resolve with `resolve_stale_comments` since the code
they reviewed has changed.

## Auto-resolving reviewers

`resolve_stale_comments` automatically skips threads from reviewers that auto-resolve
their own comments when they detect a fix (CodeRabbit). Only threads from reviewers
that do NOT auto-resolve (e.g. Unblocked) are batch-resolved. The result includes a
`skipped_count` field showing how many threads were left for the reviewer to handle.

## Per-reviewer configuration

Each reviewer adapter defines sensible defaults (e.g. Devin only allows resolving
info-level threads, CodeRabbit blocks all resolution). Users can override these via
`CRB_REVIEWERS` as a JSON string in the MCP client config env block:

    "CRB_REVIEWERS": "{\"devin\": {\"enabled\": false}}"

Call `show_config` to inspect the active configuration. Overridable fields per reviewer:

- **`resolve_levels`**: Which severity levels you're allowed to resolve. If you try to
  resolve a thread whose severity (🔴 bug, 🚩 flagged, 🟡 warning, 📝 info) exceeds
  the allowed levels, the server will **block** the resolve and return an error.
- **`auto_resolve_stale`**: Whether `resolve_stale_comments` touches this reviewer's
  threads at all.
- **`enabled`**: Whether this reviewer's threads appear in results.

If `resolve_comment` or `resolve_stale_comments` returns a "blocked by config" error,
do NOT retry — the config is intentional. Inform the user about the blocked thread
and its severity level instead.

## Important: repo parameter

All review tools auto-detect the repository from the current workspace (via
MCP roots sent by your client, or `CRB_WORKSPACE` env var as fallback).
The `repo` parameter on each tool is only needed when auto-detection fails.
**Never use the self-improvement repo
(`CRB_SELF_IMPROVEMENT__REPO`) for review operations** — that repo is exclusively
for filing issues about this MCP server itself, not for reviewing PRs.

## Tracking useful suggestions

When review comments contain genuinely useful improvement suggestions (not bugs being
fixed in the PR), use `create_issue_from_comment` to create a GitHub issue. Use labels
to classify: type labels (bug, enhancement, documentation) and priority labels (P0-P3).
Don't file issues for nitpicks or things already being addressed.

## Self-improvement (server bug reports only)

If you encounter errors, missing capabilities, or find yourself repeatedly working
around a limitation of this MCP server, create a GitHub issue describing:
- What you were trying to do
- What went wrong or what's missing
- A proposed solution if you have one

Use `gh issue create` to file the issue against the repo in `CRB_SELF_IMPROVEMENT__REPO`.
Label it `agent-reported` plus any relevant type/priority labels. **This is only for
issues with the MCP server itself — never use this repo for PR review operations.**

This only applies when `CRB_SELF_IMPROVEMENT__ENABLED=true` and `CRB_SELF_IMPROVEMENT__REPO`
is set. Call `show_config` to check settings before filing.

""",
)


def _recovery_error(  # noqa: PLR0911
    exc: Exception,
    *,
    tool_name: str,
    pr_number: int | None = None,
    repo: str | None = None,
) -> str:
    """Build an actionable error message with recovery hints.

    Classifies errors into categories and suggests specific next steps
    so agents can self-correct instead of retrying blindly.
    """
    msg = str(exc)
    low = msg.lower()

    # Auth errors — not retryable, needs human intervention
    if isinstance(exc, gh.GhNotFoundError):
        return f"{tool_name} failed: gh CLI not found. Install it from https://cli.github.com/ then run: gh auth login"
    if isinstance(exc, gh.GhNotAuthenticatedError):
        return f"{tool_name} failed: gh CLI not authenticated. Run: gh auth login"

    # Rate limit — retryable after delay
    if "rate limit" in low or "403" in msg:
        return f"{tool_name} failed: GitHub API rate limit hit. Wait 60 seconds and retry."

    # Not found — likely bad PR number or repo
    if "not found" in low or "could not resolve" in low or "404" in msg:
        hints = [f"{tool_name} failed: resource not found — {msg}."]
        if pr_number:
            hints.append(f"Verify PR #{pr_number} exists and is open.")
        hints.append(f"Verify repo '{repo}' is correct." if repo else "Try passing repo='owner/repo' explicitly if auto-detection failed.")
        return " ".join(hints)

    # Workspace detection failure
    if "workspace" in low or "CRB_WORKSPACE" in msg:
        return (
            f"{tool_name} failed: workspace not detected. "
            "Pass repo='owner/repo' explicitly, or set CRB_WORKSPACE in your MCP client config."
        )

    # GraphQL errors
    if "graphql" in low:
        return f"{tool_name} failed: GitHub GraphQL error — {msg}. This may be a transient issue; retry once."

    # Config-related
    if "blocked by config" in low or "resolve_levels" in low:
        return f"{tool_name} blocked by configuration: {msg}. Call show_config() to inspect active settings."

    # Generic fallback — still better than bare "Error: ..."
    parts = [f"{tool_name} failed: {msg}."]
    if pr_number:
        parts.append(f"Verify PR #{pr_number} exists.")
    if not repo:
        parts.append("Try passing repo='owner/repo' explicitly.")
    return " ".join(parts)


mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=True, transform_errors=True))
mcp.add_middleware(TimingMiddleware())
mcp.add_middleware(LoggingMiddleware(include_payloads=True, max_payload_length=500))
mcp.add_middleware(PingMiddleware(interval_ms=30_000))
mcp.add_middleware(write_operation_middleware)


@mcp.tool(tags={"query"})
async def list_review_comments(
    pr_number: int | None = None,
    repo: str | None = None,
    status: Literal["resolved", "unresolved"] | None = None,
) -> ReviewSummary:
    """List all review threads for a PR with reviewer identification and staleness.

    After fetching, always present a summary to the user:
    1. Group comments by file for readability.
    2. Classify each by severity using the reviewer's indicators:
       🔴 Bug/Critical — must fix before merge
       🚩 Flagged — likely needs a code change
       🟡 Warning — worth addressing but not blocking
       📝 Info — acknowledged, no action required
    3. Mark stale threads (is_stale=true) — these are on files changed since
       the review and can likely be resolved.
    4. Show unresolved count and severity breakdown as a quick summary line.

    Args:
        pr_number: The PR number to fetch comments for. Auto-detected from current branch if omitted.
        repo: Repository in "owner/repo" format. Auto-detected from git remote if not provided.
        status: Filter by "resolved" or "unresolved". Returns all if not set.

    Returns:
        List of review threads with thread_id, file, line, reviewer, status, is_stale, and comments.
    """
    try:
        ctx = get_context()
        cwd = await _get_workspace_cwd(ctx)
        _check_auto_detect_prerequisites(cwd, has_pr=pr_number is not None, has_repo=repo is not None)
        pr_number = _resolve_pr_number(pr_number, cwd=cwd)
        return await comments.list_review_comments(pr_number, repo=repo, status=status, cwd=cwd, ctx=ctx)
    except Exception as exc:
        logger.exception("list_review_comments failed for PR #%s", pr_number)
        return ReviewSummary(threads=[], error=_recovery_error(exc, tool_name="list_review_comments", pr_number=pr_number, repo=repo))
    except asyncio.CancelledError:
        logger.warning("list_review_comments cancelled for PR #%s", pr_number)
        return ReviewSummary(threads=[], error="Cancelled")


@mcp.tool(tags={"query"})
async def list_stack_review_comments(
    pr_numbers: list[int],
    repo: str | None = None,
    status: Literal["resolved", "unresolved"] | None = None,
) -> dict[int, ReviewSummary]:
    """List review threads for multiple PRs in a stack, grouped by PR number.

    Collapses N tool calls into 1 for the common stacked-PR review workflow.
    Gives the agent a full picture of the review state before deciding what to fix.

    After fetching, present a per-PR summary: group by file, classify each
    comment by severity (🔴 Bug, 🚩 Flagged, 🟡 Warning, 📝 Info), and
    highlight stale threads that can likely be resolved.

    Args:
        pr_numbers: List of PR numbers to fetch comments for.
        repo: Repository in "owner/repo" format. Auto-detected from git remote if not provided.
        status: Filter by "resolved" or "unresolved". Returns all if not set.

    Returns:
        Dict mapping each PR number to its list of review threads.
    """
    try:
        ctx = get_context()
        cwd = await _get_workspace_cwd(ctx)
        _check_auto_detect_prerequisites(cwd, has_pr=True, has_repo=repo is not None)
        return await comments.list_stack_review_comments(pr_numbers, repo=repo, status=status, cwd=cwd, ctx=ctx)
    except Exception as exc:
        logger.exception("list_stack_review_comments failed")
        error_msg = _recovery_error(exc, tool_name="list_stack_review_comments", repo=repo)
        return {pr: ReviewSummary(threads=[], error=error_msg) for pr in pr_numbers}
    except asyncio.CancelledError:
        logger.warning("list_stack_review_comments cancelled")
        return {pr: ReviewSummary(threads=[], error="Cancelled") for pr in pr_numbers}


@mcp.tool(tags={"command"})
async def resolve_comment(
    thread_id: str,
    pr_number: int | None = None,
) -> str:
    """Resolve a specific review thread by its ID.

    For inline threads (PRRT_), uses the resolveReviewThread GraphQL mutation.
    For PR-level reviews (PRR_), dismisses the review via dismissPullRequestReview.

    Args:
        pr_number: PR number (for context). Auto-detected from current branch if omitted.
        thread_id: The GraphQL node ID (PRRT_... or PRR_...) to resolve/dismiss.
    """
    try:
        ctx = get_context()
        cwd = await _get_workspace_cwd(ctx)
        _check_auto_detect_prerequisites(cwd, has_pr=pr_number is not None, has_repo=True)
        pr_number = _resolve_pr_number(pr_number, cwd=cwd)
        return comments.resolve_comment(pr_number, thread_id, cwd=cwd)
    except Exception as exc:
        logger.exception("resolve_comment failed for %s on PR #%s", thread_id, pr_number)
        return _recovery_error(exc, tool_name="resolve_comment", pr_number=pr_number)


@mcp.tool(tags={"command"})
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
        ctx = get_context()
        cwd = await _get_workspace_cwd(ctx)
        _check_auto_detect_prerequisites(cwd, has_pr=pr_number is not None, has_repo=repo is not None)
        pr_number = _resolve_pr_number(pr_number, cwd=cwd)
        return await comments.resolve_stale_comments(pr_number, repo=repo, cwd=cwd, ctx=ctx)
    except Exception as exc:
        logger.exception("resolve_stale_comments failed for PR #%s", pr_number)
        return ResolveStaleResult(
            resolved_count=0,
            resolved_thread_ids=[],
            error=_recovery_error(exc, tool_name="resolve_stale_comments", pr_number=pr_number, repo=repo),
        )
    except asyncio.CancelledError:
        logger.warning("resolve_stale_comments cancelled for PR #%s", pr_number)
        return ResolveStaleResult(resolved_count=0, resolved_thread_ids=[], error="Cancelled")


@mcp.tool(tags={"command"})
async def reply_to_comment(
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
        ctx = get_context()
        cwd = await _get_workspace_cwd(ctx)
        _check_auto_detect_prerequisites(cwd, has_pr=pr_number is not None, has_repo=repo is not None)
        pr_number = _resolve_pr_number(pr_number, cwd=cwd)
        return comments.reply_to_comment(pr_number, thread_id, body, repo=repo, cwd=cwd)
    except Exception as exc:
        logger.exception("reply_to_comment failed for %s on PR #%s", thread_id, pr_number)
        return _recovery_error(exc, tool_name="reply_to_comment", pr_number=pr_number, repo=repo)


@mcp.tool(tags={"command"})
async def create_issue_from_comment(
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
        ctx = get_context()
        cwd = await _get_workspace_cwd(ctx)
        _check_auto_detect_prerequisites(cwd, has_pr=pr_number is not None, has_repo=repo is not None)
        pr_number = _resolve_pr_number(pr_number, cwd=cwd)
        return issues.create_issue_from_comment(
            pr_number,
            thread_id,
            title,
            labels=labels,
            repo=repo,
            cwd=cwd,
        )
    except Exception as exc:
        logger.exception("create_issue_from_comment failed for %s on PR #%s", thread_id, pr_number)
        return CreateIssueResult(
            issue_number=0,
            issue_url="",
            title=title,
            error=_recovery_error(exc, tool_name="create_issue_from_comment", pr_number=pr_number, repo=repo),
        )


@mcp.tool(tags={"query"})
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
        cwd = await _get_workspace_cwd(ctx)
        _check_auto_detect_prerequisites(cwd, has_pr=True, has_repo=repo is not None)
        return await descriptions.review_pr_descriptions(pr_numbers, repo=repo, cwd=cwd, ctx=ctx)
    except Exception as exc:
        logger.exception("review_pr_descriptions failed")
        return PRDescriptionReviewResult(error=_recovery_error(exc, tool_name="review_pr_descriptions", repo=repo))
    except asyncio.CancelledError:
        logger.warning("review_pr_descriptions cancelled")
        return PRDescriptionReviewResult(error="Cancelled")


@mcp.tool(tags={"query", "discovery"})
async def summarize_review_status(
    pr_numbers: list[int] | None = None,
    repo: str | None = None,
) -> StackReviewStatusResult:
    """Get a lightweight stack-wide review status overview with severity counts.

    Much fewer tokens than full thread data — use this to quickly scan which PRs
    need attention before diving into details with ``list_review_comments``.

    When ``pr_numbers`` is omitted, auto-discovers the stack from the current branch
    using the same branch-chain walking as ``list_review_comments``.

    Present the result as a concise table: one row per PR with unresolved count,
    severity breakdown (🔴 bugs, 🚩 flagged, 🟡 warnings, 📝 info), stale count,
    and reviewer status. Highlight PRs that need immediate attention.

    Args:
        pr_numbers: PR numbers to summarize. Auto-discovers stack if omitted.
        repo: Repository in "owner/repo" format. Auto-detected if not provided.

    Returns:
        Per-PR status with unresolved/resolved counts, severity breakdown
        (bugs, flagged, warnings, info), staleness, and reviewer progress.
    """
    try:
        ctx = get_context()
        cwd = await _get_workspace_cwd(ctx)
        _check_auto_detect_prerequisites(cwd, has_pr=pr_numbers is not None, has_repo=repo is not None)
        return await stack.summarize_review_status(pr_numbers=pr_numbers, repo=repo, cwd=cwd, ctx=ctx)
    except Exception as exc:
        logger.exception("summarize_review_status failed")
        return StackReviewStatusResult(error=_recovery_error(exc, tool_name="summarize_review_status", repo=repo))
    except asyncio.CancelledError:
        logger.warning("summarize_review_status cancelled")
        return StackReviewStatusResult(error="Cancelled")


@mcp.tool(tags={"query"})
async def list_recent_unresolved(
    repo: str | None = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
) -> StackReviewStatusResult:
    """Scan recently merged PRs for unresolved review threads.

    Reviewers like Greptile may post late comments on already-merged PRs.
    Use this alongside ``summarize_review_status`` to catch feedback the
    current stack view misses.

    Only returns PRs that have at least one unresolved thread.

    Args:
        repo: Repository in "owner/repo" format. Auto-detected if not provided.
        limit: How many recently merged PRs to scan (default 10, max 50).

    Returns:
        Per-PR status with severity counts — same format as ``summarize_review_status``.
    """
    try:
        ctx = get_context()
        cwd = await _get_workspace_cwd(ctx)
        _check_auto_detect_prerequisites(cwd, has_pr=True, has_repo=repo is not None)
        return await stack.list_recent_unresolved(repo=repo, limit=limit, cwd=cwd, ctx=ctx)
    except Exception as exc:
        logger.exception("list_recent_unresolved failed")
        return StackReviewStatusResult(error=_recovery_error(exc, tool_name="list_recent_unresolved", repo=repo))
    except asyncio.CancelledError:
        logger.warning("list_recent_unresolved cancelled")
        return StackReviewStatusResult(error="Cancelled")


@mcp.tool(tags={"query"})
async def stack_activity(
    pr_numbers: list[int] | None = None,
    repo: str | None = None,
) -> StackActivityResult:
    """Get a chronological activity feed across all PRs in a stack.

    Shows pushes, reviews, comments, labels, merges, and closes in timeline order.
    The ``settled`` flag is True when no activity for 10+ minutes after a
    push+review cycle — helps decide whether to wait or proceed.

    When ``pr_numbers`` is omitted, auto-discovers the stack from the current branch.

    Args:
        pr_numbers: PR numbers to include. Auto-discovers stack if omitted.
        repo: Repository in "owner/repo" format. Auto-detected if not provided.

    Returns:
        StackActivityResult with merged chronological events and settled flag.
    """
    try:
        ctx = get_context()
        cwd = await _get_workspace_cwd(ctx)
        _check_auto_detect_prerequisites(cwd, has_pr=pr_numbers is not None, has_repo=repo is not None)
        return await stack.stack_activity(pr_numbers=pr_numbers, repo=repo, cwd=cwd, ctx=ctx)
    except Exception as exc:
        logger.exception("stack_activity failed")
        return StackActivityResult(error=_recovery_error(exc, tool_name="stack_activity", repo=repo))
    except asyncio.CancelledError:
        logger.warning("stack_activity cancelled")
        return StackActivityResult(error="Cancelled")


@mcp.tool(tags={"query"})
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
        cwd = await _get_workspace_cwd(ctx)
        _check_auto_detect_prerequisites(cwd, has_pr=True, has_repo=repo is not None)
        return await comments.triage_review_comments(pr_numbers, repo=repo, owner_logins=owner_logins, cwd=cwd, ctx=ctx)
    except Exception as exc:
        logger.exception("triage_review_comments failed")
        return TriageResult(error=_recovery_error(exc, tool_name="triage_review_comments", repo=repo))
    except asyncio.CancelledError:
        logger.warning("triage_review_comments cancelled")
        return TriageResult(error="Cancelled")


@mcp.tool(tags={"query"})
async def diagnose_ci(
    pr_number: int | None = None,
    repo: str | None = None,
    run_id: int | None = None,
) -> CIDiagnosisResult:
    """Diagnose CI failures for a PR or workflow run in one call.

    Collapses the typical 3-5 sequential ``gh`` commands into a single tool call:
    finds the latest failed run, identifies failed jobs and steps, and extracts
    actionable error lines from the logs.

    Args:
        pr_number: PR number to check CI for. Auto-detected from current branch if omitted.
        repo: Repository in "owner/repo" format. Auto-detected if not provided.
        run_id: Specific workflow run ID to diagnose. If omitted, finds the latest failed run.

    Returns:
        Structured diagnosis with run info, failed jobs, failed steps, and extracted error lines.
    """
    try:
        ctx = get_context()
        cwd = await _get_workspace_cwd(ctx)
        if run_id is None:
            _check_auto_detect_prerequisites(cwd, has_pr=pr_number is not None, has_repo=repo is not None)
            if pr_number is None and cwd is not None:
                pr_number = _resolve_pr_number(None, cwd=cwd)
        else:
            _check_auto_detect_prerequisites(cwd, has_pr=True, has_repo=repo is not None)
        return await call_sync_fn_in_threadpool(ci.diagnose_ci, pr_number=pr_number, repo=repo, run_id=run_id, cwd=cwd)
    except Exception as exc:
        logger.exception("diagnose_ci failed")
        return CIDiagnosisResult(error=_recovery_error(exc, tool_name="diagnose_ci", pr_number=pr_number, repo=repo))


@mcp.tool(tags={"discovery"})
def show_config() -> ConfigInfo:
    """Show the active codereviewbuddy configuration.

    Returns the full loaded config including per-reviewer settings, resolve policies,
    self-improvement config, and diagnostics. Configuration is loaded from CRB_*
    environment variables at server startup.
    """
    config = get_config()

    # Build human-readable explanation
    parts: list[str] = []
    reviewer_summaries = []
    for name, rc in config.reviewers.items():
        status = "enabled" if rc.enabled else "disabled"
        levels = ", ".join(s.value for s in rc.resolve_levels)
        auto_stale = "auto-resolve stale" if rc.auto_resolve_stale else "skip stale"
        reviewer_summaries.append(f"{name}: {status}, resolve=[{levels}], {auto_stale}")
    parts.append(f"{len(config.reviewers)} reviewer(s) configured: {'; '.join(reviewer_summaries)}.")

    if config.self_improvement.enabled and config.self_improvement.repo:
        parts.append(f"Self-improvement: enabled → {config.self_improvement.repo}.")
    else:
        parts.append("Self-improvement: disabled.")

    if config.pr_descriptions.enabled:
        parts.append("PR description review: enabled.")
    else:
        parts.append("PR description review: disabled.")

    return ConfigInfo(
        config=config.model_dump(mode="json"),
        source="env",
        explanation=" ".join(parts),
    )


# ---------------------------------------------------------------------------
# Prompts — user-invoked workflows (#114)
# ---------------------------------------------------------------------------


@mcp.prompt
def review_stack() -> str:
    """Full review pass workflow for a PR stack.

    Returns a structured workflow the agent should follow to review
    all PRs in the current stack end-to-end.
    """
    return """\
You are doing a full review pass on the current PR stack. Follow these steps in order:

1. **Summarize status** — call `summarize_review_status()` (no args = auto-discover stack).
   Note which PRs have unresolved threads.

2. **Triage** — call `triage_review_comments(pr_numbers)` with the discovered PR numbers.
   This gives you only actionable threads, pre-classified by severity.

3. **Fix bugs first** — for each `action: "fix"` item (🔴 bug, 🚩 flagged):
   - Read the thread snippet and file/line.
   - Implement the fix.
   - Reply with `reply_to_comment` explaining what you fixed and the commit hash.

4. **Reply to warnings/info** — for `action: "reply"` items:
   - If you made changes based on the comment, reply explaining what changed.
   - If the comment is not actionable, reply explaining why (don't ignore it).

5. **Create issues for followups** — for `action: "create_issue"` items:
   - Call `create_issue_from_comment` with an appropriate title and labels.

6. **Resolve stale threads** — call `resolve_stale_comments` for each PR that had stale threads.

7. **Trigger re-reviews** — for manual-trigger reviewers, post a PR comment
   (see "Re-review triggers" table in instructions).

8. **Verify descriptions** — call `review_pr_descriptions(pr_numbers)` and fix any missing elements.

9. **Final check** — call `summarize_review_status()` again to confirm all bugs are addressed.
"""


@mcp.prompt
def pr_review_checklist() -> str:
    """Pre-merge checklist to verify PR quality before shipping.

    Use this after completing a review pass to make sure nothing was missed.
    """
    return """\
Run through this checklist before considering the stack ready to merge:

## Code quality
- [ ] All 🔴 bug and 🚩 flagged threads are resolved (fixed + replied)
- [ ] All 🟡 warning threads have been replied to (even if no code change)
- [ ] No `action: "create_issue"` items left without a GitHub issue filed

## PR hygiene
- [ ] Every PR body has `Fixes #N` or `Closes #N` linking the issue it solves
- [ ] PR descriptions are non-empty and not boilerplate (run `review_pr_descriptions`)
- [ ] Commit messages follow conventional commits format

## Review cycle
- [ ] `resolve_stale_comments` was called for PRs with stale threads
- [ ] Re-review triggered for manual-trigger reviewers (see instructions for trigger comments)

## Testing
- [ ] New/changed code has test coverage
- [ ] CI is green on all PRs in the stack

If any item fails, fix it before shipping. Use the tools to verify programmatically
where possible (e.g. `summarize_review_status` for review state, `review_pr_descriptions`
for PR bodies).
"""


@mcp.prompt
def ship_stack() -> str:
    """Pre-merge sanity check workflow before merging a PR stack.

    Runs through final verification to catch anything missed.
    """
    return """\
You are preparing to merge the current PR stack. Run these final checks:

1. **Review status** — call `summarize_review_status()`.
   - Any unresolved bugs (🔴) or flagged (🚩) threads? → STOP, fix them first.
   - Did you push fixes? → Trigger re-reviews for manual-trigger reviewers
     (see "Re-review triggers" table in instructions).

2. **Activity check** — call `stack_activity()`.
   - Is the stack `settled` (no activity for 10+ min after push+review)? Good.
   - If not settled, reviewers may still be working. Consider waiting.

3. **PR descriptions** — call `review_pr_descriptions(pr_numbers)`.
   - Every PR must have `Fixes #N` or `Closes #N` in the body.
   - No empty or boilerplate descriptions.

4. **Report** — summarize the stack state:
   - Total PRs, total unresolved threads, severity breakdown.
   - Whether it's safe to merge or what still needs attention.

If everything is green, tell the user the stack is ready to merge.
If anything blocks, list the specific items that need attention.
"""


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


def check_fastmcp_runtime() -> None:
    """Fail fast if runtime FastMCP is missing required task routing internals."""
    try:
        spec = importlib.util.find_spec(_FASTMCP_TASK_ROUTING_MODULE)
    except ModuleNotFoundError:
        spec = None

    if spec is None:
        msg = (
            "Incompatible FastMCP runtime: missing fastmcp.server.tasks.routing. "
            "Make sure codereviewbuddy is launched with its managed environment "
            "(for example: `uv run codereviewbuddy`)."
        )
        logger.error(msg)
        raise RuntimeError(msg)

    try:
        importlib.import_module(_FASTMCP_TASK_ROUTING_MODULE)
    except Exception as exc:
        msg = (
            "Incompatible FastMCP runtime: found fastmcp.server.tasks.routing "
            "but failed to import it. Make sure codereviewbuddy is launched "
            "with its managed environment (for example: `uv run codereviewbuddy`)."
        )
        logger.exception(msg)
        raise RuntimeError(msg) from exc

    logger.info("FastMCP runtime OK: %s", _FASTMCP_TASK_ROUTING_MODULE)
