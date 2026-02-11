"""MCP tools for managing PR review comments."""

from __future__ import annotations

import logging
import operator
from typing import TYPE_CHECKING

from fastmcp.utilities.async_utils import call_sync_fn_in_threadpool

from codereviewbuddy import gh
from codereviewbuddy.config import Severity, get_config
from codereviewbuddy.models import CommentStatus, ResolveStaleResult, ReviewComment, ReviewerStatus, ReviewSummary, ReviewThread
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
                is_stale=False,
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


def _get_files_changed_between(
    owner: str,
    repo: str,
    base_sha: str,
    head_sha: str,
    cwd: str | None = None,
) -> set[str]:
    """Get files changed between two commits using the compare API.

    Returns an empty set on API errors (e.g. 404 after force-push
    rewrites history and the SHA no longer exists).
    """
    try:
        result = gh.rest(f"/repos/{owner}/{repo}/compare/{base_sha}...{head_sha}", cwd=cwd)
    except gh.GhError:
        logger.debug("Compare API failed for %s...%s, treating as empty", base_sha[:7], head_sha[:7])
        return set()
    if not result:
        return set()
    return {f["filename"] for f in result.get("files", []) if f.get("filename")}


def _compute_staleness(
    threads: list[ReviewThread],
    commits: list[dict[str, Any]],
    owner: str,
    repo: str,
    cwd: str | None = None,
) -> None:
    """Compute per-thread staleness by checking if the file changed after the comment.

    A thread is stale when commits pushed AFTER the comment's timestamp
    modify the same file the comment is on.  Uses the GitHub compare API,
    cached by review-point SHA to minimise API calls.
    """
    from datetime import UTC, datetime

    if not commits:
        return

    # HEAD is the last commit in API order (topological), NOT the latest by timestamp.
    # Timestamp order can diverge after rebases, cherry-picks, or --amend --date.
    head_sha = commits[-1].get("sha")
    if not head_sha:
        return

    # Build a sorted timeline of (timestamp, sha) for review-point lookup
    timeline: list[tuple[datetime, str]] = []
    for c in commits:
        date_str = c.get("commit", {}).get("committer", {}).get("date")
        sha = c.get("sha")
        if date_str and sha:
            ts = datetime.fromisoformat(date_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            timeline.append((ts, sha))
    timeline.sort(key=operator.itemgetter(0))

    if not timeline:
        return
    # Cache compare results keyed by review-point SHA
    compare_cache: dict[str, set[str]] = {}

    for thread in threads:
        if not thread.file or not thread.comments:
            continue
        comment_time = thread.comments[0].created_at
        if comment_time is None:
            continue
        if comment_time.tzinfo is None:
            comment_time = comment_time.replace(tzinfo=UTC)

        # Find the latest commit that existed when the comment was posted
        review_point_sha: str | None = None
        for ts, sha in timeline:
            if ts <= comment_time:
                review_point_sha = sha
            else:
                break

        # If the comment predates all commits, every commit is "after" it —
        # fall back to comparing the first commit's parent (base).
        # For simplicity, skip staleness in this edge case.
        if review_point_sha is None:
            continue

        # No commits after the review (by timestamp) → not stale.
        # Compare against the last timeline entry, NOT head_sha, because
        # timestamps can be non-monotonic (rebase/cherry-pick) while head_sha
        # is topological — they can diverge.
        if review_point_sha == timeline[-1][1]:
            continue

        # Fetch (cached) files changed since review point
        if review_point_sha not in compare_cache:
            compare_cache[review_point_sha] = _get_files_changed_between(owner, repo, review_point_sha, head_sha, cwd=cwd)

        thread.is_stale = thread.file in compare_cache[review_point_sha]


def _latest_push_time_from_commits(commits: list[dict[str, Any]]) -> datetime | None:
    """Extract the latest commit timestamp from a pre-fetched commits list."""
    from datetime import datetime

    if not commits:
        return None

    last_commit = commits[-1]
    date_str = last_commit.get("commit", {}).get("committer", {}).get("date")
    if not date_str:
        return None

    return datetime.fromisoformat(date_str)


def _build_reviewer_statuses(
    threads: list[ReviewThread],
    last_push_at: datetime | None,
) -> list[ReviewerStatus]:
    """Build per-reviewer status by comparing review timestamps against latest push.

    Only reports on reviewers that have actually posted on this PR (data-driven).
    """
    from datetime import UTC

    # Collect the latest comment timestamp per known reviewer
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

    if not reviewer_latest:
        return []

    statuses: list[ReviewerStatus] = []
    for reviewer_name, latest_review in reviewer_latest.items():
        if last_push_at is None:
            statuses.append(
                ReviewerStatus(
                    reviewer=reviewer_name,
                    status="completed",
                    detail="Could not determine push time; assuming completed",
                    last_review_at=latest_review,
                    last_push_at=None,
                )
            )
            continue

        push_at = last_push_at
        if push_at.tzinfo is None:
            push_at = push_at.replace(tzinfo=UTC)

        if latest_review >= push_at:
            statuses.append(
                ReviewerStatus(
                    reviewer=reviewer_name,
                    status="completed",
                    detail=f"{reviewer_name} reviewed after latest push",
                    last_review_at=latest_review,
                    last_push_at=push_at,
                )
            )
        else:
            statuses.append(
                ReviewerStatus(
                    reviewer=reviewer_name,
                    status="pending",
                    detail=f"{reviewer_name} has not reviewed since latest push",
                    last_review_at=latest_review,
                    last_push_at=push_at,
                )
            )

    return statuses


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

    # Fetch threads (paginated) and changed files
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

    # Fetch commits once — reused for staleness and reviewer status
    commits = await call_sync_fn_in_threadpool(_get_pr_commits, owner, repo_name, pr_number, cwd=cwd)

    threads = _parse_threads(raw_threads, pr_number)

    # Compute per-thread staleness (file changed after comment was posted)
    await call_sync_fn_in_threadpool(_compute_staleness, threads, commits, owner, repo_name, cwd=cwd)

    # Include PR-level reviews from AI reviewers (e.g. Devin summaries)
    pr_reviews = await call_sync_fn_in_threadpool(_get_pr_reviews, owner, repo_name, pr_number, cwd=cwd)
    threads.extend(pr_reviews)

    # Include regular PR comments from bots (e.g. codecov, netlify, vercel)
    bot_comments = await call_sync_fn_in_threadpool(_get_pr_issue_comments, owner, repo_name, pr_number, cwd=cwd)
    threads.extend(bot_comments)

    # Filter out threads from disabled reviewers
    config = get_config()
    threads = [t for t in threads if config.get_reviewer(t.reviewer).enabled]

    # Build reviewer statuses (timestamp heuristic)
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
        if ctx:
            await ctx.report_progress(progress=i, total=total)
        results[pr_number] = await list_review_comments(pr_number, repo=repo, status=status, cwd=cwd, ctx=ctx)
    if ctx:
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
        config = get_config()
        adapter = get_reviewer(reviewer_name)
        severity = adapter.classify_severity(comment_body) if adapter else Severity.INFO
        allowed, reason = config.can_resolve(reviewer_name, severity)
        if not allowed:
            raise gh.GhError(reason)

    result = gh.graphql(_RESOLVE_THREAD_MUTATION, variables={"threadId": thread_id}, cwd=cwd)

    thread_data = result.get("data", {}).get("resolveReviewThread", {}).get("thread", {})
    if thread_data.get("isResolved"):
        return f"Resolved thread {thread_id} on PR #{pr_number}"

    msg = f"Failed to resolve thread {thread_id} on PR #{pr_number}"
    raise gh.GhError(msg)


async def resolve_stale_comments(
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
        adapter = get_reviewer(t.reviewer)
        severity = adapter.classify_severity(body) if adapter else Severity.INFO
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

    # Batch resolve using GraphQL aliases
    mutations = []
    for i, thread in enumerate(allowed):
        mutations.append(f'  t{i}: resolveReviewThread(input: {{threadId: "{thread.thread_id}"}}) {{ thread {{ id isResolved }} }}')

    batch_mutation = "mutation {\n" + "\n".join(mutations) + "\n}"
    await call_sync_fn_in_threadpool(gh.graphql, batch_mutation, cwd=cwd)

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
    errors = result.get("errors")
    if errors:
        msg = f"GraphQL error replying to {thread_id}: {errors[0].get('message', errors)}"
        raise gh.GhError(msg)
    return f"Replied to thread {thread_id} on PR #{pr_number}"


def _reply_to_pr_comment(
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
