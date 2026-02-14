"""Stack discovery and lightweight review status summarization."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from fastmcp.utilities.async_utils import call_sync_fn_in_threadpool

from codereviewbuddy import gh
from codereviewbuddy.config import Severity, get_config
from codereviewbuddy.models import ActivityEvent, PRReviewStatusSummary, StackActivityResult, StackPR, StackReviewStatusResult
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


def _index_prs(
    all_prs: list[dict],
) -> tuple[dict[str, dict], dict[str, list[dict]], dict[int, dict]]:
    """Build lookup indices for PRs by head branch, base branch, and number."""
    by_head: dict[str, dict] = {}
    by_base: dict[str, list[dict]] = {}
    by_number: dict[int, dict] = {}
    for pr in all_prs:
        by_head[pr.get("headRefName", "")] = pr
        by_base.setdefault(pr.get("baseRefName", ""), []).append(pr)
        by_number[pr["number"]] = pr
    return by_head, by_base, by_number


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

    by_head, by_base, by_number = _index_prs(all_prs)

    current = by_number.get(current_pr_number)
    if current is None:
        return []

    # Collect stack members (use set to avoid duplicates)
    stack_numbers: set[int] = {current_pr_number}

    # Walk DOWN: follow base branch chain (find PRs this one is stacked on)
    down: list[dict] = []
    pr = current
    while True:
        parent = by_head.get(pr.get("baseRefName", ""))
        if parent is None or parent["number"] in stack_numbers:
            break
        stack_numbers.add(parent["number"])
        down.append(parent)
        pr = parent

    # Walk UP: find PRs stacked on top (PRs whose base is our head)
    up: list[dict] = []
    pr = current
    while True:
        children = by_base.get(pr.get("headRefName", ""), [])
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
          isOutdated
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


def _paginate_summary_threads(
    owner: str,
    repo: str,
    pr_number: int,
    cwd: str | None = None,
) -> tuple[list[dict[str, Any]], str, str]:
    """Paginate through review threads via the lightweight summary query.

    Returns:
        (raw_thread_nodes, pr_title, pr_url)
    """
    raw_threads: list[dict[str, Any]] = []
    cursor = None
    pr_data: dict[str, Any] = {}

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

    return raw_threads, pr_data.get("title", ""), pr_data.get("url", "")


def _first_comment_reviewer(node: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    """Extract the first comment and reviewer name from a thread node.

    Returns None if the thread has no comments or the reviewer is disabled.
    """
    comments = node.get("comments", {}).get("nodes", [])
    if not comments:
        return None
    first = comments[0]
    author = (first.get("author") or {}).get("login", "unknown")
    reviewer = identify_reviewer(author)
    config = get_config()
    if not config.get_reviewer(reviewer).enabled:
        return None
    return first, reviewer


_SEVERITY_TO_FIELD: dict[Severity, str] = {
    Severity.BUG: "bugs",
    Severity.FLAGGED: "flagged",
    Severity.WARNING: "warnings",
    Severity.INFO: "info_count",
}


def _count_thread_statuses(
    raw_threads: list[dict[str, Any]],
) -> dict[str, int]:
    """Count resolved/unresolved threads and severity buckets for unresolved."""
    counts: dict[str, int] = {
        "unresolved": 0,
        "resolved": 0,
        "bugs": 0,
        "flagged": 0,
        "warnings": 0,
        "info_count": 0,
    }

    for node in raw_threads:
        parsed = _first_comment_reviewer(node)
        if parsed is None:
            continue
        first, reviewer = parsed

        if node.get("isResolved", False):
            counts["resolved"] += 1
        else:
            counts["unresolved"] += 1
            severity = _classify_severity(reviewer, first.get("body", ""))
            field = _SEVERITY_TO_FIELD.get(severity, "info_count")
            counts[field] += 1

    return counts


def _count_stale_threads(raw_threads: list[dict[str, Any]]) -> int:
    """Count unresolved + outdated threads from enabled reviewers."""
    stale = 0
    for node in raw_threads:
        if node.get("isResolved") or not node.get("isOutdated"):
            continue
        if _first_comment_reviewer(node) is not None:
            stale += 1
    return stale


def _fetch_pr_summary(
    owner: str,
    repo: str,
    pr_number: int,
    cwd: str | None = None,
) -> PRReviewStatusSummary:
    """Fetch lightweight review status for a single PR."""
    raw_threads, title, url = _paginate_summary_threads(owner, repo, pr_number, cwd=cwd)
    counts = _count_thread_statuses(raw_threads)
    stale = _count_stale_threads(raw_threads)

    return PRReviewStatusSummary(
        pr_number=pr_number,
        title=title,
        url=url,
        stale=stale,
        **counts,
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
        # Guard: auto-discovery uses `gh pr view` which reads the current branch.
        # If `repo` was explicitly provided, verify it matches the cwd repo —
        # otherwise we'd detect a PR from the wrong repo (#115).
        if repo:
            try:
                cwd_owner, cwd_repo = await call_sync_fn_in_threadpool(gh.get_repo_info, cwd=cwd)
                cwd_full = f"{cwd_owner}/{cwd_repo}"
            except gh.GhError:
                cwd_full = None
            if cwd_full is None or cwd_full.lower() != full_repo.lower():
                return StackReviewStatusResult(
                    error=f"Auto-discovery unavailable: working directory is {cwd_full or 'unknown'}, "
                    f"but target repo is {full_repo}. Pass pr_numbers explicitly.",
                )

        current_pr = await call_sync_fn_in_threadpool(gh.get_current_pr_number, cwd=cwd)
        stack_prs = await discover_stack(current_pr, repo=full_repo, cwd=cwd, ctx=ctx)
        pr_numbers = [p.pr_number for p in stack_prs]

    if not pr_numbers:
        return StackReviewStatusResult(error="No PRs to summarize")

    summaries: list[PRReviewStatusSummary] = []
    total = len(pr_numbers)

    for i, pr_num in enumerate(pr_numbers):
        if ctx and total:
            await ctx.report_progress(i, total)
        summary = await call_sync_fn_in_threadpool(
            _fetch_pr_summary,
            owner,
            repo_name,
            pr_num,
            cwd=cwd,
        )
        summaries.append(summary)

    if ctx and total:
        await ctx.report_progress(total, total)

    total_unresolved = sum(s.unresolved for s in summaries)

    return StackReviewStatusResult(
        prs=summaries,
        total_unresolved=total_unresolved,
    )


# ---------------------------------------------------------------------------
# Stack activity timeline (#98)
# ---------------------------------------------------------------------------

_SETTLED_MINUTES = 10

_TIMELINE_EVENT_MAP: dict[str, str] = {
    "reviewed": "review",
    "commented": "comment",
    "head_ref_force_pushed": "push",
    "committed": "commit",
    "labeled": "labeled",
    "unlabeled": "unlabeled",
    "merged": "merged",
    "closed": "closed",
    "reopened": "reopened",
}


def _fetch_timeline(
    owner: str,
    repo: str,
    pr_number: int,
    cwd: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch timeline events for a single PR via the GitHub REST API."""
    endpoint = f"/repos/{owner}/{repo}/issues/{pr_number}/timeline"
    result = gh.rest(endpoint, paginate=True, cwd=cwd)
    return result if isinstance(result, list) else []


def _parse_timeline_events(
    raw_events: list[dict[str, Any]],
    pr_number: int,
) -> list[ActivityEvent]:
    """Parse raw GitHub timeline events into ActivityEvent models."""
    from datetime import datetime  # noqa: PLC0415

    events: list[ActivityEvent] = []
    for raw in raw_events:
        event_name = raw.get("event", "")
        mapped = _TIMELINE_EVENT_MAP.get(event_name)
        if mapped is None:
            continue

        # Extract timestamp — different event types store it differently
        ts_str = raw.get("submitted_at") or raw.get("created_at") or raw.get("committer", {}).get("date")
        if not ts_str:
            continue

        try:
            ts = datetime.fromisoformat(ts_str)
        except ValueError, AttributeError:
            continue

        # Extract actor
        actor = ""
        if raw.get("user"):
            actor = raw["user"].get("login", "")
        elif raw.get("actor"):
            actor = raw["actor"].get("login", "")
        elif raw.get("author"):
            actor = raw["author"].get("login", "")
        elif raw.get("committer"):
            actor = raw["committer"].get("login", raw["committer"].get("name", ""))

        # Extract detail
        detail = ""
        if mapped == "review":
            detail = raw.get("state", "").lower()
        elif mapped in {"labeled", "unlabeled"}:
            detail = raw.get("label", {}).get("name", "")
        elif mapped == "commit":
            detail = (raw.get("message") or "")[:80]

        events.append(
            ActivityEvent(
                time=ts,
                pr_number=pr_number,
                event_type=mapped,
                actor=actor,
                detail=detail,
            )
        )

    return events


async def stack_activity(  # noqa: PLR0914
    pr_numbers: list[int] | None = None,
    repo: str | None = None,
    cwd: str | None = None,
    ctx: Context | None = None,
) -> StackActivityResult:
    """Chronological activity feed across all PRs in a stack.

    Merges timeline events from each PR, sorted by timestamp. The ``settled``
    flag is True when no activity has occurred for 10+ minutes after the last
    push+review cycle, helping agents decide whether to wait or proceed.

    Args:
        pr_numbers: PR numbers to include. Auto-discovers stack if omitted.
        repo: Repository in "owner/repo" format. Auto-detected if not provided.
        cwd: Working directory for git operations.
        ctx: FastMCP context for progress reporting and session caching.

    Returns:
        StackActivityResult with merged, chronologically ordered events.
    """
    from datetime import UTC, datetime  # noqa: PLC0415

    if repo:
        owner, repo_name = repo.split("/", 1)
    else:
        owner, repo_name = await call_sync_fn_in_threadpool(gh.get_repo_info, cwd=cwd)
    full_repo = f"{owner}/{repo_name}"

    # Auto-discover stack if needed
    if pr_numbers is None:
        # Guard: verify explicit repo matches cwd repo before auto-discovery (#115)
        if repo:
            try:
                cwd_owner, cwd_repo = await call_sync_fn_in_threadpool(gh.get_repo_info, cwd=cwd)
                cwd_full = f"{cwd_owner}/{cwd_repo}"
            except gh.GhError:
                cwd_full = None
            if cwd_full is None or cwd_full.lower() != full_repo.lower():
                return StackActivityResult(
                    error=f"Auto-discovery unavailable: working directory is {cwd_full or 'unknown'}, "
                    f"but target repo is {full_repo}. Pass pr_numbers explicitly.",
                )

        current_pr = await call_sync_fn_in_threadpool(gh.get_current_pr_number, cwd=cwd)
        stack_prs = await discover_stack(current_pr, repo=full_repo, cwd=cwd, ctx=ctx)
        pr_numbers = [p.pr_number for p in stack_prs]

    if not pr_numbers:
        return StackActivityResult(error="No PRs to fetch activity for")

    all_events: list[ActivityEvent] = []
    total = len(pr_numbers)

    for i, pr_num in enumerate(pr_numbers):
        if ctx and total:
            await ctx.report_progress(i, total)
        raw = await call_sync_fn_in_threadpool(_fetch_timeline, owner, repo_name, pr_num, cwd=cwd)
        all_events.extend(_parse_timeline_events(raw, pr_num))

    if ctx and total:
        await ctx.report_progress(total, total)

    # Sort chronologically
    all_events.sort(key=lambda e: e.time)

    # Compute settled flag
    last_activity = all_events[-1].time if all_events else None
    minutes_since = None
    settled = False

    if last_activity is not None:
        now = datetime.now(UTC)
        minutes_since = int((now - last_activity).total_seconds() / 60)
        # Settled = no activity for 10+ min AND at least one push and one review exist
        has_push = any(e.event_type == "push" for e in all_events)
        has_review = any(e.event_type == "review" for e in all_events)
        settled = minutes_since >= _SETTLED_MINUTES and has_push and has_review

    return StackActivityResult(
        events=all_events,
        last_activity=last_activity,
        minutes_since_last_activity=minutes_since,
        settled=settled,
    )
