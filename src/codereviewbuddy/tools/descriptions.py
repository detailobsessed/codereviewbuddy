"""MCP tools for reviewing and updating PR descriptions."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from fastmcp.utilities.async_utils import call_sync_fn_in_threadpool

from codereviewbuddy import gh
from codereviewbuddy.config import get_config
from codereviewbuddy.models import PRDescriptionInfo, PRDescriptionReviewResult, UpdatePRDescriptionResult

if TYPE_CHECKING:
    from fastmcp.server.context import Context

# Common boilerplate patterns found in PR description templates
_BOILERPLATE_PATTERNS = [
    r"<!-- Brief description",
    r"<!-- Link related issues",
    r"\[[ x]?\]\s*Tests added",
    r"\[[ x]?\]\s*Documentation updated",
    r"\[[ x]?\]\s*Commit messages follow",
    r"## Description\s*\n\s*\n\s*##",  # Empty description section
    r"## Checklist",
]

# Pattern for issue references: #123, org/repo#123
_ISSUE_REF_PATTERN = re.compile(
    r"(?:(?:closes?|fixes?|resolves?)\s+)?(?:[\w.-]+/[\w.-]+)?#(\d+)",
    re.IGNORECASE,
)


def _fetch_pr_info(pr_number: int, repo: str | None = None, cwd: str | None = None) -> dict:
    """Fetch PR title, body, and URL via gh CLI."""
    args = ["pr", "view", str(pr_number), "--json", "number,title,body,url"]
    if repo:
        args.extend(["--repo", repo])
    import json

    raw = gh.run_gh(*args, cwd=cwd)
    return json.loads(raw)


def _is_boilerplate(body: str) -> bool:
    """Check if the body is mostly template boilerplate."""
    if not body.strip():
        return True
    # Strip HTML comments and checklist items, see what's left
    stripped = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    stripped = re.sub(r"- \[[ x]?\].*", "", stripped)
    stripped = re.sub(r"^#{1,6}\s+.*", "", stripped, flags=re.MULTILINE)
    stripped = stripped.strip()
    # If very little remains after stripping, it's boilerplate
    if len(stripped) < 20:
        return True
    # Check for known boilerplate patterns
    matches = sum(1 for p in _BOILERPLATE_PATTERNS if re.search(p, body, re.IGNORECASE))
    return matches >= 3


def _analyze_pr(data: dict) -> PRDescriptionInfo:
    """Analyze a PR's description quality."""
    body = data.get("body", "") or ""
    title = data.get("title", "")
    pr_number = data.get("number", 0)
    url = data.get("url", "")

    has_body = bool(body.strip())
    boilerplate = _is_boilerplate(body) if has_body else False
    body_no_comments = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    issue_refs = _ISSUE_REF_PATTERN.findall(body_no_comments)

    missing: list[str] = []
    if not has_body:
        missing.append("empty body")
    elif boilerplate:
        missing.append("body is template boilerplate only")
    if not issue_refs:
        missing.append("no linked issues")
    if len(body.strip()) < 50 and has_body and not boilerplate:
        missing.append("description is very short")

    return PRDescriptionInfo(
        pr_number=pr_number,
        title=title,
        body=body,
        url=url,
        has_body=has_body,
        is_boilerplate=boilerplate,
        linked_issues=[f"#{ref}" for ref in issue_refs],
        missing_elements=missing,
    )


async def review_pr_descriptions(
    pr_numbers: list[int],
    repo: str | None = None,
    ctx: Context | None = None,
) -> PRDescriptionReviewResult:
    """Fetch and analyze PR descriptions for quality issues.

    Args:
        pr_numbers: List of PR numbers to review.
        repo: Repository in "owner/repo" format. Auto-detected if not provided.
        ctx: FastMCP context for progress reporting.

    Returns:
        Analysis results for each PR's description.
    """
    config = get_config()
    if not config.pr_descriptions.enabled:
        return PRDescriptionReviewResult(error="PR description tools are disabled in config")

    descriptions: list[PRDescriptionInfo] = []
    total = len(pr_numbers)

    for i, pr_number in enumerate(pr_numbers):
        if ctx:
            await ctx.report_progress(i, total)
        data = await call_sync_fn_in_threadpool(_fetch_pr_info, pr_number, repo=repo)
        descriptions.append(_analyze_pr(data))

    if ctx:
        await ctx.report_progress(total, total)
        await ctx.info(f"Reviewed {total} PR description(s)")

    return PRDescriptionReviewResult(descriptions=descriptions)


async def update_pr_description(
    pr_number: int,
    body: str,
    repo: str | None = None,
    ctx: Context | None = None,
) -> UpdatePRDescriptionResult:
    """Update a PR's description.

    Respects config settings:
    - If ``pr_descriptions.enabled`` is false, returns an error.
    - If ``pr_descriptions.require_review`` is true, returns a preview
      instead of applying the update. The agent should present the
      preview to the user for approval.

    Args:
        pr_number: PR number to update.
        body: New description body.
        repo: Repository in "owner/repo" format. Auto-detected if not provided.
        ctx: FastMCP context for logging.

    Returns:
        Update result with status and optional preview.
    """
    config = get_config()
    if not config.pr_descriptions.enabled:
        return UpdatePRDescriptionResult(
            pr_number=pr_number,
            error="PR description tools are disabled in config",
        )

    if config.pr_descriptions.require_review:
        if ctx:
            await ctx.info(f"PR #{pr_number}: require_review is on \u2014 returning preview for user approval")
        return UpdatePRDescriptionResult(
            pr_number=pr_number,
            updated=False,
            requires_review=True,
            preview=body,
        )

    # Apply the update
    args = ["pr", "edit", str(pr_number), "--body", body]
    if repo:
        args.extend(["--repo", repo])
    await call_sync_fn_in_threadpool(gh.run_gh, *args)

    if ctx:
        await ctx.info(f"Updated description for PR #{pr_number}")

    return UpdatePRDescriptionResult(
        pr_number=pr_number,
        updated=True,
    )
