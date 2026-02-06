"""MCP tools for managing PR review comments."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from codereviewbuddy import gh
from codereviewbuddy.models import CommentStatus, ResolveStaleResult, ReviewComment, ReviewThread
from codereviewbuddy.reviewers import identify_reviewer

if TYPE_CHECKING:
    from typing import Any

logger = logging.getLogger(__name__)

# GraphQL query to fetch all review threads for a PR
_THREADS_QUERY = """
query($owner: String!, $repo: String!, $pr: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      title
      url
      reviewThreads(first: 100) {
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

# GraphQL query to get the diff for staleness detection
_DIFF_QUERY = """
query($owner: String!, $repo: String!, $pr: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      files(first: 100) {
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


def _parse_threads(raw_threads: list[dict[str, Any]], pr_number: int, changed_files: set[str] | None = None) -> list[ReviewThread]:
    """Parse raw GraphQL thread nodes into ReviewThread models."""
    threads = []
    for node in raw_threads:
        comments_raw = node.get("comments", {}).get("nodes", [])
        if not comments_raw:
            continue

        first_comment = comments_raw[0]
        author = first_comment.get("author", {}).get("login", "unknown")
        file_path = first_comment.get("path")

        # Staleness: if the file has been modified since the review, it's stale
        is_stale = False
        if changed_files and file_path:
            is_stale = file_path in changed_files

        comments = [
            ReviewComment(
                author=c.get("author", {}).get("login", "unknown"),
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


def _get_changed_files(owner: str, repo: str, pr_number: int, cwd: str | None = None) -> set[str]:
    """Get the set of files changed in the latest push of a PR."""
    result = gh.graphql(_DIFF_QUERY, variables={"owner": owner, "repo": repo, "pr": pr_number}, cwd=cwd)
    files = result.get("data", {}).get("repository", {}).get("pullRequest", {}).get("files", {}).get("nodes", [])
    return {f["path"] for f in files if f.get("path")}


def list_review_comments(
    pr_number: int,
    repo: str | None = None,
    status: str | None = None,
    cwd: str | None = None,
) -> list[ReviewThread]:
    """List all review threads for a PR with reviewer identification and staleness detection.

    Args:
        pr_number: The PR number to fetch comments for.
        repo: Repository in "owner/repo" format. Auto-detected if not provided.
        status: Filter by "resolved" or "unresolved". Returns all if not set.
        cwd: Working directory for git operations.

    Returns:
        List of ReviewThread objects.
    """
    if repo:
        owner, repo_name = repo.split("/", 1)
    else:
        owner, repo_name = gh.get_repo_info(cwd=cwd)

    # Fetch threads and changed files in parallel-ish (sequential for now)
    result = gh.graphql(_THREADS_QUERY, variables={"owner": owner, "repo": repo_name, "pr": pr_number}, cwd=cwd)
    changed_files = _get_changed_files(owner, repo_name, pr_number, cwd=cwd)

    raw_threads = result.get("data", {}).get("repository", {}).get("pullRequest", {}).get("reviewThreads", {}).get("nodes", [])

    threads = _parse_threads(raw_threads, pr_number, changed_files)

    # Filter by status if requested
    if status:
        target = CommentStatus(status)
        threads = [t for t in threads if t.status == target]

    return threads


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


def resolve_stale_comments(
    pr_number: int,
    repo: str | None = None,
    cwd: str | None = None,
) -> ResolveStaleResult:
    """Bulk-resolve all unresolved threads on lines that changed since the review.

    Args:
        pr_number: PR number.
        repo: Repository in "owner/repo" format. Auto-detected if not provided.
        cwd: Working directory.

    Returns:
        Dict with "resolved_count" and "resolved_thread_ids".
    """
    threads = list_review_comments(pr_number, repo=repo, status="unresolved", cwd=cwd)
    stale = [t for t in threads if t.is_stale]

    if not stale:
        return {"resolved_count": 0, "resolved_thread_ids": []}

    # Batch resolve using GraphQL aliases
    mutations = []
    for i, thread in enumerate(stale):
        mutations.append(f'  t{i}: resolveReviewThread(input: {{threadId: "{thread.thread_id}"}}) {{ thread {{ id isResolved }} }}')

    batch_mutation = "mutation {\n" + "\n".join(mutations) + "\n}"
    gh.graphql(batch_mutation, cwd=cwd)

    resolved_ids = [t.thread_id for t in stale]
    logger.info("Resolved %d stale threads on PR #%d", len(resolved_ids), pr_number)
    return {"resolved_count": len(resolved_ids), "resolved_thread_ids": resolved_ids}


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
