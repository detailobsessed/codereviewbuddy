"""Abstract base for reviewer adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codereviewbuddy.config import ReviewerConfig, Severity


class ReviewerAdapter(ABC):
    """Base class for AI code reviewer adapters.

    Each adapter encodes the behavior quirks of a specific AI reviewer:
    whether it needs manual re-review triggers, whether it auto-resolves
    comments, and how to identify its comments by author username.

    Call :meth:`configure` to apply per-reviewer config overrides from
    ``CRB_*`` env vars.  Without configuration, adapters use their
    hardcoded defaults.
    """

    _config: ReviewerConfig | None = None

    def configure(self, config: ReviewerConfig) -> None:
        """Apply per-reviewer configuration overrides."""
        self._config = config

    @property
    def enabled(self) -> bool:
        """Whether this reviewer integration is active."""
        if self._config is not None:
            return self._config.enabled
        return True

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

    def classify_severity(self, comment_body: str) -> Severity:  # noqa: ARG002, PLR6301
        """Classify the severity of a comment based on reviewer-specific markers.

        Each reviewer uses different formats (emojis, labels, etc.) to indicate
        severity.  Override in subclasses that have known formats.  The default
        returns ``info`` — the safest assumption for unknown formats.
        """
        from codereviewbuddy.config import Severity  # noqa: PLC0415

        return Severity.INFO

    def auto_resolves_thread(self, comment_body: str) -> bool:  # noqa: ARG002
        """Whether this reviewer will auto-resolve a specific thread.

        Override to make per-thread decisions based on comment content.
        Defaults to ``auto_resolves_comments`` (all-or-nothing).
        """
        return self.auto_resolves_comments

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
