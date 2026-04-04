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
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

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
from codereviewbuddy.models import (
    CIDiagnosisResult,
    CIStatusResult,
    ConfigInfo,
    PRDescriptionReviewResult,
    ReviewThread,
    StackActivityResult,
    StackReviewStatusResult,
    TriageResult,
)
from codereviewbuddy.tools import ci, comments, descriptions, stack

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastmcp.server.context import Context

logger = logging.getLogger(__name__)
_FASTMCP_TASK_ROUTING_MODULE = "fastmcp.server.tasks.routing"


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
    3. Process cwd — only if it resolves to a git repository root.

    See #142, #174.
    """
    # 1. MCP roots (per-window — correct for multi-window setups)
    if ctx is not None:
        try:
            roots = await asyncio.wait_for(ctx.list_roots(), timeout=5.0)
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

    # 3. Process cwd — only if it is inside a git repository.
    #    Windsurf sets process cwd to "/" for MCP servers, so we guard against
    #    non-repo directories to avoid confusing gh errors downstream.
    git_root = await asyncio.to_thread(gh._git_root_for_cwd, str(Path.cwd()))
    if git_root:
        logger.warning(
            "Workspace not detected (MCP roots unavailable, CRB_WORKSPACE not set). Using git root from process cwd: %s",
            git_root,
        )
        return git_root

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


def _resolve_thread_pr_number(
    thread_id: str,
    pr_number: int | None,
    cwd: str | None,
    *,
    has_repo: bool,
) -> int | None:
    """Resolve pr_number for thread-based tool operations.

    PRRT_ threads use GraphQL with only thread_id — workspace/pr_number not needed.
    All other thread types require workspace context and pr_number resolution.
    """
    if thread_id.startswith("PRRT_"):
        return pr_number
    _check_auto_detect_prerequisites(cwd, has_pr=pr_number is not None, has_repo=has_repo)
    return _resolve_pr_number(pr_number, cwd=cwd)


@lifespan
async def check_gh_cli(server: FastMCP) -> AsyncIterator[dict[str, object] | None]:  # noqa: RUF029
    """Verify gh CLI is installed and authenticated on server startup."""
    from codereviewbuddy._instance import _remove_pid_file, enforce_single_instance  # noqa: PLC0415

    pid_file = enforce_single_instance()
    check_fastmcp_runtime()
    check_prerequisites()
    config = load_config()
    set_config(config)
    if config.self_improvement.enabled:
        server.instructions = _BASE_INSTRUCTIONS + _SELF_IMPROVEMENT_INSTRUCTIONS
    else:
        server.instructions = _BASE_INSTRUCTIONS
    try:
        yield {}
    finally:
        _remove_pid_file(pid_file)


_BASE_INSTRUCTIONS = """\
AI code review buddy — fetch, triage, and manage PR review comments
from any AI reviewer that uses GitHub's PR review infrastructure.

## Stack discovery

`summarize_review_status` and `triage_review_comments` automatically discover the full
PR stack by walking the branch chain (works with Graphite, git-spice, or manual stacking).
Omit `pr_numbers` to auto-discover. Use `get_thread(thread_id)` to fetch full details
for a specific thread.

## Review workflow — step by step

Follow this sequence when reviewing or responding to AI review comments:

1. **Summarize** — `summarize_review_status()` (omit `pr_numbers` → auto-discover stack).
   Check which PRs have unresolved threads.
2. **Triage** — `triage_review_comments(pr_numbers)` with the discovered PR numbers.
   Returns only unresolved threads that still need attention (titles + metadata).
3. **Read** — `get_thread(thread_id)` for each thread that needs action.
   Returns full comment body and conversation history.
4. **Fix and reply** — implement fixes, then `reply_to_comment` explaining the change
   and commit hash.
5. **Verify** — `summarize_review_status()` again to confirm all threads are addressed.

## Responding to review comments

Always reply to review comments with `reply_to_comment` explaining what you fixed
and the commit hash. Do not silently push — always acknowledge findings.

## Important: repo parameter

All review tools auto-detect the repository from the current workspace (via
MCP roots sent by your client, or `CRB_WORKSPACE` env var as fallback).
The `repo` parameter on each tool is only needed when auto-detection fails.

## Tracking useful suggestions

When review comments contain genuinely useful improvement suggestions that aren't
being addressed in the current PR, ask the user if they'd like to file an issue
to track the suggestion.
"""

_SELF_IMPROVEMENT_INSTRUCTIONS = """
## Self-improvement

As you use this MCP server, keep track of any errors, missing capabilities,
or limitations you work around. When you have something worth reporting,
ask the user if you should file a Linear issue to track the problem or
improvement idea.
"""

mcp = FastMCP(
    "codereviewbuddy",
    lifespan=check_gh_cli,
    instructions=_BASE_INSTRUCTIONS,
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


# ---------------------------------------------------------------------------
# Resources — read-only views of PR data
# ---------------------------------------------------------------------------


@mcp.resource("pr://{owner}/{repo}/{pr_number}/reviews")
async def pr_reviews(owner: str, repo: str, pr_number: int) -> str:
    """Read-only review summary for a single PR.

    Returns lightweight review status: thread counts, reviewer states,
    and overall review state. No full comment bodies.
    """
    summary = await stack._fetch_pr_summary(owner, repo, pr_number)
    return summary.model_dump_json()


@mcp.tool(tags={"query"})
async def get_thread(thread_id: str) -> ReviewThread | str:
    """Fetch full details for a single review thread by its node ID.

    Use this after ``triage_review_comments`` to read the full comment body
    and conversation history for threads that need attention.

    Supports all thread types: inline review threads (PRRT_), PR-level
    reviews (PRR_), and bot comments (IC_).

    Args:
        thread_id: The node ID (PRRT_..., PRR_..., or IC_...) to fetch.

    Returns:
        Full thread with all comments, status, file/line info.
    """
    try:
        return await comments.get_thread(thread_id)
    except Exception as exc:
        logger.exception("get_thread failed for %s", thread_id)
        return _recovery_error(exc, tool_name="get_thread")
    except asyncio.CancelledError:
        logger.warning("get_thread cancelled for %s", thread_id)
        return "Cancelled"


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
        pr_number: PR number. Not required for inline threads (PRRT_). Auto-detected
            from the current branch for PRR_ and IC_ threads if omitted.
        thread_id: The node ID (PRRT_..., PRR_..., or IC_...) to reply to.
        body: Reply text.
        repo: Repository in "owner/repo" format. Not required for PRRT_ threads.
            Auto-detected for PRR_ and IC_ threads if not provided.
    """
    try:
        ctx = get_context()
        cwd = await _get_workspace_cwd(ctx)
        pr_number = _resolve_thread_pr_number(thread_id, pr_number, cwd, has_repo=repo is not None)
        return await comments.reply_to_comment(pr_number, thread_id, body, repo=repo, cwd=cwd)
    except Exception as exc:
        logger.exception("reply_to_comment failed for %s on PR #%s", thread_id, pr_number)
        return _recovery_error(exc, tool_name="reply_to_comment", pr_number=pr_number, repo=repo)
    except asyncio.CancelledError:
        logger.warning("reply_to_comment cancelled for %s on PR #%s", thread_id, pr_number)
        return "Cancelled"


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
    """Get a lightweight stack-wide review status overview.

    Much fewer tokens than full thread data — use this to quickly scan which PRs
    need attention before diving into details with ``triage_review_comments``.

    When ``pr_numbers`` is omitted, auto-discovers the stack from the current branch.

    Args:
        pr_numbers: PR numbers to summarize. Auto-discovers stack if omitted.
        repo: Repository in "owner/repo" format. Auto-detected if not provided.

    Returns:
        Per-PR status with unresolved/resolved counts.
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

    Some bots may post late comments on already-merged PRs.
    Use this alongside ``summarize_review_status`` to catch feedback the
    current stack view misses.

    Only returns PRs that have at least one unresolved thread.

    Args:
        repo: Repository in "owner/repo" format. Auto-detected if not provided.
        limit: How many recently merged PRs to scan (default 10, max 50).

    Returns:
        Per-PR status with unresolved/resolved counts — same format as ``summarize_review_status``.
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
    """Show only unresolved inline review threads that need attention — titles and metadata only.

    Returns actionable inline threads (PRRT_) only. PR-level reviews and bot issue
    comments are excluded as non-actionable. Filters out already-replied and resolved
    threads. Use ``get_thread`` for full details.

    Args:
        pr_numbers: PR numbers to triage (use stack from ``summarize_review_status``).
        repo: Repository in "owner/repo" format. Auto-detected if not provided.
        owner_logins: GitHub usernames considered "ours" (agent + human).
            Defaults to CRB_OWNER_LOGINS env var. Pass explicitly to override.

    Returns:
        TriageResult with unresolved threads needing action.
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
    except asyncio.CancelledError:
        logger.warning("diagnose_ci cancelled")
        return CIDiagnosisResult(error="Cancelled")


@mcp.tool(tags={"query"})
async def check_ci_status(
    pr_number: int | None = None,
    repo: str | None = None,
) -> CIStatusResult:
    """Check whether CI is passing for a PR.

    Returns a simple pass/fail/pending verdict with per-check breakdown.
    Much lighter than ``diagnose_ci`` — use this to verify CI is green
    before merging, and fall back to ``diagnose_ci`` only when it fails.

    Args:
        pr_number: PR number to check. Auto-detected from current branch if omitted.
        repo: Repository in "owner/repo" format. Auto-detected if not provided.

    Returns:
        Overall CI status with counts and per-check breakdown.
    """
    try:
        ctx = get_context()
        cwd = await _get_workspace_cwd(ctx)
        _check_auto_detect_prerequisites(cwd, has_pr=pr_number is not None, has_repo=repo is not None)
        pr_number = _resolve_pr_number(pr_number, cwd=cwd)
        return await call_sync_fn_in_threadpool(ci.check_ci_status, pr_number=pr_number, repo=repo, cwd=cwd)
    except Exception as exc:
        logger.exception("check_ci_status failed")
        return CIStatusResult(
            overall="error",
            error=_recovery_error(exc, tool_name="check_ci_status", pr_number=pr_number, repo=repo),
        )
    except asyncio.CancelledError:
        logger.warning("check_ci_status cancelled")
        return CIStatusResult(overall="error", error="Cancelled")


@mcp.tool(tags={"discovery"})
def show_config() -> ConfigInfo:
    """Show the active codereviewbuddy configuration.

    Returns the full loaded config including PR description and self-improvement settings.
    Configuration is loaded from CRB_* environment variables at server startup.
    """
    config = get_config()

    # Build human-readable explanation
    parts: list[str] = []

    if config.self_improvement.enabled:
        parts.append("Self-improvement: enabled.")
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
   This gives you only unresolved threads that need attention.

3. **Read and fix** — for each thread:
   - Call `get_thread(thread_id)` to read the full comment and conversation.
   - If a code fix is needed, implement it.
   - Reply with `reply_to_comment` explaining what you did and the commit hash.

4. **Verify descriptions** — call `review_pr_descriptions(pr_numbers)` and fix any missing elements.

5. **Final check** — call `summarize_review_status()` again to confirm all threads are addressed.
"""


@mcp.prompt
def pr_review_checklist() -> str:
    """Pre-merge checklist to verify PR quality before shipping.

    Use this after completing a review pass to make sure nothing was missed.
    """
    return """\
Run through this checklist before considering the stack ready to merge:

## Code quality
- [ ] All unresolved review threads are addressed (fixed + replied)

## PR hygiene
- [ ] Every PR body has `Fixes #N` or `Closes #N` linking the issue it solves
- [ ] PR descriptions are non-empty and not boilerplate (run `review_pr_descriptions`)
- [ ] Commit messages follow conventional commits format

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
   - Any unresolved threads? → STOP, fix them first.
2. **Activity check** — call `stack_activity()`.
   - Is the stack `settled` (no activity for 10+ min after push+review)? Good.
   - If not settled, review bots may still be working. Consider waiting.

3. **PR descriptions** — call `review_pr_descriptions(pr_numbers)`.
   - Every PR must have `Fixes #N` or `Closes #N` in the body.
   - No empty or boilerplate descriptions.

4. **CI status** — call `check_ci_status()` for each PR in the stack.
   - All green? Good.
   - Pending? Wait for completion, then re-check.
   - Failed? Call `diagnose_ci()` and fix before merging.

5. **Report** — summarize the stack state:
   - Total PRs, total unresolved threads.
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
