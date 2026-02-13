"""CodeRabbit reviewer adapter."""

from __future__ import annotations

from typing import override

from codereviewbuddy.reviewers.base import ReviewerAdapter


class CodeRabbitAdapter(ReviewerAdapter):
    """Adapter for the CodeRabbit AI reviewer.

    - Auto-triggers re-review on new pushes.
    - Auto-resolves addressed comments.
    - Comments are posted by 'coderabbitai[bot]' or 'coderabbit'.
    """

    @property
    def name(self) -> str:
        return "coderabbit"

    @property
    def needs_manual_rereview(self) -> bool:
        return False

    @property
    def auto_resolves_comments(self) -> bool:
        return True

    @override
    def identify(self, author: str) -> bool:
        normalized = author.lower().strip()
        return "coderabbit" in normalized

    @override
    def rereview_trigger(self, pr_number: int, owner: str, repo: str) -> list[str]:
        return []  # CodeRabbit auto-triggers on push
