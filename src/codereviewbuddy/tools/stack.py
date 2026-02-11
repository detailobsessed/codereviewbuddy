"""Stack discovery and lightweight review status summarization."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from fastmcp.utilities.async_utils import call_sync_fn_in_threadpool

from codereviewbuddy import gh
from codereviewbuddy.config import Severity, get_config
from codereviewbuddy.models import PRReviewStatusSummary, StackPR, StackReviewStatusResult
from codereviewbuddy.reviewers import get_reviewer, identify_reviewer

if TYPE_CHECKING:
    from typing import Any

    from fastmcp.server.context import Context

logger = logging.getLogger(__name__)


def _fetch_open_prs(repo: str | None = None, cwd: str | None = None) -> list[dict]:
    """Fetch all open PRs with branch info via gh CLI."""
    args = [
        "pr",
        "list",
        "--json",
        "number,title,headRefName,baseRefName,url",
        "--state",
        "open",
        "--limit",
        "100",
    ]
    if repo:
        args.extend(["--repo", repo])
    raw = gh.run_gh(*args, cwd=cwd)
    return json.loads(raw)


def _build_stack(current_pr_number: int, all_prs: list[dict]) -> list[StackPR]:
    """Walk the branch chain to find PRs in the same stack.

    Strategy:
    1. Build a map of baseRefName → PR and headRefName → PR
    2. Find the current PR
    3. Walk down (follow baseRefName chain) and up (follow headRefName chain)
    4. Return the stack ordered bottom-to-top
    """
    if not all_prs:
        return []

    # Index PRs by head branch (unique per PR)
    by_head: dict[str, dict] = {}
    # Index PRs by base branch (multiple PRs can target same base)
    by_base: dict[str, list[dict]] = {}
    by_number: dict[int, dict] = {}

    for pr in all_prs:
        head = pr.get("headRefName", "")
        base = pr.get("baseRefName", "")
        by_head[head] = pr
        by_base.setdefault(base, []).append(pr)
        by_number[pr["number"]] = pr

    current = by_number.get(current_pr_number)
    if current is None:
        return []

    # Collect stack members (use set to avoid duplicates)
    stack_numbers: set[int] = {current_pr_number}
    ordered: list[dict] = []

    # Walk DOWN: follow base branch chain (find PRs this one is stacked on)
    down: list[dict] = []
    pr = current
    while True:
        base_branch = pr.get("baseRefName", "")
        parent = by_head.get(base_branch)
        if parent is None or parent["number"] in stack_numbers:
            break
        stack_numbers.add(parent["number"])
        down.append(parent)
        pr = parent

    # Walk UP: find PRs stacked on top (PRs whose base is our head)
    up: list[dict] = []
    pr = current
    while True:
        head_branch = pr.get("headRefName", "")
        children = by_base.get(head_branch, [])
        # Pick the first child that isn't already in the stack
        child = next((c for c in children if c["number"] not in stack_numbers), None)
        if child is None:
            break
        stack_numbers.add(child["number"])
        up.append(child)
        pr = child

    # Order: bottom of stack first (furthest parent), then current, then children
    ordered = [*list(reversed(down)), current, *up]

    return [
        StackPR(
            pr_number=pr["number"],
            title=pr.get("title", ""),
            branch=pr.get("headRefName", ""),
            url=pr.get("url", ""),
        )
        for pr in ordered
    ]


async def discover_stack(
    pr_number: int,
    repo: str | None = None,
    cwd: str | None = None,
    ctx: Context | None = None,
) -> list[StackPR]:
    """Discover the full PR stack containing the given PR.

    Uses session state caching — the gh API call only happens once per session.

    Args:
        pr_number: A PR number in the stack.
        repo: Repository in "owner/repo" format.
        cwd: Working directory for git operations.
        ctx: FastMCP context for session caching and logging.

    Returns:
        List of StackPR ordered bottom-to-top.
    """
    # Try session cache first — validate the requested PR is in the cached stack
    if ctx:
        try:
            cached = await ctx.get_state("stack_prs")
            if cached is not None:
                stack_list = [StackPR(**pr) if isinstance(pr, dict) else pr for pr in cached]
                if any(p.pr_number == pr_number for p in stack_list):
                    logger.debug("Stack discovery cache hit")
                    return stack_list
                logger.debug("Cache miss — PR #%d not in cached stack", pr_number)
        except Exception:
            logger.debug("Session state not available, skipping cache", exc_info=True)

    all_prs = await call_sync_fn_in_threadpool(_fetch_open_prs, repo=repo, cwd=cwd)
    stack = _build_stack(pr_number, all_prs)

    # Cache in session state
    if ctx and stack:
        try:
            await ctx.set_state("stack_prs", [pr.model_dump() for pr in stack])
        except Exception:
            logger.debug("Failed to cache stack in session state", exc_info=True)

    if ctx and len(stack) > 1:
        pr_nums = [p.pr_number for p in stack]
        await ctx.info(f"Discovered stack of {len(stack)} PRs: {pr_nums}")

    return stack


# ---------------------------------------------------------------------------
# Lightweight review status summarization
# ---------------------------------------------------------------------------

# Lightweight query: only first comment per thread (for severity), no full history
_SUMMARY_QUERY = """
query($owner: String!, $repo: String!, $pr: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      title
      url
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          isResolved
          comments(first: 1) {
            nodes {
              author { login }
              body
              path
              createdAt
            }
          }
        }
      }
    }
  }
}
"""


def _classify_severity(reviewer_name: str, body: str) -> Severity:
    """Classify a comment's severity using the reviewer adapter."""
    adapter = get_reviewer(reviewer_name)
    if adapter is None:
        return Severity.INFO
    return adapter.classify_severity(body)


def _fetch_pr_summary(
    owner: str,
    repo: str,
    pr_number: int,
    commits: list[dict[str, Any]] | None = None,
    cwd: str | None = None,
) -> PRReviewStatusSummary:
    """Fetch lightweight review status for a single PR."""
    raw_threads: list[dict[str, Any]] = []
    cursor = None

    while True:
        variables: dict[str, Any] = {"owner": owner, "repo": repo, "pr": pr_number}
        if cursor:
            variables["cursor"] = cursor
        result = gh.graphql(_SUMMARY_QUERY, variables=variables, cwd=cwd)
        pr_data = result.get("data", {}).get("repository", {}).get("pullRequest") or {}
        threads_data = pr_data.get("reviewThreads", {})
        raw_threads.extend(threads_data.get("nodes", []))

        page_info = threads_data.get("pageInfo", {})
        if page_info.get("hasNextPage") and page_info.get("endCursor"):
            cursor = page_info["endCursor"]
        else:
            break

    title = pr_data.get("title", "")
    url = pr_data.get("url", "")

    config = get_config()
    unresolved = 0
    resolved = 0
    bugs = 0
    flagged = 0
    warnings = 0
    info_count = 0

    for node in raw_threads:
        comments = node.get("comments", {}).get("nodes", [])
        if not comments:
            continue

        first = comments[0]
        author = (first.get("author") or {}).get("login", "unknown")
        reviewer = identify_reviewer(author)

        # Skip disabled reviewers
        if not config.get_reviewer(reviewer).enabled:
            continue

        is_resolved = node.get("isResolved", False)
        if is_resolved:
            resolved += 1
        else:
            unresolved += 1

        # Severity classification (only for unresolved)
        if not is_resolved:
            body = first.get("body", "")
            severity = _classify_severity(reviewer, body)
            if severity == Severity.BUG:
                bugs += 1
            elif severity == Severity.FLAGGED:
                flagged += 1
            elif severity == Severity.WARNING:
                warnings += 1
            else:
                info_count += 1

    # Reviewer status detection
    from codereviewbuddy.tools.comments import _build_reviewer_statuses, _latest_push_time_from_commits

    last_push = _latest_push_time_from_commits(commits or [])

    # Build minimal reviewer threads for status detection
    from codereviewbuddy.models import CommentStatus as CS
    from codereviewbuddy.models import ReviewComment, ReviewThread

    mini_threads: list[ReviewThread] = []
    for node in raw_threads:
        comments = node.get("comments", {}).get("nodes", [])
        if not comments:
            continue
        first = comments[0]
        author = (first.get("author") or {}).get("login", "unknown")
        reviewer = identify_reviewer(author)
        if not config.get_reviewer(reviewer).enabled:
            continue
        mini_threads.append(
            ReviewThread(
                thread_id="",
                pr_number=pr_number,
                status=CS.RESOLVED if node.get("isResolved") else CS.UNRESOLVED,
                file=first.get("path"),
                reviewer=reviewer,
                comments=[
                    ReviewComment(
                        author=author,
                        body="",
                        created_at=first.get("createdAt"),
                    )
                ],
            )
        )

    # Compute staleness on the mini threads
    from codereviewbuddy.tools.comments import _compute_staleness

    _compute_staleness(mini_threads, commits or [], owner, repo, cwd=cwd)
    stale = sum(1 for t in mini_threads if t.is_stale and t.status == CS.UNRESOLVED)

    reviewer_statuses = _build_reviewer_statuses(mini_threads, last_push)
    reviews_in_progress = any(s.status == "pending" for s in reviewer_statuses)

    return PRReviewStatusSummary(
        pr_number=pr_number,
        title=title,
        url=url,
        unresolved=unresolved,
        resolved=resolved,
        bugs=bugs,
        flagged=flagged,
        warnings=warnings,
        info_count=info_count,
        stale=stale,
        reviews_in_progress=reviews_in_progress,
    )


async def summarize_review_status(
    pr_numbers: list[int] | None = None,
    repo: str | None = None,
    cwd: str | None = None,
    ctx: Context | None = None,
) -> StackReviewStatusResult:
    """Lightweight stack-wide review status overview.

    When ``pr_numbers`` is omitted, auto-discovers the stack from the current branch.

    Args:
        pr_numbers: PR numbers to summarize. Auto-discovers stack if omitted.
        repo: Repository in "owner/repo" format. Auto-detected if not provided.
        cwd: Working directory for git operations.
        ctx: FastMCP context for session caching and progress.

    Returns:
        Compact per-PR status with severity counts.
    """
    if repo:
        owner, repo_name = repo.split("/", 1)
    else:
        owner, repo_name = await call_sync_fn_in_threadpool(gh.get_repo_info, cwd=cwd)
    full_repo = f"{owner}/{repo_name}"

    # Auto-discover stack if no PR numbers provided
    if pr_numbers is None:
        current_pr = await call_sync_fn_in_threadpool(gh.get_current_pr_number, cwd=cwd)
        stack_prs = await discover_stack(current_pr, repo=full_repo, cwd=cwd, ctx=ctx)
        pr_numbers = [p.pr_number for p in stack_prs]

    if not pr_numbers:
        return StackReviewStatusResult(error="No PRs to summarize")

    summaries: list[PRReviewStatusSummary] = []
    total = len(pr_numbers)

    from codereviewbuddy.tools.comments import _get_pr_commits

    for i, pr_num in enumerate(pr_numbers):
        if ctx:
            await ctx.report_progress(i, total)
        commits = await call_sync_fn_in_threadpool(
            _get_pr_commits,
            owner,
            repo_name,
            pr_num,
            cwd=cwd,
        )
        summary = await call_sync_fn_in_threadpool(
            _fetch_pr_summary,
            owner,
            repo_name,
            pr_num,
            commits=commits,
            cwd=cwd,
        )
        summaries.append(summary)

    if ctx:
        await ctx.report_progress(total, total)

    total_unresolved = sum(s.unresolved for s in summaries)
    any_in_progress = any(s.reviews_in_progress for s in summaries)

    return StackReviewStatusResult(
        prs=summaries,
        total_unresolved=total_unresolved,
        any_reviews_in_progress=any_in_progress,
    )
