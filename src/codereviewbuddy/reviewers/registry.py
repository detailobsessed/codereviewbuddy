"""Reviewer registry — lookup and identification helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codereviewbuddy.reviewers.base import ReviewerAdapter


def _build_registry() -> list[ReviewerAdapter]:
    """Instantiate all known reviewer adapters."""
    from codereviewbuddy.reviewers.coderabbit import CodeRabbitAdapter  # noqa: PLC0415
    from codereviewbuddy.reviewers.devin import DevinAdapter  # noqa: PLC0415
    from codereviewbuddy.reviewers.greptile import GreptileAdapter  # noqa: PLC0415
    from codereviewbuddy.reviewers.unblocked import UnblockedAdapter  # noqa: PLC0415

    return [
        UnblockedAdapter(),
        DevinAdapter(),
        CodeRabbitAdapter(),
        GreptileAdapter(),
    ]


REVIEWERS: list[ReviewerAdapter] = _build_registry()


def identify_reviewer(author: str) -> str:
    """Identify which reviewer posted a comment based on the author username.

    Returns:
        Reviewer name (e.g. "unblocked", "devin", "coderabbit") or "unknown".
    """
    for reviewer in REVIEWERS:
        if reviewer.identify(author):
            return reviewer.name
    return "unknown"


def get_reviewer(name: str) -> ReviewerAdapter | None:
    """Get a reviewer adapter by name."""
    for reviewer in REVIEWERS:
        if reviewer.name == name:
            return reviewer
    return None
