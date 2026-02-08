"""MCP tool for creating GitHub issues from review comments."""

from __future__ import annotations

import logging

from codereviewbuddy import gh
from codereviewbuddy.models import CreateIssueResult

logger = logging.getLogger(__name__)

_THREAD_QUERY = """
query($threadId: ID!) {
  node(id: $threadId) {
    ... on PullRequestReviewThread {
      comments(first: 1) {
        nodes {
          body
          path
          line
          author { login }
          url
        }
      }
    }
  }
}
"""


def create_issue_from_comment(
    pr_number: int,
    thread_id: str,
    title: str,
    labels: list[str] | None = None,
    repo: str | None = None,
    cwd: str | None = None,
) -> CreateIssueResult:
    """Create a GitHub issue from a review comment thread.

    Args:
        pr_number: PR number the comment belongs to.
        thread_id: GraphQL node ID (PRRT_...) of the review thread.
        title: Issue title.
        labels: Optional labels to apply (e.g. ["enhancement", "P2"]).
        repo: Repository in "owner/repo" format. Auto-detected if not provided.
        cwd: Working directory.

    Returns:
        Created issue number, URL, and title.
    """
    if repo:
        owner, repo_name = repo.split("/", 1)
    else:
        owner, repo_name = gh.get_repo_info(cwd=cwd)

    full_repo = f"{owner}/{repo_name}"

    # Fetch the thread content
    result = gh.graphql(_THREAD_QUERY, variables={"threadId": thread_id}, cwd=cwd)
    node = result.get("data", {}).get("node") or {}
    comment_nodes = node.get("comments", {}).get("nodes", [])

    if not comment_nodes:
        msg = f"Could not find comment content for thread {thread_id}"
        raise gh.GhError(msg)

    comment = comment_nodes[0]
    body_text = comment.get("body", "")
    file_path = comment.get("path")
    line = comment.get("line")
    author = (comment.get("author") or {}).get("login", "unknown")
    comment_url = comment.get("url", "")

    # Build issue body
    parts = [f"From review comment on PR #{pr_number}"]
    if comment_url:
        parts[0] = f"From [review comment]({comment_url}) on PR #{pr_number}"

    if file_path:
        location = f"`{file_path}`"
        if line:
            location += f" line {line}"
        parts.append(f"**Location:** {location}")

    parts.extend([
        f"**Reviewer:** {author}",
        "",
        f"> {body_text.replace(chr(10), chr(10) + '> ')}",
    ])

    issue_body = "\n\n".join(parts)

    # Create issue via gh CLI
    args = [
        "issue",
        "create",
        "--repo",
        full_repo,
        "--title",
        title,
        "--body",
        issue_body,
    ]
    if labels:
        for label in labels:
            args.extend(["--label", label])

    raw = gh.run_gh(*args, cwd=cwd)

    # Parse the issue URL from output (gh issue create prints the URL)
    issue_url = raw.strip()

    # Extract issue number from URL (e.g. https://github.com/owner/repo/issues/42)
    try:
        issue_number = int(issue_url.rstrip("/").split("/")[-1])
    except ValueError, IndexError:
        logger.warning("Could not parse issue number from: %s", issue_url)
        issue_number = 0

    return CreateIssueResult(
        issue_number=issue_number,
        issue_url=issue_url,
        title=title,
    )
