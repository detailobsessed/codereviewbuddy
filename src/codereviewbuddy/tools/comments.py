"""MCP tools for managing PR review comments."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Literal

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
              url
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

# GraphQL mutation to dismiss a PR-level review (#120)
_DISMISS_REVIEW_MUTATION = """
mutation($reviewId: ID!, $message: String!) {
  dismissPullRequestReview(input: {pullRequestReviewId: $reviewId, message: $message}) {
    pullRequestReview { id state }
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


# -- Body stripping (issue #99) ------------------------------------------------

# HTML comment blocks injected by reviewer bots (badges, metadata, etc.)
_HTML_COMMENT_BLOCK_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# <details>...</details> → keep only the <summary> text
_DETAILS_BLOCK_RE = re.compile(
    r"<details[^>]*>\s*<summary[^>]*>(.*?)</summary>.*?</details>",
    re.DOTALL,
)

# Any remaining HTML tags
_HTML_TAG_RE = re.compile(r"<[^>]+>")

# Collapse 3+ consecutive blank lines into 2
_BLANK_LINES_RE = re.compile(r"\n{3,}")

_MAX_BODY_LENGTH = 2000


def _strip_comment_body(body: str) -> str:
    """Strip reviewer badge HTML, collapse <details> blocks, remove tags.

    This keeps comment bodies small enough for LLM context windows while
    preserving the actual review content.
    """
    # 1. Remove HTML comment blocks (badge metadata, tracking pixels, etc.)
    body = _HTML_COMMENT_BLOCK_RE.sub("", body)

    # 2. Collapse <details> blocks to just their <summary> text
    body = _DETAILS_BLOCK_RE.sub(r"[details: \1]", body)

    # 3. Strip remaining HTML tags
    body = _HTML_TAG_RE.sub("", body)

    # 4. Clean up whitespace
    body = _BLANK_LINES_RE.sub("\n\n", body)
    body = body.strip()

    # 5. Truncate extremely long bodies
    if len(body) > _MAX_BODY_LENGTH:
        body = body[:_MAX_BODY_LENGTH] + "… [truncated]"

    return body


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
                body=_strip_comment_body(c.get("body", "")),
                created_at=c.get("createdAt"),
                url=c.get("url", ""),
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

        raw_body = (review.get("body") or "").strip()
        if not raw_body:
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
                        body=_strip_comment_body(raw_body),
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

        raw_body = (comment.get("body") or "").strip()
        if not raw_body:
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
                        body=_strip_comment_body(raw_body),
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
        _check_graphql_errors(result, f"fetch review threads for PR #{pr_number} (page {page})")
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
        ReviewSummary with threads and per-reviewer statuses.
    """
    if repo:
        owner, repo_name = gh.parse_repo(repo)
    else:
        owner, repo_name = await call_sync_fn_in_threadpool(gh.get_repo_info, cwd=cwd)

    threads = await _collect_all_threads(owner, repo_name, pr_number, cwd, ctx)

    # Build reviewer statuses (timestamp heuristic)
    commits = await call_sync_fn_in_threadpool(_get_pr_commits, owner, repo_name, pr_number, cwd=cwd)
    last_push_at = _latest_push_time_from_commits(commits)
    reviewer_statuses = _build_reviewer_statuses(threads, last_push_at)

    # Filter threads by status if requested (after building reviewer statuses from all threads)
    if status:
        target = CommentStatus(status)
        threads = [t for t in threads if t.status == target]

    if ctx:
        await ctx.info(f"Found {len(threads)} review threads for PR #{pr_number}")

    # Discover PR stack (cached per session) — best-effort, don't fail the request
    try:
        full_repo = f"{owner}/{repo_name}"
        stack_prs = await discover_stack(pr_number, repo=full_repo, cwd=cwd, ctx=ctx)
    except Exception:
        stack_prs = []

    # Build next_steps and message based on review state
    next_steps: list[str] = []
    message = ""
    unresolved = [t for t in threads if t.status == CommentStatus.UNRESOLVED]

    if not threads:
        message = f"No review threads found on PR #{pr_number}. Reviewers may not have posted yet."
    elif not unresolved:
        message = f"All {len(threads)} review threads on PR #{pr_number} are resolved."
        next_steps.append("Call resolve_stale_comments() if you pushed new changes since the last review.")
    else:
        stale_count = sum(1 for t in unresolved if t.is_stale)
        if stale_count:
            next_steps.append(f"Call resolve_stale_comments() to batch-resolve {stale_count} stale thread(s).")
        if stack_prs and len(stack_prs) > 1:
            pr_nums = [p.pr_number for p in stack_prs]
            next_steps.append(f"Call triage_review_comments(pr_numbers={pr_nums}) for actionable threads across the stack.")
        else:
            next_steps.append(f"Call triage_review_comments(pr_numbers=[{pr_number}]) for actionable-only view.")

    return ReviewSummary(
        threads=threads,
        reviewer_statuses=reviewer_statuses,
        stack=stack_prs,
        next_steps=next_steps,
        message=message,
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
      comments(first: 50) {
        nodes {
          author { login }
          body
        }
      }
    }
  }
}
"""


def _fetch_thread_detail(thread_id: str, cwd: str | None = None) -> tuple[str, str, list[str]]:
    """Fetch reviewer name, first comment body, and all comment author logins for a thread.

    Returns:
        (reviewer_name, comment_body, all_logins) — empty strings/list if lookup fails.
    """
    result = gh.graphql(_THREAD_DETAIL_QUERY, variables={"threadId": thread_id}, cwd=cwd)
    _check_graphql_errors(result, f"fetch thread detail {thread_id}")
    node = result.get("data", {}).get("node") or {}
    comments = node.get("comments", {}).get("nodes", [])
    if not comments:
        return "", "", []
    first = comments[0]
    login = (first.get("author") or {}).get("login", "")
    reviewer = identify_reviewer(login)
    body = first.get("body", "")
    all_logins = [(c.get("author") or {}).get("login", "") for c in comments]
    return reviewer, body, all_logins


def _has_any_reply(all_logins: list[str]) -> bool:
    """Check if a thread has at least one reply (from anyone).

    The first comment is the reviewer's original.  Any subsequent comment
    — human or bot — counts as a reply, ensuring there is a decision-log
    entry before the thread can be resolved.
    """
    return len(all_logins) > 1


def _dismiss_pr_review(
    pr_number: int,
    review_id: str,
    message: str = "Dismissed via codereviewbuddy",
    cwd: str | None = None,
) -> str:
    """Dismiss a PR-level review (PRR_ ID) via GraphQL mutation.

    Args:
        pr_number: PR number (for context/logging).
        review_id: The GraphQL node ID (PRR_...) of the review to dismiss.
        message: Reason for dismissal.
        cwd: Working directory.

    Returns:
        Confirmation message.

    Raises:
        gh.GhError: If the dismiss fails.
    """
    result = gh.graphql(
        _DISMISS_REVIEW_MUTATION,
        variables={"reviewId": review_id, "message": message},
        cwd=cwd,
    )
    _check_graphql_errors(result, f"dismiss review {review_id}")

    review_data = result.get("data", {}).get("dismissPullRequestReview", {}).get("pullRequestReview", {})
    if review_data.get("state") == "DISMISSED":
        return f"Dismissed PR-level review {review_id} on PR #{pr_number}"

    msg = f"Failed to dismiss PR-level review {review_id} on PR #{pr_number}"
    raise gh.GhError(msg)


def resolve_comment(
    pr_number: int,
    thread_id: str,
    cwd: str | None = None,
) -> str:
    """Resolve a review thread (PRRT_) or dismiss a PR-level review (PRR_).

    For inline threads (PRRT_), fetches thread details server-side and enforces
    the per-reviewer ``resolve_levels`` policy — agents cannot bypass this.
    For PR-level reviews (PRR_), dismisses the review via GraphQL.

    Args:
        pr_number: PR number (for context/logging).
        thread_id: The GraphQL node ID (PRRT_... or PRR_...) to resolve/dismiss.
        cwd: Working directory.

    Returns:
        Confirmation message.

    Raises:
        gh.GhError: If the thread type is not supported or resolve is blocked by config.
    """
    if thread_id.startswith("IC_"):
        msg = (
            f"Cannot resolve bot comments — only inline review threads (PRRT_) and PR-level reviews (PRR_) are supported. Got: {thread_id}"
        )
        raise gh.GhError(msg)

    if thread_id.startswith("PRR_"):
        return _dismiss_pr_review(pr_number, thread_id, cwd=cwd)

    # Config enforcement: fetch thread details and check resolve_levels + reply requirement
    reviewer_name, comment_body, all_logins = _fetch_thread_detail(thread_id, cwd=cwd)
    if reviewer_name:
        from codereviewbuddy.tools.stack import _classify_severity  # noqa: PLC0415

        config = get_config()
        severity = _classify_severity(reviewer_name, comment_body)
        allowed, reason = config.can_resolve(reviewer_name, severity)
        if not allowed:
            raise gh.GhError(reason)

        rc = config.get_reviewer(reviewer_name)
        if rc.require_reply_before_resolve and not _has_any_reply(all_logins):
            msg = "Cannot resolve thread without a reply. Add a reply explaining how the feedback was addressed, then resolve."
            raise gh.GhError(msg)

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
        no_resolve_steps: list[str] = []
        if blocked_count:
            no_resolve_steps.append(
                f"{blocked_count} thread(s) blocked by resolve_levels config — inform the user about the severity restrictions."
            )
        if skipped:
            no_resolve_steps.append(f"{len(skipped)} thread(s) skipped — their reviewer auto-resolves on push.")
        if not blocked_count and not skipped:
            no_resolve_steps.append("No stale unresolved threads found. All comments are on current code.")
        return ResolveStaleResult(
            resolved_count=0,
            resolved_thread_ids=[],
            skipped_count=len(skipped),
            blocked_count=blocked_count,
            next_steps=no_resolve_steps,
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

    resolve_steps: list[str] = []
    if blocked_count:
        resolve_steps.append(
            f"{blocked_count} thread(s) blocked by resolve_levels config — inform the user about the severity restrictions."
        )
    resolve_steps.extend([
        "Call summarize_review_status() to verify remaining unresolved threads.",
        "Trigger re-reviews for manual-trigger reviewers (Unblocked, Greptile) if you pushed fixes.",
    ])

    return ResolveStaleResult(
        resolved_count=len(resolved_ids),
        resolved_thread_ids=resolved_ids,
        skipped_count=len(skipped),
        blocked_count=blocked_count,
        next_steps=resolve_steps,
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
        owner, repo_name = gh.parse_repo(repo)
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


def _classify_action(severity: str) -> Literal["fix", "reply"]:
    """Map severity to suggested action."""
    if severity in {"bug", "flagged"}:
        return "fix"
    return "reply"


def _thread_to_triage_item(
    thread: ReviewThread,
    classify_severity: Any,
    action: Literal["fix", "reply", "create_issue"] = "reply",
) -> TriageItem:
    """Convert a ReviewThread into a TriageItem with severity classification."""
    first = thread.comments[0] if thread.comments else None
    body = first.body if first else ""
    severity = classify_severity(thread.reviewer, body).value
    if action != "create_issue":
        action = _classify_action(severity)
    return TriageItem(
        thread_id=thread.thread_id,
        pr_number=thread.pr_number,
        file=thread.file,
        line=thread.line,
        reviewer=thread.reviewer,
        severity=severity,
        title=_extract_title(body),
        is_stale=thread.is_stale,
        action=action,
        snippet=body[:200],
        comment_url=first.url if first else "",
    )


def _build_triage_hints(
    all_items: list[TriageItem],
    needs_fix: int,
    needs_reply: int,
    needs_issue: int,
) -> tuple[list[str], str]:
    """Build next_steps and message for a TriageResult."""
    next_steps: list[str] = []
    message = ""
    if not all_items:
        message = "No actionable threads — all threads have owner replies or are resolved."
        next_steps.append("Call resolve_stale_comments() to clean up any stale threads.")
    else:
        if needs_fix:
            next_steps.append(f"Fix the {needs_fix} bug/flagged item(s) first, then call reply_to_comment() for each explaining the fix.")
        if needs_reply:
            next_steps.append(f"Reply to the {needs_reply} info/warning thread(s) with reply_to_comment().")
        if needs_issue:
            next_steps.append(f"Call create_issue_from_comment() for the {needs_issue} followup(s) missing a GitHub issue reference.")
    return next_steps, message


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
        owner, repo_name = gh.parse_repo(repo)
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
                issue_items.append(_thread_to_triage_item(thread, _classify_severity, action="create_issue"))
                continue

            # Skip threads that already have an owner reply
            if _has_owner_reply(thread, owners):
                continue

            items.append(_thread_to_triage_item(thread, _classify_severity))

    if ctx and total:
        await ctx.report_progress(total, total)

    # Sort all items by severity (bugs first)
    all_items = items + issue_items
    all_items.sort(key=lambda x: _SEVERITY_ORDER.get(x.severity, 99))
    needs_fix = sum(1 for item in all_items if item.action == "fix")
    needs_reply = sum(1 for item in all_items if item.action == "reply")
    needs_issue = len(issue_items)

    next_steps, message = _build_triage_hints(all_items, needs_fix, needs_reply, needs_issue)

    return TriageResult(
        items=all_items,
        needs_fix=needs_fix,
        needs_reply=needs_reply,
        needs_issue=needs_issue,
        total=len(all_items),
        next_steps=next_steps,
        message=message,
    )
