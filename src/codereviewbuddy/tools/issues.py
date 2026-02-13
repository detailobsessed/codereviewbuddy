"""MCP tool for creating GitHub issues from review comments."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from codereviewbuddy import gh
from codereviewbuddy.models import CreateIssueResult

if TYPE_CHECKING:
    from typing import Any

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


def _fetch_thread_comment(thread_id: str, cwd: str | None = None) -> dict[str, Any]:
    """Fetch the first comment from a review thread, raising on failure."""
    result = gh.graphql(_THREAD_QUERY, variables={"threadId": thread_id}, cwd=cwd)
    node = result.get("data", {}).get("node") or {}
    comment_nodes = node.get("comments", {}).get("nodes", [])
    if not comment_nodes:
        msg = f"Could not find comment content for thread {thread_id}"
        raise gh.GhError(msg)
    return comment_nodes[0]


def _build_issue_body(comment: dict[str, Any], pr_number: int) -> tuple[str, str]:
    """Build the markdown body for a GitHub issue from a review comment.

    Returns:
        (issue_body, author)
    """
    body_text = comment.get("body", "")
    file_path = comment.get("path")
    line = comment.get("line")
    author = (comment.get("author") or {}).get("login", "unknown")
    comment_url = comment.get("url", "")

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

    return "\n\n".join(parts), author


def _parse_issue_number(issue_url: str) -> int:
    """Extract issue number from a GitHub issue URL."""
    try:
        return int(issue_url.rstrip("/").split("/")[-1])
    except ValueError, IndexError:
        logger.warning("Could not parse issue number from: %s", issue_url)
        return 0


def create_issue_from_comment(  # noqa: PLR0913, PLR0917
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

    comment = _fetch_thread_comment(thread_id, cwd=cwd)
    issue_body, _author = _build_issue_body(comment, pr_number)

    full_repo = f"{owner}/{repo_name}"
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

    issue_url = gh.run_gh(*args, cwd=cwd).strip()

    return CreateIssueResult(
        issue_number=_parse_issue_number(issue_url),
        issue_url=issue_url,
        title=title,
    )
