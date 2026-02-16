"""Greptile reviewer adapter."""

from __future__ import annotations

from typing import override

from codereviewbuddy.reviewers.base import ReviewerAdapter


class GreptileAdapter(ReviewerAdapter):
    """Adapter for the Greptile AI reviewer.

    - Does NOT auto-resolve comments.
    - Comments are posted by 'greptile-apps' (inline) or 'greptile-apps[bot]' (PR summary).
    """

    @property
    def name(self) -> str:
        return "greptile"

    @property
    def auto_resolves_comments(self) -> bool:
        return False

    @override
    def identify(self, author: str) -> bool:
        normalized = author.lower().strip()
        return "greptile" in normalized
