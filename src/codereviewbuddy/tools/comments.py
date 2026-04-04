"""MCP tools for managing PR review comments."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from fastmcp.utilities.async_utils import call_sync_fn_in_threadpool

from codereviewbuddy import gh, github_api
from codereviewbuddy.config import get_config
from codereviewbuddy.models import (
    CommentStatus,
    ReviewComment,
    ReviewThread,
    TriageItem,
    TriageResult,
)

logger = logging.getLogger(__name__)

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
              url
            }
          }
        }
      }
    }
  }
}
"""


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
                reviewer=author,
                comments=comments,
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


async def _fetch_raw_threads(
    owner: str,
    repo_name: str,
    pr_number: int,
    cwd: str | None,  # noqa: ARG001
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

        result = await github_api.graphql(_THREADS_QUERY, variables=variables)
        pr_data = result.get("data", {}).get("repository", {}).get("pullRequest") or {}
        threads_data = pr_data.get("reviewThreads", {})
        raw_threads.extend(threads_data.get("nodes", []))

        page_info = threads_data.get("pageInfo", {})
        if page_info.get("hasNextPage") and page_info.get("endCursor"):
            cursor = page_info["endCursor"]
        else:
            break

    return raw_threads


async def _get_inline_threads(
    owner: str,
    repo_name: str,
    pr_number: int,
    cwd: str | None = None,
    ctx: Context | None = None,
) -> list[ReviewThread]:
    """Fetch only inline review threads (PRRT_) for a PR."""
    raw = await _fetch_raw_threads(owner, repo_name, pr_number, cwd, ctx)
    return _parse_threads(raw, pr_number)


# GraphQL query to fetch a single thread/review/comment by node ID
_THREAD_BY_ID_QUERY = """
query($id: ID!) {
  node(id: $id) {
    ... on PullRequestReviewThread {
      __typename
      id
      isResolved
      pullRequest { number }
      comments(first: 50) {
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
    ... on PullRequestReview {
      __typename
      id
      state
      body
      author { login }
      submittedAt
      url
      pullRequest { number }
    }
    ... on IssueComment {
      __typename
      id
      body
      author { login }
      createdAt
      url
      issue { number }
    }
  }
}
"""


def _node_to_review_thread(node: dict[str, Any], thread_id: str) -> ReviewThread:
    """Convert a GraphQL node response into a ReviewThread model."""
    typename = node.get("__typename", "")

    pr_number = (node.get("pullRequest") or {}).get("number", 0)

    if typename == "PullRequestReviewThread":
        # Inline review thread — reuse existing parser
        threads = _parse_threads([node], pr_number=pr_number)
        if not threads:
            msg = f"Thread {thread_id} has no comments."
            raise gh.GhError(msg)
        return threads[0]

    if typename == "PullRequestReview":
        login = (node.get("author") or {}).get("login", "unknown")
        raw_body = (node.get("body") or "").strip()
        state = node.get("state", "COMMENTED")
        status = _REVIEW_STATE_MAP.get(state, CommentStatus.UNRESOLVED)
        return ReviewThread(
            thread_id=thread_id,
            pr_number=pr_number,
            status=status,
            file=None,
            line=None,
            reviewer=login,
            comments=[
                ReviewComment(
                    author=login,
                    body=_strip_comment_body(raw_body) if raw_body else "",
                    created_at=node.get("submittedAt"),
                    url=node.get("url", ""),
                ),
            ],
            is_pr_review=True,
        )

    if typename == "IssueComment":
        login = (node.get("author") or {}).get("login", "unknown")
        raw_body = (node.get("body") or "").strip()
        return ReviewThread(
            thread_id=thread_id,
            pr_number=(node.get("issue") or {}).get("number", 0),
            status=CommentStatus.UNRESOLVED,
            file=None,
            line=None,
            reviewer=login,
            comments=[
                ReviewComment(
                    author=login,
                    body=_strip_comment_body(raw_body) if raw_body else "",
                    created_at=node.get("createdAt"),
                    url=node.get("url", ""),
                ),
            ],
            is_pr_review=True,
        )

    msg = f"Unexpected node type {typename!r} for thread {thread_id}."
    raise gh.GhError(msg)


async def get_thread(thread_id: str) -> ReviewThread:
    """Fetch full details for a single review thread by its GraphQL node ID.

    Supports all thread types: inline review threads (PRRT_), PR-level reviews
    (PRR_), and issue comments (IC_). Uses GitHub's ``node(id:)`` interface.

    Args:
        thread_id: The GraphQL node ID to fetch.

    Returns:
        Full ReviewThread with all comments.
    """
    result = await github_api.graphql(_THREAD_BY_ID_QUERY, variables={"id": thread_id})

    node = result.get("data", {}).get("node")
    if not node:
        msg = f"Thread {thread_id} not found or not accessible."
        raise gh.GhError(msg)

    return _node_to_review_thread(node, thread_id)


async def reply_to_comment(
    pr_number: int | None,
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
    # PRRT_ threads use GraphQL with only the thread ID — no repo or pr_number needed
    if thread_id.startswith("PRRT_"):
        return await _reply_to_review_thread(pr_number, thread_id, body, cwd=cwd)

    # IC_ and PRR_ paths need owner/repo + pr_number for the issues comments API
    if thread_id.startswith(("IC_", "PRR_")):
        if pr_number is None:
            msg = "pr_number is required for IC_ and PRR_ thread replies"
            raise ValueError(msg)
        if repo:
            owner, repo_name = github_api.parse_repo(repo)
        else:
            owner, repo_name = await call_sync_fn_in_threadpool(gh.get_repo_info, cwd=cwd)
        kind = "bot comment" if thread_id.startswith("IC_") else "PR-level review"
        return await _reply_to_pr_comment(pr_number, owner, repo_name, body, kind=kind, cwd=cwd)

    msg = f"Unsupported thread ID prefix: {thread_id!r}. Expected PRRT_, IC_, or PRR_."
    raise gh.GhError(msg)


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


async def _reply_to_review_thread(
    pr_number: int | None,
    thread_id: str,
    body: str,
    cwd: str | None = None,  # noqa: ARG001
) -> str:
    """Reply to an inline review thread (PRRT_ ID) via GraphQL mutation."""
    await github_api.graphql(
        _REPLY_TO_THREAD_MUTATION,
        variables={"threadId": thread_id, "body": body},
    )
    pr_suffix = f" on PR #{pr_number}" if pr_number is not None else ""
    return f"Replied to thread {thread_id}{pr_suffix}"


async def _reply_to_pr_comment(  # noqa: PLR0913, PLR0917
    pr_number: int,
    owner: str,
    repo_name: str,
    body: str,
    kind: str = "PR-level review",
    cwd: str | None = None,  # noqa: ARG001
) -> str:
    """Reply to a PR-level review or bot comment by posting an issue comment."""
    await github_api.rest(
        f"/repos/{owner}/{repo_name}/issues/{pr_number}/comments",
        method="POST",
        body=body,
    )
    return f"Replied to {kind} on PR #{pr_number}"


# ---------------------------------------------------------------------------
# Triage — actionable threads only (#96)
# ---------------------------------------------------------------------------

_BOLD_TITLE_RE = re.compile(r"\*\*(?:Bug|Info|Warning|Flagged)?:?\s*(.+?)\*\*", re.IGNORECASE)


def _extract_title(body: str) -> str:
    """Extract a short title from the first bold text in a comment."""
    match = _BOLD_TITLE_RE.search(body)
    return match.group(1).strip() if match else ""


_FIX_PATTERN = re.compile(r"(\U0001f534|\U0001f6a9|bug|critical|breaking|must fix|security)", re.IGNORECASE)
_ACKNOWLEDGE_PATTERN = re.compile(r"(\U0001f4dd|\U0001f7e1|info|note|nit|style|nitpick|minor|consider)", re.IGNORECASE)


def _classify_action(thread: ReviewThread) -> str:
    """Classify the suggested action for a triage item based on comment content.

    Returns:
        "fix" — clear code change needed (bug/critical markers).
        "acknowledge" — informational, just reply (info/nit/style markers).
        "ambiguous" — unclear intent, needs user input.
    """
    body = thread.comments[0].body if thread.comments else ""
    if _FIX_PATTERN.search(body):
        return "fix"
    if _ACKNOWLEDGE_PATTERN.search(body):
        return "acknowledge"
    return "ambiguous"


def _has_owner_reply(thread: ReviewThread, owner_logins: frozenset[str]) -> bool:
    """Check if any comment in the thread is from the repo owner / agent."""
    return any(c.author in owner_logins for c in thread.comments)


def _thread_to_triage_item(thread: ReviewThread) -> TriageItem:
    """Convert a ReviewThread into a compact TriageItem."""
    first = thread.comments[0] if thread.comments else None
    body = first.body if first else ""
    return TriageItem(
        thread_id=thread.thread_id,
        pr_number=thread.pr_number,
        file=thread.file,
        line=thread.line,
        reviewer=thread.reviewer,
        title=_extract_title(body),
        comment_url=first.url if first else "",
        action=_classify_action(thread),
    )


def _build_triage_hints(
    all_items: list[TriageItem],
) -> tuple[list[str], str]:
    """Build next_steps and message for a TriageResult."""
    if not all_items:
        return [], "No actionable threads — all threads have owner replies or are resolved."
    n = len(all_items)
    hint = f"Call get_thread(thread_id) on the {n} thread(s), fix what needs fixing, and reply with reply_to_comment()."
    return [hint], ""


async def triage_review_comments(
    pr_numbers: list[int],
    repo: str | None = None,
    owner_logins: list[str] | None = None,
    cwd: str | None = None,
    ctx: Context | None = None,
) -> TriageResult:
    """Return only unresolved inline review threads that need attention — no noise, no full bodies.

    Fetches threads and filters to unresolved inline threads (PRRT_) without owner
    replies. PR-level reviews (PRR_) and bot issue comments (IC_) are excluded as
    non-actionable. Use ``get_thread`` to fetch full details for individual threads.

    Args:
        pr_numbers: PR numbers to triage.
        repo: Repository in "owner/repo" format. Auto-detected if not provided.
        owner_logins: GitHub usernames considered "ours" (agent + human).
            Defaults to ``CRB_OWNER_LOGINS`` config value if not provided.
        cwd: Working directory for git operations.
        ctx: FastMCP context for progress reporting.

    Returns:
        TriageResult with unresolved threads needing action.
    """
    configured_owners = get_config().owner_logins
    resolved = owner_logins if owner_logins is not None else configured_owners
    if not resolved:
        logger.warning("No owner_logins configured — owner-reply filtering is disabled. Set CRB_OWNER_LOGINS to enable.")
    owners = frozenset(resolved)
    items: list[TriageItem] = []

    if repo:
        owner, repo_name = github_api.parse_repo(repo)
    else:
        owner, repo_name = await call_sync_fn_in_threadpool(gh.get_repo_info, cwd=cwd)

    total = len(pr_numbers)
    for i, pr_number in enumerate(pr_numbers):
        if ctx and total:
            await ctx.report_progress(i, total)

        threads = await _get_inline_threads(owner, repo_name, pr_number, cwd=cwd, ctx=ctx)

        for thread in threads:
            if thread.status != CommentStatus.UNRESOLVED:
                continue
            if _has_owner_reply(thread, owners):
                continue
            items.append(_thread_to_triage_item(thread))

    if ctx and total:
        await ctx.report_progress(total, total)

    next_steps, message = _build_triage_hints(items)

    return TriageResult(
        items=items,
        total=len(items),
        next_steps=next_steps,
        message=message,
    )
