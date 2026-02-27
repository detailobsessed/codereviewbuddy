"""MCP tools for reviewing and updating PR descriptions."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from codereviewbuddy import github_api
from codereviewbuddy.config import get_config
from codereviewbuddy.models import PRDescriptionInfo, PRDescriptionReviewResult

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

_MIN_NON_BOILERPLATE_CHARS = 20
_BOILERPLATE_MATCH_THRESHOLD = 3
_MIN_DESCRIPTION_CHARS = 50


async def _fetch_pr_info(pr_number: int, repo: str | None = None, cwd: str | None = None) -> dict:
    """Fetch PR title, body, and URL via GitHub REST API."""
    if not repo:
        from fastmcp.utilities.async_utils import call_sync_fn_in_threadpool  # noqa: PLC0415

        from codereviewbuddy import gh  # noqa: PLC0415

        owner, repo_name = await call_sync_fn_in_threadpool(gh.get_repo_info, cwd=cwd)
    else:
        owner, repo_name = github_api.parse_repo(repo)
    pr = await github_api.rest(f"/repos/{owner}/{repo_name}/pulls/{pr_number}")
    return {
        "number": pr["number"],
        "title": pr["title"],
        "body": pr.get("body") or "",
        "url": pr["html_url"],
    }


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
    if len(stripped) < _MIN_NON_BOILERPLATE_CHARS:
        return True
    # Check for known boilerplate patterns
    matches = sum(1 for p in _BOILERPLATE_PATTERNS if re.search(p, body, re.IGNORECASE))
    return matches >= _BOILERPLATE_MATCH_THRESHOLD


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
    if len(body.strip()) < _MIN_DESCRIPTION_CHARS and has_body and not boilerplate:
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
    cwd: str | None = None,
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
        if ctx and total:
            await ctx.report_progress(i, total)
        try:
            data = await _fetch_pr_info(pr_number, repo=repo, cwd=cwd)
            descriptions.append(_analyze_pr(data))
        except Exception as exc:
            descriptions.append(
                PRDescriptionInfo(
                    pr_number=pr_number,
                    title="",
                    error=f"Failed to fetch PR #{pr_number}: {exc}",
                )
            )

    if ctx and total:
        await ctx.report_progress(total, total)
    if ctx:
        await ctx.info(f"Reviewed {total} PR description(s)")

    return PRDescriptionReviewResult(descriptions=descriptions)
