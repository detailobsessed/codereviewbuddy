"""Abstract base for reviewer adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod


class ReviewerAdapter(ABC):
    """Base class for AI code reviewer adapters.

    Each adapter encodes the behavior quirks of a specific AI reviewer:
    whether it needs manual re-review triggers, whether it auto-resolves
    comments, and how to identify its comments by author username.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for this reviewer (e.g. 'unblocked')."""

    @property
    @abstractmethod
    def needs_manual_rereview(self) -> bool:
        """Whether this reviewer requires a manual re-review trigger after pushing."""

    @property
    @abstractmethod
    def auto_resolves_comments(self) -> bool:
        """Whether this reviewer auto-resolves addressed comments on new pushes."""

    @abstractmethod
    def identify(self, author: str) -> bool:
        """Return True if the given GitHub username belongs to this reviewer."""

    @abstractmethod
    def rereview_trigger(self, pr_number: int, owner: str, repo: str) -> list[str]:
        """Return the gh CLI args to trigger a re-review for this reviewer.

        Args:
            pr_number: PR number to re-review.
            owner: Repository owner.
            repo: Repository name.

        Returns:
            List of gh CLI arguments (e.g. ["pr", "comment", "42", ...]).
            Return an empty list if re-review is automatic.
        """
