"""MCP tools for managing PR review comments."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp.utilities.async_utils import call_sync_fn_in_threadpool

from codereviewbuddy import gh
from codereviewbuddy.models import CommentStatus, ResolveStaleResult, ReviewComment, ReviewThread
from codereviewbuddy.reviewers import get_reviewer, identify_reviewer

if TYPE_CHECKING:
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

# GraphQL query to get the diff for staleness detection (paginated)
_DIFF_QUERY = """
query($owner: String!, $repo: String!, $pr: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      files(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          path
          additions
          deletions
          changeType
        }
      }
    }
  }
}
"""


def _reviewer_auto_resolves(reviewer_name: str) -> bool:
    """Check if a reviewer auto-resolves addressed comments on new pushes."""
    adapter = get_reviewer(reviewer_name)
    return adapter.auto_resolves_comments if adapter else False


def _parse_threads(raw_threads: list[dict[str, Any]], pr_number: int, changed_files: set[str] | None = None) -> list[ReviewThread]:
    """Parse raw GraphQL thread nodes into ReviewThread models."""
    threads = []
    for node in raw_threads:
        comments_raw = node.get("comments", {}).get("nodes", [])
        if not comments_raw:
            continue

        first_comment = comments_raw[0]
        author = (first_comment.get("author") or {}).get("login", "unknown")
        file_path = first_comment.get("path")

        # Staleness: if the file has been modified since the review, it's stale
        is_stale = False
        if changed_files and file_path:
            is_stale = file_path in changed_files

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
                is_stale=is_stale,
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
    result = gh.rest(f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews", cwd=cwd)
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


def _get_changed_files(owner: str, repo: str, pr_number: int, cwd: str | None = None) -> set[str]:
    """Get the set of files changed in the latest push of a PR."""
    all_files: list[dict[str, Any]] = []
    cursor = None

    while True:
        variables: dict[str, Any] = {"owner": owner, "repo": repo, "pr": pr_number}
        if cursor:
            variables["cursor"] = cursor

        result = gh.graphql(_DIFF_QUERY, variables=variables, cwd=cwd)
        pr_data = result.get("data", {}).get("repository", {}).get("pullRequest") or {}
        files_data = pr_data.get("files", {})
        all_files.extend(files_data.get("nodes", []))

        page_info = files_data.get("pageInfo", {})
        if page_info.get("hasNextPage") and page_info.get("endCursor"):
            cursor = page_info["endCursor"]
        else:
            break

    return {f["path"] for f in all_files if f.get("path")}


async def list_review_comments(
    pr_number: int,
    repo: str | None = None,
    status: str | None = None,
    cwd: str | None = None,
    ctx: Context | None = None,
) -> list[ReviewThread]:
    """List all review threads for a PR with reviewer identification and staleness detection.

    Args:
        pr_number: The PR number to fetch comments for.
        repo: Repository in "owner/repo" format. Auto-detected if not provided.
        status: Filter by "resolved" or "unresolved". Returns all if not set.
        cwd: Working directory for git operations.
        ctx: FastMCP context for progress reporting. Injected by server tools.

    Returns:
        List of ReviewThread objects.
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

    changed_files = await call_sync_fn_in_threadpool(_get_changed_files, owner, repo_name, pr_number, cwd=cwd)

    threads = _parse_threads(raw_threads, pr_number, changed_files)

    # Include PR-level reviews from AI reviewers (e.g. Devin summaries)
    pr_reviews = await call_sync_fn_in_threadpool(_get_pr_reviews, owner, repo_name, pr_number, cwd=cwd)
    threads.extend(pr_reviews)

    # Filter by status if requested
    if status:
        target = CommentStatus(status)
        threads = [t for t in threads if t.status == target]

    if ctx:
        await ctx.info(f"Found {len(threads)} review threads for PR #{pr_number}")

    return threads


async def list_stack_review_comments(
    pr_numbers: list[int],
    repo: str | None = None,
    status: str | None = None,
    cwd: str | None = None,
    ctx: Context | None = None,
) -> dict[int, list[ReviewThread]]:
    """List review threads for multiple PRs in a stack, grouped by PR number.

    Collapses N tool calls into 1 for the common stacked-PR review workflow.

    Args:
        pr_numbers: List of PR numbers to fetch comments for.
        repo: Repository in "owner/repo" format. Auto-detected if not provided.
        status: Filter by "resolved" or "unresolved". Returns all if not set.
        cwd: Working directory for git operations.
        ctx: FastMCP context for progress reporting. Injected by server tools.

    Returns:
        Dict mapping each PR number to its list of ReviewThread objects.
    """
    results: dict[int, list[ReviewThread]] = {}
    total = len(pr_numbers)
    for i, pr_number in enumerate(pr_numbers):
        if ctx:
            await ctx.report_progress(progress=i, total=total)
        results[pr_number] = await list_review_comments(pr_number, repo=repo, status=status, cwd=cwd, ctx=ctx)
    if ctx:
        await ctx.report_progress(progress=total, total=total)
    return results


def resolve_comment(
    pr_number: int,
    thread_id: str,
    cwd: str | None = None,
) -> str:
    """Resolve a specific review thread by its GraphQL ID.

    Args:
        pr_number: PR number (for context/logging).
        thread_id: The GraphQL node ID (PRRT_...) of the thread to resolve.
        cwd: Working directory.

    Returns:
        Confirmation message.
    """
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
    threads = await list_review_comments(pr_number, repo=repo, status="unresolved", cwd=cwd, ctx=ctx)
    stale = [t for t in threads if t.is_stale and not t.is_pr_review]
    # Skip threads from reviewers that auto-resolve (e.g. Devin, CodeRabbit)
    skipped = [t for t in stale if _reviewer_auto_resolves(t.reviewer)]
    stale = [t for t in stale if not _reviewer_auto_resolves(t.reviewer)]

    if not stale:
        return ResolveStaleResult(resolved_count=0, resolved_thread_ids=[], skipped_count=len(skipped))

    # Batch resolve using GraphQL aliases
    mutations = []
    for i, thread in enumerate(stale):
        mutations.append(f'  t{i}: resolveReviewThread(input: {{threadId: "{thread.thread_id}"}}) {{ thread {{ id isResolved }} }}')

    batch_mutation = "mutation {\n" + "\n".join(mutations) + "\n}"
    await call_sync_fn_in_threadpool(gh.graphql, batch_mutation, cwd=cwd)

    resolved_ids = [t.thread_id for t in stale]
    if ctx:
        await ctx.info(f"Resolved {len(resolved_ids)} stale threads on PR #{pr_number}")
    return ResolveStaleResult(resolved_count=len(resolved_ids), resolved_thread_ids=resolved_ids, skipped_count=len(skipped))


def reply_to_comment(
    pr_number: int,
    thread_id: str,
    body: str,
    repo: str | None = None,
    cwd: str | None = None,
) -> str:
    """Reply to a specific review thread.

    Note: GitHub's GraphQL API doesn't have a direct addPullRequestReviewThreadReply
    mutation in all contexts, so we use the REST API for this.

    Args:
        pr_number: PR number.
        thread_id: The thread ID to reply to (we need the comment ID from the thread).
        body: Reply text.
        repo: Repository in "owner/repo" format. Auto-detected if not provided.
        cwd: Working directory.

    Returns:
        Confirmation message.
    """
    if repo:
        owner, repo_name = repo.split("/", 1)
    else:
        owner, repo_name = gh.get_repo_info(cwd=cwd)

    # First, get the comment ID from the thread
    query = """
    query($threadId: ID!) {
      node(id: $threadId) {
        ... on PullRequestReviewThread {
          comments(first: 1) {
            nodes { databaseId }
          }
        }
      }
    }
    """
    result = gh.graphql(query, variables={"threadId": thread_id}, cwd=cwd)
    comment_id = result.get("data", {}).get("node", {}).get("comments", {}).get("nodes", [{}])[0].get("databaseId")

    if not comment_id:
        msg = f"Could not find comment ID for thread {thread_id}"
        raise gh.GhError(msg)

    # Use REST API to reply
    gh.rest(
        f"/repos/{owner}/{repo_name}/pulls/{pr_number}/comments/{comment_id}/replies",
        method="POST",
        body=body,
        cwd=cwd,
    )
    return f"Replied to thread {thread_id} on PR #{pr_number}"
