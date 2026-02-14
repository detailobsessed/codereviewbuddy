"""MCP tools for managing PR review comments."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from fastmcp.utilities.async_utils import call_sync_fn_in_threadpool

from codereviewbuddy import gh
from codereviewbuddy.config import get_config
from codereviewbuddy.models import (
    CommentStatus,
    ResolveStaleResult,
    ReviewComment,
    ReviewerStatus,
    ReviewSummary,
    ReviewThread,
    TriageItem,
    TriageResult,
)
from codereviewbuddy.reviewers import get_reviewer, identify_reviewer
from codereviewbuddy.tools.stack import discover_stack

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from datetime import datetime
    from typing import Any

    from fastmcp.server.context import Context

# GraphQL query to fetch review threads for a PR (paginated)
_THREADS_QUERY = """
query($owner: String!, $repo: String!, $pr: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      title
      url
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          isOutdated
          comments(first: 10) {
            nodes {
              author { login }
              body
              createdAt
              path
              line
            }
          }
        }
      }
    }
  }
}
"""


def _check_graphql_errors(result: dict[str, Any], context: str) -> None:
    """Raise GhError if a GraphQL response contains errors."""
    errors = result.get("errors")
    if errors:
        msg = f"GraphQL error in {context}: {errors[0].get('message', errors)}"
        raise gh.GhError(msg)


# GraphQL mutation to resolve a review thread
_RESOLVE_THREAD_MUTATION = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
"""


def _reviewer_auto_resolves(reviewer_name: str, comment_body: str = "") -> bool:
    """Check if a reviewer will auto-resolve a specific thread.

    First consults the config's ``auto_resolve_stale`` setting.  If config
    says we *should* auto-resolve (i.e. the reviewer does NOT handle its own),
    returns ``False`` so our bulk-resolve picks it up.  Otherwise delegates
    to the adapter's ``auto_resolves_thread`` method which may inspect the
    comment body (e.g. Devin skips info-level threads).
    """
    config = get_config()
    rc = config.get_reviewer(reviewer_name)
    # If config says auto_resolve_stale=True, WE handle resolution — reviewer
    # is not expected to auto-resolve, so return False ("reviewer won't do it").
    # If auto_resolve_stale=False, the reviewer handles its own resolution,
    # so delegate to the adapter for per-thread decisions.
    if rc.auto_resolve_stale:
        return False
    adapter = get_reviewer(reviewer_name)
    if not adapter:
        return False
    return adapter.auto_resolves_thread(comment_body)


def _parse_threads(raw_threads: list[dict[str, Any]], pr_number: int) -> list[ReviewThread]:
    """Parse raw GraphQL thread nodes into ReviewThread models."""
    threads = []
    for node in raw_threads:
        comments_raw = node.get("comments", {}).get("nodes", [])
        if not comments_raw:
            continue

        first_comment = comments_raw[0]
        author = (first_comment.get("author") or {}).get("login", "unknown")
        file_path = first_comment.get("path")

        comments = [
            ReviewComment(
                author=(c.get("author") or {}).get("login", "unknown"),
                body=c.get("body", ""),
                created_at=c.get("createdAt"),
            )
            for c in comments_raw
        ]

        threads.append(
            ReviewThread(
                thread_id=node["id"],
                pr_number=pr_number,
                status=CommentStatus.RESOLVED if node.get("isResolved") else CommentStatus.UNRESOLVED,
                file=file_path,
                line=first_comment.get("line"),
                reviewer=identify_reviewer(author),
                comments=comments,
                is_stale=node.get("isOutdated", False),
            )
        )
    return threads


# Map GitHub review states to our comment status
_REVIEW_STATE_MAP: dict[str, CommentStatus] = {
    "APPROVED": CommentStatus.RESOLVED,
    "DISMISSED": CommentStatus.RESOLVED,
    "CHANGES_REQUESTED": CommentStatus.UNRESOLVED,
    "COMMENTED": CommentStatus.UNRESOLVED,
}


def _get_pr_reviews(
    owner: str,
    repo: str,
    pr_number: int,
    cwd: str | None = None,
) -> list[ReviewThread]:
    """Fetch PR-level reviews from known AI reviewers.

    These are review summaries posted by AI tools (e.g. Devin's "N potential issues"
    or Unblocked's "N issues found") that appear on the PR conversation tab but are
    NOT inline code threads. Without this, reviewers like Devin that don't create
    inline threads are completely invisible.
    """
    result = gh.rest(f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews?per_page=100", cwd=cwd, paginate=True)
    if not result:
        return []

    threads: list[ReviewThread] = []
    for review in result:
        login = (review.get("user") or {}).get("login", "unknown")
        reviewer = identify_reviewer(login)
        if reviewer == "unknown":
            continue

        body = (review.get("body") or "").strip()
        if not body:
            continue

        state = review.get("state", "COMMENTED")
        status = _REVIEW_STATE_MAP.get(state, CommentStatus.UNRESOLVED)

        threads.append(
            ReviewThread(
                thread_id=review.get("node_id", ""),
                pr_number=pr_number,
                status=status,
                file=None,
                line=None,
                reviewer=reviewer,
                comments=[
                    ReviewComment(
                        author=login,
                        body=body,
                        created_at=review.get("submitted_at"),
                    ),
                ],
                is_stale=False,
                is_pr_review=True,
            )
        )
    return threads


def _get_pr_issue_comments(
    owner: str,
    repo: str,
    pr_number: int,
    cwd: str | None = None,
) -> list[ReviewThread]:
    """Fetch regular PR comments from bots (e.g. codecov, netlify, vercel).

    These are IssueComment nodes posted on the PR conversation tab — not review
    threads or PR reviews. Without this, bot feedback like coverage reports and
    deployment previews is invisible.
    """
    result = gh.rest(f"/repos/{owner}/{repo}/issues/{pr_number}/comments?per_page=100", cwd=cwd, paginate=True)
    if not result:
        return []

    threads: list[ReviewThread] = []
    for comment in result:
        login = (comment.get("user") or {}).get("login", "unknown")
        # Only include bot comments (login ends with [bot] or user type is Bot)
        user_type = (comment.get("user") or {}).get("type", "")
        is_bot = user_type == "Bot" or login.endswith("[bot]")
        if not is_bot:
            continue

        body = (comment.get("body") or "").strip()
        if not body:
            continue

        reviewer_name = identify_reviewer(login)
        threads.append(
            ReviewThread(
                thread_id=comment.get("node_id", ""),
                pr_number=pr_number,
                status=CommentStatus.UNRESOLVED,
                file=None,
                line=None,
                reviewer=reviewer_name if reviewer_name != "unknown" else login,
                comments=[
                    ReviewComment(
                        author=login,
                        body=body,
                        created_at=comment.get("created_at"),
                    ),
                ],
                is_stale=False,
                is_pr_review=True,
            )
        )
    return threads


def _get_pr_commits(
    owner: str,
    repo: str,
    pr_number: int,
    cwd: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch all commits on a PR with SHAs and timestamps.

    Uses ``--paginate`` to follow Link headers so PRs with >100
    commits return the complete list (fixes #95).
    """
    return gh.rest(f"/repos/{owner}/{repo}/pulls/{pr_number}/commits?per_page=100", cwd=cwd, paginate=True) or []


def _latest_push_time_from_commits(commits: list[dict[str, Any]]) -> datetime | None:
    """Extract the latest commit timestamp from a pre-fetched commits list."""
    from datetime import datetime  # noqa: PLC0415

    if not commits:
        return None

    last_commit = commits[-1]
    date_str = last_commit.get("commit", {}).get("committer", {}).get("date")
    if not date_str:
        return None

    return datetime.fromisoformat(date_str)


def _collect_reviewer_latest(threads: list[ReviewThread]) -> dict[str, datetime]:
    """Collect the latest comment timestamp per known AI reviewer."""
    from datetime import UTC  # noqa: PLC0415

    reviewer_latest: dict[str, datetime] = {}
    for thread in threads:
        if thread.reviewer == "unknown":
            continue
        # Only track known AI reviewers (skip generic bot names like "codecov[bot]")
        adapter = get_reviewer(thread.reviewer)
        if adapter is None:
            continue
        for comment in thread.comments:
            if comment.created_at is None:
                continue
            # Only count comments actually posted by the reviewer, not human replies
            if not adapter.identify(comment.author):
                continue
            ts = comment.created_at
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            if thread.reviewer not in reviewer_latest or ts > reviewer_latest[thread.reviewer]:
                reviewer_latest[thread.reviewer] = ts
    return reviewer_latest


def _compare_review_to_push(
    reviewer_name: str,
    latest_review: datetime,
    last_push_at: datetime | None,
) -> ReviewerStatus:
    """Compare a reviewer's latest comment against the last push to determine status."""
    from datetime import UTC  # noqa: PLC0415

    if last_push_at is None:
        return ReviewerStatus(
            reviewer=reviewer_name,
            status="completed",
            detail="Could not determine push time; assuming completed",
            last_review_at=latest_review,
            last_push_at=None,
        )

    push_at = last_push_at
    if push_at.tzinfo is None:
        push_at = push_at.replace(tzinfo=UTC)

    if latest_review >= push_at:
        return ReviewerStatus(
            reviewer=reviewer_name,
            status="completed",
            detail=f"{reviewer_name} reviewed after latest push",
            last_review_at=latest_review,
            last_push_at=push_at,
        )
    return ReviewerStatus(
        reviewer=reviewer_name,
        status="pending",
        detail=f"{reviewer_name} has not reviewed since latest push",
        last_review_at=latest_review,
        last_push_at=push_at,
    )


def _build_reviewer_statuses(
    threads: list[ReviewThread],
    last_push_at: datetime | None,
) -> list[ReviewerStatus]:
    """Build per-reviewer status by comparing review timestamps against latest push.

    Only reports on reviewers that have actually posted on this PR (data-driven).
    """
    reviewer_latest = _collect_reviewer_latest(threads)
    if not reviewer_latest:
        return []

    return [_compare_review_to_push(name, ts, last_push_at) for name, ts in reviewer_latest.items()]


async def _fetch_raw_threads(
    owner: str,
    repo_name: str,
    pr_number: int,
    cwd: str | None,
    ctx: Context | None,
) -> list[dict[str, Any]]:
    """Paginate through all review threads for a PR via GraphQL."""
    raw_threads: list[dict[str, Any]] = []
    cursor = None
    page = 0

    while True:
        page += 1
        if ctx:
            await ctx.report_progress(progress=page, total=None)
            await ctx.info(f"Fetching review threads for PR #{pr_number} (page {page})")

        variables: dict[str, Any] = {"owner": owner, "repo": repo_name, "pr": pr_number}
        if cursor:
            variables["cursor"] = cursor

        result = await call_sync_fn_in_threadpool(gh.graphql, _THREADS_QUERY, variables=variables, cwd=cwd)
        pr_data = result.get("data", {}).get("repository", {}).get("pullRequest") or {}
        threads_data = pr_data.get("reviewThreads", {})
        raw_threads.extend(threads_data.get("nodes", []))

        page_info = threads_data.get("pageInfo", {})
        if page_info.get("hasNextPage") and page_info.get("endCursor"):
            cursor = page_info["endCursor"]
        else:
            break

    return raw_threads


async def _collect_all_threads(
    owner: str,
    repo_name: str,
    pr_number: int,
    cwd: str | None,
    ctx: Context | None,
) -> list[ReviewThread]:
    """Fetch inline threads, PR-level reviews, and bot comments — filtered by config."""
    raw_threads = await _fetch_raw_threads(owner, repo_name, pr_number, cwd, ctx)
    threads = _parse_threads(raw_threads, pr_number)

    # Include PR-level reviews from AI reviewers (e.g. Devin summaries)
    pr_reviews = await call_sync_fn_in_threadpool(_get_pr_reviews, owner, repo_name, pr_number, cwd=cwd)
    threads.extend(pr_reviews)

    # Include regular PR comments from bots (e.g. codecov, netlify, vercel)
    bot_comments = await call_sync_fn_in_threadpool(_get_pr_issue_comments, owner, repo_name, pr_number, cwd=cwd)
    threads.extend(bot_comments)

    # Filter out threads from disabled reviewers
    config = get_config()
    return [t for t in threads if config.get_reviewer(t.reviewer).enabled]


async def _collect_inline_threads_only(  # noqa: PLR0913, PLR0917
    owner: str,
    repo_name: str,
    pr_number: int,
    status: str | None,
    cwd: str | None,
    ctx: Context | None,
) -> list[ReviewThread]:
    """Lightweight thread fetch — inline threads only, no staleness, no PR reviews, no bot comments.

    Skips commit fetch, compare API (staleness), PR-level reviews, bot comments,
    and stack discovery.  Suitable for callers that only need thread status,
    comment bodies, and reviewer identification (e.g. triage).
    """
    raw_threads = await _fetch_raw_threads(owner, repo_name, pr_number, cwd, ctx)
    threads = _parse_threads(raw_threads, pr_number)

    config = get_config()
    threads = [t for t in threads if config.get_reviewer(t.reviewer).enabled]

    if status:
        target = CommentStatus(status)
        threads = [t for t in threads if t.status == target]

    return threads


async def list_review_comments(
    pr_number: int,
    repo: str | None = None,
    status: str | None = None,
    cwd: str | None = None,
    ctx: Context | None = None,
) -> ReviewSummary:
    """List all review threads for a PR with reviewer identification, staleness, and reviewer status.

    Args:
        pr_number: The PR number to fetch comments for.
        repo: Repository in "owner/repo" format. Auto-detected if not provided.
        status: Filter by "resolved" or "unresolved". Returns all if not set.
        cwd: Working directory for git operations.
        ctx: FastMCP context for progress reporting. Injected by server tools.

    Returns:
        ReviewSummary with threads, per-reviewer statuses, and reviews_in_progress flag.
    """
    if repo:
        owner, repo_name = repo.split("/", 1)
    else:
        owner, repo_name = await call_sync_fn_in_threadpool(gh.get_repo_info, cwd=cwd)

    threads = await _collect_all_threads(owner, repo_name, pr_number, cwd, ctx)

    # Build reviewer statuses (timestamp heuristic)
    commits = await call_sync_fn_in_threadpool(_get_pr_commits, owner, repo_name, pr_number, cwd=cwd)
    last_push_at = _latest_push_time_from_commits(commits)
    reviewer_statuses = _build_reviewer_statuses(threads, last_push_at)
    reviews_in_progress = any(s.status == "pending" for s in reviewer_statuses)

    # Filter threads by status if requested (after building reviewer statuses from all threads)
    if status:
        target = CommentStatus(status)
        threads = [t for t in threads if t.status == target]

    if ctx:
        await ctx.info(f"Found {len(threads)} review threads for PR #{pr_number}")
        if reviews_in_progress:
            pending = [s.reviewer for s in reviewer_statuses if s.status == "pending"]
            await ctx.warning(f"⚠️ Reviews still pending from: {', '.join(pending)}")

    # Discover PR stack (cached per session) — best-effort, don't fail the request
    try:
        full_repo = f"{owner}/{repo_name}"
        stack_prs = await discover_stack(pr_number, repo=full_repo, cwd=cwd, ctx=ctx)
    except Exception:
        stack_prs = []

    return ReviewSummary(
        threads=threads,
        reviewer_statuses=reviewer_statuses,
        reviews_in_progress=reviews_in_progress,
        stack=stack_prs,
    )


async def list_stack_review_comments(
    pr_numbers: list[int],
    repo: str | None = None,
    status: str | None = None,
    cwd: str | None = None,
    ctx: Context | None = None,
) -> dict[int, ReviewSummary]:
    """List review threads for multiple PRs in a stack, grouped by PR number.

    Collapses N tool calls into 1 for the common stacked-PR review workflow.

    Args:
        pr_numbers: List of PR numbers to fetch comments for.
        repo: Repository in "owner/repo" format. Auto-detected if not provided.
        status: Filter by "resolved" or "unresolved". Returns all if not set.
        cwd: Working directory for git operations.
        ctx: FastMCP context for progress reporting. Injected by server tools.

    Returns:
        Dict mapping each PR number to its ReviewSummary.
    """
    results: dict[int, ReviewSummary] = {}
    total = len(pr_numbers)
    for i, pr_number in enumerate(pr_numbers):
        if ctx and total:
            await ctx.report_progress(progress=i, total=total)
        results[pr_number] = await list_review_comments(pr_number, repo=repo, status=status, cwd=cwd, ctx=ctx)
    if ctx and total:
        await ctx.report_progress(progress=total, total=total)
    return results


_THREAD_DETAIL_QUERY = """
query($threadId: ID!) {
  node(id: $threadId) {
    ... on PullRequestReviewThread {
      comments(first: 1) {
        nodes {
          author { login }
          body
        }
      }
    }
  }
}
"""


def _fetch_thread_detail(thread_id: str, cwd: str | None = None) -> tuple[str, str]:
    """Fetch reviewer name and first comment body for a thread.

    Returns:
        (reviewer_name, comment_body) — empty strings if lookup fails.
    """
    result = gh.graphql(_THREAD_DETAIL_QUERY, variables={"threadId": thread_id}, cwd=cwd)
    node = result.get("data", {}).get("node") or {}
    comments = node.get("comments", {}).get("nodes", [])
    if not comments:
        return "", ""
    first = comments[0]
    login = (first.get("author") or {}).get("login", "")
    reviewer = identify_reviewer(login)
    body = first.get("body", "")
    return reviewer, body


def resolve_comment(
    pr_number: int,
    thread_id: str,
    cwd: str | None = None,
) -> str:
    """Resolve a specific review thread by its GraphQL ID.

    Always fetches thread details server-side and enforces the per-reviewer
    ``resolve_levels`` policy — agents cannot bypass this.

    Args:
        pr_number: PR number (for context/logging).
        thread_id: The GraphQL node ID (PRRT_...) of the thread to resolve.
        cwd: Working directory.

    Returns:
        Confirmation message.

    Raises:
        gh.GhError: If the thread type is not resolvable or resolve is blocked by config.
    """
    if thread_id.startswith(("PRR_", "IC_")):
        msg = f"Cannot resolve PR-level reviews or bot comments — only inline review threads (PRRT_) are resolvable. Got: {thread_id}"
        raise gh.GhError(msg)

    # Config enforcement: fetch thread details and check resolve_levels
    reviewer_name, comment_body = _fetch_thread_detail(thread_id, cwd=cwd)
    if reviewer_name:
        from codereviewbuddy.tools.stack import _classify_severity  # noqa: PLC0415

        config = get_config()
        severity = _classify_severity(reviewer_name, comment_body)
        allowed, reason = config.can_resolve(reviewer_name, severity)
        if not allowed:
            raise gh.GhError(reason)

    result = gh.graphql(_RESOLVE_THREAD_MUTATION, variables={"threadId": thread_id}, cwd=cwd)
    _check_graphql_errors(result, f"resolve thread {thread_id}")

    thread_data = result.get("data", {}).get("resolveReviewThread", {}).get("thread", {})
    if thread_data.get("isResolved"):
        return f"Resolved thread {thread_id} on PR #{pr_number}"

    msg = f"Failed to resolve thread {thread_id} on PR #{pr_number}"
    raise gh.GhError(msg)


async def resolve_stale_comments(  # noqa: PLR0914
    pr_number: int,
    repo: str | None = None,
    cwd: str | None = None,
    ctx: Context | None = None,
) -> ResolveStaleResult:
    """Bulk-resolve all unresolved threads on lines that changed since the review.

    Args:
        pr_number: PR number.
        repo: Repository in "owner/repo" format. Auto-detected if not provided.
        cwd: Working directory.
        ctx: FastMCP context for progress reporting. Injected by server tools.

    Returns:
        Dict with "resolved_count" and "resolved_thread_ids".
    """
    config = get_config()
    from codereviewbuddy.tools.stack import _classify_severity  # noqa: PLC0415

    summary = await list_review_comments(pr_number, repo=repo, status="unresolved", cwd=cwd, ctx=ctx)
    stale = [t for t in summary.threads if t.is_stale and not t.is_pr_review]
    # Skip threads from reviewers that auto-resolve (e.g. Devin bugs, CodeRabbit)
    # but allow info-level threads through (Devin won't auto-resolve those)

    def _will_auto_resolve(t: ReviewThread) -> bool:
        body = t.comments[0].body if t.comments else ""
        return _reviewer_auto_resolves(t.reviewer, body)

    skipped = [t for t in stale if _will_auto_resolve(t)]
    stale = [t for t in stale if not _will_auto_resolve(t)]

    # Config enforcement: filter out threads whose severity exceeds resolve_levels
    allowed: list[ReviewThread] = []
    blocked_count = 0
    for t in stale:
        body = t.comments[0].body if t.comments else ""
        severity = _classify_severity(t.reviewer, body)
        can, _reason = config.can_resolve(t.reviewer, severity)
        if can:
            allowed.append(t)
        else:
            blocked_count += 1

    if not allowed:
        return ResolveStaleResult(
            resolved_count=0,
            resolved_thread_ids=[],
            skipped_count=len(skipped),
            blocked_count=blocked_count,
        )

    # Batch resolve using GraphQL aliases with parameterized variables
    params = []
    aliases = []
    variables: dict[str, str] = {}
    for i, thread in enumerate(allowed):
        var = f"t{i}"
        params.append(f"${var}: ID!")
        aliases.append(f"  {var}: resolveReviewThread(input: {{threadId: ${var}}}) {{ thread {{ id isResolved }} }}")
        variables[var] = thread.thread_id

    batch_mutation = f"mutation({', '.join(params)}) {{\n" + "\n".join(aliases) + "\n}"
    result = await call_sync_fn_in_threadpool(gh.graphql, batch_mutation, variables=variables, cwd=cwd)
    _check_graphql_errors(result, f"batch resolve {len(allowed)} threads on PR #{pr_number}")

    resolved_ids = [t.thread_id for t in allowed]
    if ctx:
        await ctx.info(f"Resolved {len(resolved_ids)} stale threads on PR #{pr_number}")
        if blocked_count:
            await ctx.warning(f"⚠️ {blocked_count} threads blocked by resolve_levels config")
    return ResolveStaleResult(
        resolved_count=len(resolved_ids),
        resolved_thread_ids=resolved_ids,
        skipped_count=len(skipped),
        blocked_count=blocked_count,
    )


def reply_to_comment(
    pr_number: int,
    thread_id: str,
    body: str,
    repo: str | None = None,
    cwd: str | None = None,
) -> str:
    """Reply to a specific review thread, PR-level review, or issue comment.

    Supports inline review threads (PRRT_ IDs), PR-level reviews (PRR_ IDs),
    and issue comments (IC_ IDs, e.g. bot comments from codecov/netlify).
    For PRRT_ threads, replies via the GraphQL addPullRequestReviewThreadReply mutation.
    For PRR_/IC_ IDs, posts a regular PR comment via the issues comments API.

    Args:
        pr_number: PR number.
        thread_id: The thread ID to reply to (PRRT_..., PRR_..., or IC_...).
        body: Reply text.
        repo: Repository in "owner/repo" format. Auto-detected if not provided.
        cwd: Working directory.

    Returns:
        Confirmation message.
    """
    # PRRT_ threads use GraphQL with only the thread ID — no repo info needed
    if thread_id.startswith("PRRT_"):
        return _reply_to_review_thread(pr_number, thread_id, body, cwd=cwd)

    # IC_ and PRR_ paths need owner/repo for the issues comments API
    if repo:
        owner, repo_name = repo.split("/", 1)
    else:
        owner, repo_name = gh.get_repo_info(cwd=cwd)

    if thread_id.startswith("IC_"):
        return _reply_to_pr_comment(pr_number, owner, repo_name, body, kind="bot comment", cwd=cwd)
    if thread_id.startswith("PRR_"):
        return _reply_to_pr_comment(pr_number, owner, repo_name, body, kind="PR-level review", cwd=cwd)

    return _reply_to_review_thread(pr_number, thread_id, body, cwd=cwd)


_REPLY_TO_THREAD_MUTATION = """
mutation($threadId: ID!, $body: String!) {
  addPullRequestReviewThreadReply(input: {
    pullRequestReviewThreadId: $threadId,
    body: $body
  }) {
    comment { id }
  }
}
"""


def _reply_to_review_thread(
    pr_number: int,
    thread_id: str,
    body: str,
    cwd: str | None = None,
) -> str:
    """Reply to an inline review thread (PRRT_ ID) via GraphQL mutation."""
    result = gh.graphql(
        _REPLY_TO_THREAD_MUTATION,
        variables={"threadId": thread_id, "body": body},
        cwd=cwd,
    )
    _check_graphql_errors(result, f"reply to thread {thread_id}")
    return f"Replied to thread {thread_id} on PR #{pr_number}"


def _reply_to_pr_comment(  # noqa: PLR0913, PLR0917
    pr_number: int,
    owner: str,
    repo_name: str,
    body: str,
    kind: str = "PR-level review",
    cwd: str | None = None,
) -> str:
    """Reply to a PR-level review or bot comment by posting an issue comment."""
    gh.rest(
        f"/repos/{owner}/{repo_name}/issues/{pr_number}/comments",
        method="POST",
        body=body,
        cwd=cwd,
    )
    return f"Replied to {kind} on PR #{pr_number}"


# ---------------------------------------------------------------------------
# Triage — actionable threads only (#96)
# ---------------------------------------------------------------------------

_BOLD_TITLE_RE = re.compile(r"\*\*(?:Bug|Info|Warning|Flagged)?:?\s*(.+?)\*\*", re.IGNORECASE)
_ISSUE_REF_RE = re.compile(r"#\d+")
_FOLLOWUP_KEYWORDS = re.compile(r"noted for followup|tracked for later|will address later|followup", re.IGNORECASE)

_SEVERITY_ORDER = {"bug": 0, "flagged": 1, "warning": 2, "info": 3}


def _extract_title(body: str) -> str:
    """Extract a short title from the first bold text in a comment."""
    match = _BOLD_TITLE_RE.search(body)
    return match.group(1).strip() if match else ""


def _has_owner_reply(thread: ReviewThread, owner_logins: frozenset[str]) -> bool:
    """Check if any comment in the thread is from the repo owner / agent."""
    return any(c.author in owner_logins for c in thread.comments)


def _has_followup_without_issue(thread: ReviewThread, owner_logins: frozenset[str]) -> bool:
    """Check if the owner replied with a 'noted for followup' but no issue reference anywhere in the thread."""
    owner_comments = [c for c in thread.comments if c.author in owner_logins]
    has_followup = any(_FOLLOWUP_KEYWORDS.search(c.body) for c in owner_comments)
    if not has_followup:
        return False
    has_issue_ref = any(_ISSUE_REF_RE.search(c.body) for c in owner_comments)
    return not has_issue_ref


def _classify_action(severity: str) -> str:
    """Map severity to suggested action."""
    if severity in {"bug", "flagged"}:
        return "fix"
    return "reply"


async def triage_review_comments(
    pr_numbers: list[int],
    repo: str | None = None,
    owner_logins: list[str] | None = None,
    cwd: str | None = None,
    ctx: Context | None = None,
) -> TriageResult:
    """Return only threads that need agent action — no noise, no full bodies.

    Filters:
    - Unresolved inline threads only (excludes PR-level reviews).
    - Excludes threads that already have an owner reply.
    - Pre-classifies severity using reviewer adapters.
    - Flags 'noted for followup' replies that don't reference a GH issue.

    Args:
        pr_numbers: PR numbers to triage.
        repo: Repository in "owner/repo" format. Auto-detected if not provided.
        owner_logins: GitHub usernames considered "ours" (agent + human).
            Defaults to ``["ichoosetoaccept"]`` if not provided.
        cwd: Working directory for git operations.
        ctx: FastMCP context for progress reporting.

    Returns:
        TriageResult with only actionable items, sorted by severity.
    """
    from codereviewbuddy.tools.stack import _classify_severity  # noqa: PLC0415

    owners = frozenset(owner_logins or ["ichoosetoaccept"])
    items: list[TriageItem] = []
    issue_items: list[TriageItem] = []

    if repo:
        owner, repo_name = repo.split("/", 1)
    else:
        owner, repo_name = await call_sync_fn_in_threadpool(gh.get_repo_info, cwd=cwd)

    total = len(pr_numbers)
    for i, pr_number in enumerate(pr_numbers):
        if ctx and total:
            await ctx.report_progress(i, total)

        threads = await _collect_inline_threads_only(owner, repo_name, pr_number, status="unresolved", cwd=cwd, ctx=ctx)

        for thread in threads:
            # Skip PR-level reviews — they're summary wrappers, not actionable
            if thread.is_pr_review:
                continue

            # Check for "noted for followup" without issue ref (even if owner already replied)
            if _has_followup_without_issue(thread, owners):
                first_body = thread.comments[0].body if thread.comments else ""
                severity = _classify_severity(thread.reviewer, first_body).value
                issue_items.append(
                    TriageItem(
                        thread_id=thread.thread_id,
                        pr_number=thread.pr_number,
                        file=thread.file,
                        line=thread.line,
                        reviewer=thread.reviewer,
                        severity=severity,
                        title=_extract_title(first_body),
                        is_stale=thread.is_stale,
                        action="create_issue",
                        snippet=first_body[:200],
                    )
                )
                continue

            # Skip threads that already have an owner reply
            if _has_owner_reply(thread, owners):
                continue

            first_body = thread.comments[0].body if thread.comments else ""
            severity = _classify_severity(thread.reviewer, first_body).value
            action = _classify_action(severity)

            items.append(
                TriageItem(
                    thread_id=thread.thread_id,
                    pr_number=thread.pr_number,
                    file=thread.file,
                    line=thread.line,
                    reviewer=thread.reviewer,
                    severity=severity,
                    title=_extract_title(first_body),
                    is_stale=thread.is_stale,
                    action=action,
                    snippet=first_body[:200],
                )
            )

    if ctx and total:
        await ctx.report_progress(total, total)

    # Sort all items by severity (bugs first)
    all_items = items + issue_items
    all_items.sort(key=lambda x: _SEVERITY_ORDER.get(x.severity, 99))
    needs_fix = sum(1 for item in all_items if item.action == "fix")
    needs_reply = sum(1 for item in all_items if item.action == "reply")
    needs_issue = len(issue_items)

    return TriageResult(
        items=all_items,
        needs_fix=needs_fix,
        needs_reply=needs_reply,
        needs_issue=needs_issue,
        total=len(all_items),
    )
