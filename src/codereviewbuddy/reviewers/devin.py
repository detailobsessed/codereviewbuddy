"""Devin reviewer adapter."""

from __future__ import annotations

from codereviewbuddy.reviewers.base import ReviewerAdapter


class DevinAdapter(ReviewerAdapter):
    """Adapter for the Devin AI reviewer.

    - Auto-triggers re-review on new pushes.
    - Auto-resolves addressed comments.
    - Comments are posted by 'devin-ai-integration[bot]' or 'devin-ai'.
    """

    @property
    def name(self) -> str:
        return "devin"

    @property
    def needs_manual_rereview(self) -> bool:
        return False

    @property
    def auto_resolves_comments(self) -> bool:
        return True

    def identify(self, author: str) -> bool:
        normalized = author.lower().strip()
        return "devin" in normalized

    def rereview_trigger(self, pr_number: int, owner: str, repo: str) -> list[str]:  # noqa: ARG002
        return []  # Devin auto-triggers on push
