"""CodeRabbit reviewer adapter."""

from __future__ import annotations

from typing import TYPE_CHECKING, override

from codereviewbuddy.reviewers.base import ReviewerAdapter

if TYPE_CHECKING:
    from codereviewbuddy.config import Severity


class CodeRabbitAdapter(ReviewerAdapter):
    """Adapter for the CodeRabbit AI reviewer.

    - Auto-resolves addressed comments.
    - Comments are posted by 'coderabbitai[bot]' or 'coderabbit'.
    """

    @property
    def name(self) -> str:
        return "coderabbit"

    @property
    def auto_resolves_comments(self) -> bool:
        return True

    @property
    @override
    def default_auto_resolve_stale(self) -> bool:
        return False  # CodeRabbit handles its own resolution

    @property
    @override
    def default_resolve_levels(self) -> list[Severity]:
        return []  # Don't resolve any CodeRabbit threads

    @override
    def identify(self, author: str) -> bool:
        normalized = author.lower().strip()
        return "coderabbit" in normalized
