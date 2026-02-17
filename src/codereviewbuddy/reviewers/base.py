"""Abstract base for reviewer adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codereviewbuddy.config import Severity


class ReviewerAdapter(ABC):
    """Base class for AI code reviewer adapters.

    Each adapter encodes the behavior quirks of a specific AI reviewer:
    how to identify its comments by author username, how it classifies
    severity, and whether it auto-resolves comments.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for this reviewer (e.g. 'unblocked')."""

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

    @property
    def default_auto_resolve_stale(self) -> bool:
        """Whether ``resolve_stale_comments`` should touch this reviewer's threads by default.

        ``True`` means *we* handle bulk-resolution (e.g. Unblocked).
        ``False`` means the reviewer resolves its own threads (e.g. Devin, CodeRabbit).

        This is the adapter's built-in default; users can override it via
        ``CRB_REVIEWERS`` → ``auto_resolve_stale``.
        """
        return True

    @property
    def default_resolve_levels(self) -> list[Severity]:
        """Severity levels that are allowed to be resolved by default.

        This is the adapter's built-in default; users can override it via
        ``CRB_REVIEWERS`` → ``resolve_levels``.
        """
        from codereviewbuddy.config import Severity  # noqa: PLC0415

        return list(Severity)

    def auto_resolves_thread(self, comment_body: str) -> bool:  # noqa: ARG002
        """Whether this reviewer will auto-resolve a specific thread.

        Override to make per-thread decisions based on comment content.
        Defaults to ``auto_resolves_comments`` (all-or-nothing).
        """
        return self.auto_resolves_comments

    @abstractmethod
    def identify(self, author: str) -> bool:
        """Return True if the given GitHub username belongs to this reviewer."""
