"""Devin reviewer adapter."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, override

from codereviewbuddy.reviewers.base import ReviewerAdapter

if TYPE_CHECKING:
    from codereviewbuddy.config import Severity


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

    # Devin's known emoji markers (verified from PR review comments).
    # Ordered most-critical-first so the first match wins.
    _EMOJI_SEVERITY: ClassVar[list[tuple[str, str]]] = [
        ("🔴", "bug"),
        ("🚩", "flagged"),
        ("🟡", "warning"),
        ("📝", "info"),
    ]

    def classify_severity(self, comment_body: str) -> Severity:
        """Classify using Devin's emoji markers: 🔴 bug, 🚩 flagged, 🟡 warning, 📝 info."""
        from codereviewbuddy.config import Severity  # noqa: PLC0415

        for emoji, level in self._EMOJI_SEVERITY:
            if emoji in comment_body:
                return Severity(level)
        return Severity.INFO

    @override
    def auto_resolves_thread(self, comment_body: str) -> bool:
        """Devin only auto-resolves bug/investigation threads, not info threads."""
        return "📝" not in comment_body

    @override
    def identify(self, author: str) -> bool:
        normalized = author.lower().strip()
        return "devin" in normalized

    @override
    def rereview_trigger(self, pr_number: int, owner: str, repo: str) -> list[str]:
        return []  # Devin auto-triggers on push
