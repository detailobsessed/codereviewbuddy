"""FastMCP server for codereviewbuddy.

Exposes tools for managing AI code review comments on GitHub PRs.
Authentication is handled by the `gh` CLI — no tokens needed.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import sys
from typing import TYPE_CHECKING

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context
from fastmcp.server.lifespan import lifespan
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware
from fastmcp.server.middleware.logging import LoggingMiddleware
from fastmcp.server.middleware.ping import PingMiddleware
from fastmcp.server.middleware.timing import TimingMiddleware

from codereviewbuddy import gh
from codereviewbuddy.config import Config, get_config, get_config_path, load_config, register_reload_callback, set_config
from codereviewbuddy.middleware import WriteOperationMiddleware
from codereviewbuddy.models import (
    ConfigInfo,
    CreateIssueResult,
    PRDescriptionReviewResult,
    RereviewResult,
    ResolveStaleResult,
    ReviewSummary,
    StackActivityResult,
    StackReviewStatusResult,
    TriageResult,
)
from codereviewbuddy.reviewers import apply_config
from codereviewbuddy.tools import comments, descriptions, issues, rereview, stack

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)
_FASTMCP_TASK_ROUTING_MODULE = "fastmcp.server.tasks.routing"
write_operation_middleware = WriteOperationMiddleware()


def _resolve_pr_number(pr_number: int | None) -> int:
    """Resolve pr_number, auto-detecting from the current branch if not provided."""
    if pr_number is not None:
        return pr_number
    return gh.get_current_pr_number()


def _on_config_reload(config: Config) -> None:
    """Re-apply adapter config and middleware diagnostics after hot-reload."""
    apply_config(config)
    write_operation_middleware.configure_diagnostics(
        heartbeat_enabled=config.diagnostics.tool_call_heartbeat,
        heartbeat_interval_ms=config.diagnostics.heartbeat_interval_ms,
        include_args_fingerprint=config.diagnostics.include_args_fingerprint,
    )


@lifespan
async def check_gh_cli(server: FastMCP) -> AsyncIterator[dict[str, object] | None]:  # noqa: ARG001, RUF029
    """Verify gh CLI is installed and authenticated on server startup."""
    check_fastmcp_runtime()
    check_prerequisites()
    config, config_path = load_config()
    set_config(config, config_path=config_path)
    apply_config(config)
    write_operation_middleware.configure_diagnostics(
        heartbeat_enabled=config.diagnostics.tool_call_heartbeat,
        heartbeat_interval_ms=config.diagnostics.heartbeat_interval_ms,
        include_args_fingerprint=config.diagnostics.include_args_fingerprint,
    )
    register_reload_callback(_on_config_reload)
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
8. **Re-review** — `request_rereview` for manual-trigger reviewers (e.g. Unblocked).
9. **Verify** — `summarize_review_status()` again to confirm all bugs are addressed.

For full thread details (all comments, reviewer statuses), fall back to
`list_review_comments` for a specific PR — but prefer the triage workflow above.

## Responding to review comments

Always reply to bug (🔴) and flagged (🚩) level comments with `reply_to_comment`
explaining what you fixed, the commit hash, and any regression test added. Do not
silently push — reviewers need to see that their finding was acknowledged. For info
(📝) and warning (🟡) comments, a reply is optional but appreciated when you made
changes based on them.

## Reviewer behavior differences

Some reviewers auto-trigger a new review on every push (e.g. CodeRabbit) while others
require a manual trigger via `request_rereview` (e.g. Unblocked). The trigger message
is configurable per-reviewer via `rereview_message` in `.codereviewbuddy.toml`.

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

The server loads `.codereviewbuddy.toml` from the project root at startup and
hot-reloads on file changes. Call `show_config` to inspect the active configuration.
The config controls per-reviewer resolve policy:

- **`resolve_levels`**: Which severity levels you're allowed to resolve. If you try to
  resolve a thread whose severity (🔴 bug, 🚩 flagged, 🟡 warning, 📝 info) exceeds
  the allowed levels, the server will **block** the resolve and return an error.
- **`auto_resolve_stale`**: Whether `resolve_stale_comments` touches this reviewer's
  threads at all.
- **`enabled`**: Whether this reviewer's threads appear in results.

If `resolve_comment` or `resolve_stale_comments` returns a "blocked by config" error,
do NOT retry — the config is intentional. Inform the user about the blocked thread
and its severity level instead.

## Tracking useful suggestions

When review comments contain genuinely useful improvement suggestions (not bugs being
fixed in the PR), use `create_issue_from_comment` to create a GitHub issue. Use labels
to classify: type labels (bug, enhancement, documentation) and priority labels (P0-P3).
Don't file issues for nitpicks or things already being addressed.

## Self-improvement

If you encounter errors, missing capabilities, or find yourself repeatedly working
around a limitation of this MCP server, create a GitHub issue describing:
- What you were trying to do
- What went wrong or what's missing
- A proposed solution if you have one

Use `gh issue create` to file the issue against the repo specified in the
`[self_improvement]` config section. Label it `agent-reported` plus any relevant
type/priority labels.

This only applies when `[self_improvement]` is enabled in `.codereviewbuddy.toml`
and a target `repo` is configured. Call `show_config` to check settings before filing.

""",
)

mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=True, transform_errors=True))
mcp.add_middleware(TimingMiddleware())
mcp.add_middleware(LoggingMiddleware(include_payloads=True, max_payload_length=500))
mcp.add_middleware(PingMiddleware(interval_ms=30_000))
mcp.add_middleware(write_operation_middleware)


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
        return await stack.stack_activity(pr_numbers=pr_numbers, repo=repo, ctx=ctx)
    except Exception as exc:
        logger.exception("stack_activity failed")
        return StackActivityResult(error=f"Error: {exc}")
    except asyncio.CancelledError:
        logger.warning("stack_activity cancelled")
        return StackActivityResult(error="Cancelled")


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


@mcp.tool
def show_config() -> ConfigInfo:
    """Show the active codereviewbuddy configuration.

    Returns the full loaded config including per-reviewer settings, resolve policies,
    self-improvement config, and diagnostics. Also reports the config file path and
    whether hot-reload is active (changes to the file are picked up automatically).
    """
    config = get_config()
    config_path = get_config_path()
    return ConfigInfo(
        config=config.model_dump(mode="json"),
        config_path=str(config_path) if config_path else None,
        hot_reload=config_path is not None,
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

7. **Request re-review** — call `request_rereview` for each PR you pushed fixes to.

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
- [ ] Re-review requested after pushing fixes (`request_rereview`)

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
   - Did you push fixes? → Call `request_rereview` to trigger fresh reviews.

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


def main() -> None:
    """Run the codereviewbuddy MCP server, or handle CLI subcommands."""
    if len(sys.argv) > 1 and sys.argv[1] == "init":
        print("'codereviewbuddy init' has been renamed to 'codereviewbuddy config --init'")  # noqa: T201
        _config_cmd(["--init"])
        return

    if len(sys.argv) > 1 and sys.argv[1] == "config":
        _config_cmd(sys.argv[2:])
        return

    from codereviewbuddy.io_tap import install_io_tap  # noqa: PLC0415

    install_io_tap()
    mcp.run()


def _config_cmd(args: list[str]) -> None:
    """Handle ``codereviewbuddy config [--init | --update | --clean]``."""
    from codereviewbuddy.config import clean_config, init_config, update_config  # noqa: PLC0415

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
