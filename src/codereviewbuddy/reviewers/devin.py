"""Devin reviewer adapter."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, override

from codereviewbuddy.reviewers.base import ReviewerAdapter

if TYPE_CHECKING:
    from codereviewbuddy.config import Severity


class DevinAdapter(ReviewerAdapter):
    """Adapter for the Devin AI reviewer.

    - Auto-resolves addressed comments.
    - Comments are posted by 'devin-ai-integration[bot]' or 'devin-ai'.
    """

    @property
    def name(self) -> str:
        return "devin"

    @property
    def auto_resolves_comments(self) -> bool:
        return True

    @property
    @override
    def default_auto_resolve_stale(self) -> bool:
        return False  # Devin auto-resolves its own bug threads

    @property
    @override
    def default_resolve_levels(self) -> list[Severity]:
        from codereviewbuddy.config import Severity  # noqa: PLC0415

        return [Severity.INFO]  # Only allow resolving info-level

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
