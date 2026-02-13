"""Unblocked reviewer adapter."""

from __future__ import annotations

from typing import override

from codereviewbuddy.reviewers.base import ReviewerAdapter


class UnblockedAdapter(ReviewerAdapter):
    """Adapter for the Unblocked AI reviewer.

    - Requires manual re-review trigger via PR comment.
    - Does NOT auto-resolve comments.
    - Comments are posted by 'unblocked[bot]' or 'unblocked-bot'.
    """

    @property
    def name(self) -> str:
        return "unblocked"

    @property
    def needs_manual_rereview(self) -> bool:
        return True

    @property
    def auto_resolves_comments(self) -> bool:
        return False

    @override
    def identify(self, author: str) -> bool:
        normalized = author.lower().strip()
        return normalized in {"unblocked[bot]", "unblocked-bot", "unblocked"}

    @override
    def rereview_trigger(self, pr_number: int, owner: str, repo: str) -> list[str]:
        message = "@unblocked please re-review"
        if self._config and self._config.rereview_message is not None:
            message = self._config.rereview_message
        return [
            "pr",
            "comment",
            str(pr_number),
            "--repo",
            f"{owner}/{repo}",
            "--body",
            message,
        ]
