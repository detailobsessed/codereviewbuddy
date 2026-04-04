"""Stack discovery and lightweight review status summarization."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastmcp.utilities.async_utils import call_sync_fn_in_threadpool

from codereviewbuddy import gh, github_api
from codereviewbuddy.models import (
    ActivityEvent,
    PRReviewStatusSummary,
    ReviewerState,
    StackActivityResult,
    StackPR,
    StackReviewStatusResult,
)

if TYPE_CHECKING:
    from typing import Any

    from fastmcp.server.context import Context


logger = logging.getLogger(__name__)


async def _fetch_open_prs(repo: str | None = None, cwd: str | None = None) -> list[dict]:
    """Fetch all open PRs with branch info via REST API."""
    if not repo:
        owner, repo_name = await call_sync_fn_in_threadpool(gh.get_repo_info, cwd=cwd)
    else:
        owner, repo_name = github_api.parse_repo(repo)
    prs = await github_api.rest(
        f"/repos/{owner}/{repo_name}/pulls?state=open&per_page=100",
        paginate=True,
    )
    return [
        {
            "number": pr["number"],
            "title": pr["title"],
            "headRefName": pr["head"]["ref"],
            "baseRefName": pr["base"]["ref"],
            "url": pr["html_url"],
        }
        for pr in (prs or [])
    ]


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

    all_prs = await _fetch_open_prs(repo=repo, cwd=cwd)
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

# Lightweight query: thread counts + reviewer state, no full comment history
_SUMMARY_QUERY = """
query($owner: String!, $repo: String!, $pr: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      title
      url
      latestReviews(first: 20) {
        nodes {
          author { login }
          state
        }
      }
      reviewRequests(first: 20) {
        nodes {
          requestedReviewer {
            ... on User { login }
            ... on Team { name }
            ... on Mannequin { login }
            ... on Bot { login }
          }
        }
      }
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          isResolved
          comments(first: 1) {
            nodes {
              __typename
            }
          }
        }
      }
    }
  }
}
"""


async def _paginate_summary_threads(
    owner: str,
    repo: str,
    pr_number: int,
    cwd: str | None = None,  # noqa: ARG001
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Paginate through review threads via the lightweight summary query.

    Returns:
        (raw_thread_nodes, pr_data) — pr_data from the last page (contains title, url, reviewer info).
    """
    raw_threads: list[dict[str, Any]] = []
    cursor = None
    pr_data: dict[str, Any] = {}

    while True:
        variables: dict[str, Any] = {"owner": owner, "repo": repo, "pr": pr_number}
        if cursor:
            variables["cursor"] = cursor
        result = await github_api.graphql(_SUMMARY_QUERY, variables=variables)
        pr_data = result.get("data", {}).get("repository", {}).get("pullRequest") or {}
        threads_data = pr_data.get("reviewThreads", {})
        raw_threads.extend(threads_data.get("nodes", []))

        page_info = threads_data.get("pageInfo", {})
        if page_info.get("hasNextPage") and page_info.get("endCursor"):
            cursor = page_info["endCursor"]
        else:
            break

    return raw_threads, pr_data


def _has_comments(node: dict[str, Any]) -> bool:
    """Check whether a thread node has at least one comment."""
    return bool(node.get("comments", {}).get("nodes"))


def _count_thread_statuses(
    raw_threads: list[dict[str, Any]],
) -> dict[str, int]:
    """Count resolved/unresolved threads."""
    counts: dict[str, int] = {"unresolved": 0, "resolved": 0}

    for node in raw_threads:
        if not _has_comments(node):
            continue
        if node.get("isResolved", False):
            counts["resolved"] += 1
        else:
            counts["unresolved"] += 1

    return counts


_REVIEW_STATE_MAP: dict[str, str] = {
    "APPROVED": "approved",
    "CHANGES_REQUESTED": "changes_requested",
    "COMMENTED": "commented",
    "DISMISSED": "dismissed",
}


def _extract_reviewer_states(pr_data: dict[str, Any]) -> list[ReviewerState]:
    """Extract per-reviewer states from latestReviews and reviewRequests."""
    reviewers: dict[str, str] = {}

    # Latest reviews — each entry is the most recent review from a reviewer
    for node in (pr_data.get("latestReviews") or {}).get("nodes") or []:
        login = (node.get("author") or {}).get("login", "")
        state = _REVIEW_STATE_MAP.get(node.get("state", ""), "commented")
        if login:
            reviewers[login] = state

    # Pending review requests override prior reviews (re-requested after review)
    for node in (pr_data.get("reviewRequests") or {}).get("nodes") or []:
        rr = node.get("requestedReviewer") or {}
        login = rr.get("login") or rr.get("name") or ""
        if login:
            reviewers[login] = "waiting"

    return [ReviewerState(reviewer=login, state=state) for login, state in sorted(reviewers.items())]


def _compute_review_state(reviewers: list[ReviewerState]) -> str:
    """Derive overall review state from per-reviewer states."""
    if not reviewers:
        return "none"

    states = {r.state for r in reviewers}

    if "changes_requested" in states:
        return "changes_requested"
    if "waiting" in states:
        return "waiting"
    if states == {"approved"}:
        return "approved"
    # Mix of commented/dismissed/approved but not all approved
    return "commented"


async def fetch_pr_summary(
    owner: str,
    repo: str,
    pr_number: int,
    cwd: str | None = None,
) -> PRReviewStatusSummary:
    """Fetch lightweight review status for a single PR.

    Args:
        owner: Repository owner (user or org).
        repo: Repository name (without owner prefix).
        pr_number: Pull request number.
        cwd: Working directory for git operations.

    Returns:
        Compact review status with thread counts, reviewer states, and overall review state.
    """
    raw_threads, pr_data = await _paginate_summary_threads(owner, repo, pr_number, cwd=cwd)
    if not pr_data:
        msg = f"PR #{pr_number} not found in {owner}/{repo}"
        raise ValueError(msg)
    counts = _count_thread_statuses(raw_threads)
    reviewers = _extract_reviewer_states(pr_data)

    return PRReviewStatusSummary(
        pr_number=pr_number,
        title=pr_data.get("title", ""),
        url=pr_data.get("url", ""),
        review_state=_compute_review_state(reviewers),
        reviewers=reviewers,
        **counts,
    )


def _build_status_hints(
    pr_numbers: list[int],
    total_unresolved: int,
) -> list[str]:
    """Build next_steps hints for a StackReviewStatusResult."""
    if total_unresolved == 0:
        return ["All threads resolved! Call review_pr_descriptions(pr_numbers) to check PR quality before merging."]
    return [f"Call triage_review_comments(pr_numbers={pr_numbers}) to see the {total_unresolved} unresolved thread(s)."]


async def _resolve_status_pr_numbers(
    full_repo: str,
    repo_explicit: bool,
    cwd: str | None,
    ctx: Context | None,
) -> list[int] | StackReviewStatusResult:
    """Auto-discover stack PR numbers for review status, returning error result on repo mismatch."""
    if repo_explicit:
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
    return [p.pr_number for p in stack_prs]


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
        Compact per-PR status with unresolved/resolved counts.
    """
    if repo:
        owner, repo_name = github_api.parse_repo(repo)
    else:
        owner, repo_name = await call_sync_fn_in_threadpool(gh.get_repo_info, cwd=cwd)
    full_repo = f"{owner}/{repo_name}"

    # Auto-discover stack if no PR numbers provided
    if pr_numbers is None:
        resolved = await _resolve_status_pr_numbers(full_repo, repo_explicit=bool(repo), cwd=cwd, ctx=ctx)
        if isinstance(resolved, StackReviewStatusResult):
            return resolved
        pr_numbers = resolved

    if not pr_numbers:
        return StackReviewStatusResult(error="No PRs to summarize")

    summaries: list[PRReviewStatusSummary] = []
    total = len(pr_numbers)

    for i, pr_num in enumerate(pr_numbers):
        if ctx and total:
            await ctx.report_progress(i, total)
        try:
            summary = await fetch_pr_summary(owner, repo_name, pr_num, cwd=cwd)
        except ValueError:
            logger.warning("PR #%d not found in %s/%s, skipping", pr_num, owner, repo_name)
            continue
        summaries.append(summary)

    if ctx and total:
        await ctx.report_progress(total, total)

    total_unresolved = sum(s.unresolved for s in summaries)
    next_steps = _build_status_hints(pr_numbers, total_unresolved)

    return StackReviewStatusResult(
        prs=summaries,
        total_unresolved=total_unresolved,
        next_steps=next_steps,
    )


# ---------------------------------------------------------------------------
# Recently merged PRs with unresolved comments (#182)
# ---------------------------------------------------------------------------

_MAX_MERGED_SCAN = 50


async def _fetch_merged_prs(
    repo: str | None = None,
    limit: int = 10,
    cwd: str | None = None,
) -> list[dict]:
    """Fetch recently merged PRs via REST API."""
    if not repo:
        owner, repo_name = await call_sync_fn_in_threadpool(gh.get_repo_info, cwd=cwd)
    else:
        owner, repo_name = github_api.parse_repo(repo)
    per_page = max(1, min(limit, _MAX_MERGED_SCAN))
    prs = await github_api.rest(
        f"/repos/{owner}/{repo_name}/pulls?state=closed&per_page={per_page}",
    )
    return [
        {
            "number": pr["number"],
            "title": pr["title"],
            "url": pr["html_url"],
            "mergedAt": pr["merged_at"],
        }
        for pr in (prs or [])
        if pr.get("merged_at")
    ]


async def list_recent_unresolved(
    repo: str | None = None,
    limit: int = 10,
    cwd: str | None = None,
    ctx: Context | None = None,
) -> StackReviewStatusResult:
    """Scan recently merged PRs for unresolved review threads.

    Some bots post comments on already-merged PRs.
    This tool surfaces those so agents don't miss late-arriving feedback.

    Args:
        repo: Repository in "owner/repo" format. Auto-detected if not provided.
        limit: How many recently merged PRs to scan (default 10, max 50).
        cwd: Working directory for git operations.
        ctx: FastMCP context for progress reporting.

    Returns:
        StackReviewStatusResult containing only PRs that have unresolved threads.
    """
    if repo:
        owner, repo_name = github_api.parse_repo(repo)
    else:
        owner, repo_name = await call_sync_fn_in_threadpool(gh.get_repo_info, cwd=cwd)
    full_repo = f"{owner}/{repo_name}"

    merged_prs = await _fetch_merged_prs(repo=full_repo, limit=limit, cwd=cwd)
    if not merged_prs:
        return StackReviewStatusResult(prs=[], total_unresolved=0)

    summaries: list[PRReviewStatusSummary] = []
    total = len(merged_prs)

    for i, pr in enumerate(merged_prs):
        if ctx and total:
            await ctx.report_progress(i, total)
        try:
            summary = await fetch_pr_summary(owner, repo_name, pr["number"], cwd=cwd)
        except ValueError:
            logger.warning("PR #%d not found in %s/%s, skipping", pr["number"], owner, repo_name)
            continue
        if summary.unresolved > 0:
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


async def _fetch_timeline(
    owner: str,
    repo: str,
    pr_number: int,
    cwd: str | None = None,  # noqa: ARG001
) -> list[dict[str, Any]]:
    """Fetch timeline events for a single PR via the GitHub REST API."""
    endpoint = f"/repos/{owner}/{repo}/issues/{pr_number}/timeline"
    result = await github_api.rest(endpoint, paginate=True)
    return result if isinstance(result, list) else []


def _extract_actor(raw: dict[str, Any]) -> str:
    """Extract the actor login from a timeline event."""
    for key in ("user", "actor", "author"):
        if raw.get(key):
            return raw[key].get("login", "")
    committer = raw.get("committer")
    if committer:
        return committer.get("login", committer.get("name", ""))
    return ""


def _extract_detail(mapped: str, raw: dict[str, Any]) -> str:
    """Extract event-specific detail from a timeline event."""
    if mapped == "review":
        return raw.get("state", "").lower()
    if mapped in {"labeled", "unlabeled"}:
        return raw.get("label", {}).get("name", "")
    if mapped == "commit":
        return (raw.get("message") or "")[:80]
    return ""


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
        except (ValueError, AttributeError):  # fmt: skip
            continue

        events.append(
            ActivityEvent(
                time=ts,
                pr_number=pr_number,
                event_type=mapped,
                actor=_extract_actor(raw),
                detail=_extract_detail(mapped, raw),
            )
        )

    return events


async def _resolve_pr_numbers(
    full_repo: str,
    repo_explicit: bool,
    cwd: str | None,
    ctx: Context | None,
) -> list[int] | StackActivityResult:
    """Auto-discover stack PR numbers, returning error result on repo mismatch."""
    if repo_explicit:
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
    return [p.pr_number for p in stack_prs]


async def stack_activity(
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
        owner, repo_name = github_api.parse_repo(repo)
    else:
        owner, repo_name = await call_sync_fn_in_threadpool(gh.get_repo_info, cwd=cwd)
    full_repo = f"{owner}/{repo_name}"

    # Auto-discover stack if needed
    if pr_numbers is None:
        resolved = await _resolve_pr_numbers(full_repo, repo_explicit=bool(repo), cwd=cwd, ctx=ctx)
        if isinstance(resolved, StackActivityResult):
            return resolved
        pr_numbers = resolved

    if not pr_numbers:
        return StackActivityResult(error="No PRs to fetch activity for")

    all_events: list[ActivityEvent] = []
    total = len(pr_numbers)

    for i, pr_num in enumerate(pr_numbers):
        if ctx and total:
            await ctx.report_progress(i, total)
        raw = await _fetch_timeline(owner, repo_name, pr_num, cwd=cwd)
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
