"""MCP tool for triggering re-reviews."""

from __future__ import annotations

import logging

from codereviewbuddy import gh
from codereviewbuddy.reviewers import REVIEWERS, get_reviewer

logger = logging.getLogger(__name__)


def request_rereview(
    pr_number: int,
    reviewer: str | None = None,
    repo: str | None = None,
    cwd: str | None = None,
) -> dict[str, list[str] | list[str]]:
    """Trigger a re-review for AI reviewers on a PR.

    For reviewers that need manual triggering (e.g. Unblocked), posts a comment.
    For reviewers that auto-trigger on push (e.g. Devin, CodeRabbit), reports that
    no action is needed.

    Args:
        pr_number: PR number.
        reviewer: Specific reviewer name to re-review (e.g. "unblocked").
                  If not provided, triggers all reviewers that need manual re-review.
        repo: Repository in "owner/repo" format. Auto-detected if not provided.
        cwd: Working directory.

    Returns:
        Dict with "triggered" (list of reviewers triggered) and
        "auto_triggers" (list of reviewers that auto-trigger).
    """
    if repo:
        owner, repo_name = repo.split("/", 1)
    else:
        owner, repo_name = gh.get_repo_info(cwd=cwd)

    triggered: list[str] = []
    auto_triggers: list[str] = []

    if reviewer:
        # Trigger a specific reviewer
        adapter = get_reviewer(reviewer)
        if not adapter:
            msg = f"Unknown reviewer: {reviewer}. Known reviewers: {', '.join(r.name for r in REVIEWERS)}"
            raise ValueError(msg)

        if adapter.needs_manual_rereview:
            args = adapter.rereview_trigger(pr_number, owner, repo_name)
            if args:
                gh.run_gh(*args, cwd=cwd)
                triggered.append(adapter.name)
                logger.info("Triggered re-review from %s on PR #%d", adapter.name, pr_number)
        else:
            auto_triggers.append(adapter.name)
    else:
        # Trigger all reviewers that need manual re-review
        for adapter in REVIEWERS:
            if adapter.needs_manual_rereview:
                args = adapter.rereview_trigger(pr_number, owner, repo_name)
                if args:
                    gh.run_gh(*args, cwd=cwd)
                    triggered.append(adapter.name)
                    logger.info("Triggered re-review from %s on PR #%d", adapter.name, pr_number)
            else:
                auto_triggers.append(adapter.name)

    return {"triggered": triggered, "auto_triggers": auto_triggers}
