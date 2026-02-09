"""Per-reviewer configuration system.

Loads ``.codereviewbuddy.toml`` from the project root (walking up to ``.git``),
validates with Pydantic, and provides sensible defaults so zero-config still works.
"""

from __future__ import annotations

import logging
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)

CONFIG_FILENAME = ".codereviewbuddy.toml"


class Severity(StrEnum):
    """Comment severity levels, ordered from least to most critical."""

    INFO = "info"
    WARNING = "warning"
    FLAGGED = "flagged"
    BUG = "bug"


# -- Per-reviewer defaults (match current hardcoded adapter behavior) ----------

_REVIEWER_DEFAULTS: dict[str, dict[str, Any]] = {
    "devin": {
        "enabled": True,
        "auto_resolve_stale": False,  # Devin auto-resolves its own bug threads
        "resolve_levels": [Severity.INFO],  # Only allow resolving info-level
    },
    "unblocked": {
        "enabled": True,
        "auto_resolve_stale": True,  # We batch-resolve Unblocked's stale threads
        "resolve_levels": [Severity.INFO, Severity.WARNING, Severity.FLAGGED, Severity.BUG],
    },
    "coderabbit": {
        "enabled": True,
        "auto_resolve_stale": False,  # CodeRabbit handles its own resolution
        "resolve_levels": [],  # Don't resolve any CodeRabbit threads
    },
}


class ReviewerConfig(BaseModel):
    """Configuration for a single reviewer."""

    enabled: bool = Field(default=True, description="Whether this reviewer integration is active")
    auto_resolve_stale: bool = Field(
        default=True,
        description="Whether resolve_stale_comments touches this reviewer's threads",
    )
    resolve_levels: list[Severity] = Field(
        default_factory=lambda: list(Severity),
        description="Severity levels that are allowed to be resolved",
    )


class Config(BaseModel):
    """Top-level codereviewbuddy configuration."""

    reviewers: dict[str, ReviewerConfig] = Field(
        default_factory=dict,
        description="Per-reviewer configuration sections",
    )

    @model_validator(mode="after")
    def _apply_reviewer_defaults(self) -> Config:
        """Fill in missing reviewers with their hardcoded defaults.

        For partially-specified reviewers, merge unset fields from
        ``_REVIEWER_DEFAULTS`` so that e.g. ``[reviewers.devin]\\nenabled = false``
        still gets ``auto_resolve_stale=False`` (Devin's safe default) rather
        than the generic ``ReviewerConfig`` field default (``True``).
        """
        for name, defaults in _REVIEWER_DEFAULTS.items():
            if name not in self.reviewers:
                self.reviewers[name] = ReviewerConfig(**defaults)
            else:
                rc = self.reviewers[name]
                for field_name, default_value in defaults.items():
                    if field_name not in rc.model_fields_set:
                        setattr(rc, field_name, default_value)
        return self

    def get_reviewer(self, name: str) -> ReviewerConfig:
        """Get config for a reviewer, falling back to permissive defaults for unknown reviewers."""
        if name in self.reviewers:
            return self.reviewers[name]
        # Unknown reviewer: enabled, all levels resolvable, auto-resolve on
        return ReviewerConfig()

    def can_resolve(self, reviewer_name: str, severity: Severity) -> tuple[bool, str]:
        """Check if resolving a thread is allowed by config.

        Args:
            reviewer_name: Name of the reviewer that posted the thread.
            severity: Severity of the thread (from the adapter's ``classify_severity``).

        Returns:
            (allowed, reason) — if not allowed, reason explains why.
        """
        rc = self.get_reviewer(reviewer_name)
        if not rc.enabled:
            return False, f"Reviewer '{reviewer_name}' is disabled in config"
        if severity not in rc.resolve_levels:
            return False, (
                f"Config blocks resolving {severity}-level threads from {reviewer_name}. "
                f"Allowed levels: {[s.value for s in rc.resolve_levels]}"
            )
        return True, ""


def _find_config_file(start: Path) -> Path | None:
    """Walk up from *start* looking for ``.codereviewbuddy.toml``, stopping at ``.git`` root."""
    current = start.resolve()
    while True:
        candidate = current / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
        # Stop at filesystem root
        if current.parent == current:
            return None
        # Stop if we just checked a directory that contains .git
        if (current / ".git").exists():
            return None
        current = current.parent


def load_config(cwd: str | Path | None = None) -> Config:
    """Load configuration from ``.codereviewbuddy.toml``.

    Walks up from *cwd* (defaulting to the current directory) looking for the
    config file.  If not found, returns a ``Config`` with all defaults.

    Raises ``ValueError`` on invalid TOML or validation errors so the server
    can refuse to start with a broken config.
    """
    start = Path(cwd) if cwd else Path.cwd()
    config_path = _find_config_file(start)

    if config_path is None:
        logger.info("No %s found, using defaults", CONFIG_FILENAME)
        return Config()

    logger.info("Loading config from %s", config_path)
    try:
        raw = config_path.read_text(encoding="utf-8")
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        msg = f"Invalid TOML in {config_path}: {exc}"
        raise ValueError(msg) from exc

    try:
        return Config.model_validate(data)
    except Exception as exc:
        msg = f"Invalid config in {config_path}: {exc}"
        raise ValueError(msg) from exc


# -- Global config instance (set during server lifespan) -----------------------

_config: Config = Config()


def get_config() -> Config:
    """Return the active configuration."""
    return _config


def set_config(config: Config) -> None:
    """Set the active configuration (called during server startup)."""
    global _config
    _config = config


# -- Self-documenting template for ``codereviewbuddy init`` --------------------

DEFAULT_CONFIG_TEMPLATE = """\
# .codereviewbuddy.toml — Per-reviewer configuration for codereviewbuddy
# All settings are optional. Omitted values use sensible defaults.
# Place this file in your project root (next to .git/).
#
# Severity levels used by resolve_levels:
#   bug      — 🔴 critical issues, must fix before merge
#   flagged  — 🚩 likely needs a code change
#   warning  — 🟡 worth addressing but not blocking
#   info     — 📝 informational, no action required

[reviewers.devin]
# enabled = true                  # Set to false to ignore Devin comments entirely
# auto_resolve_stale = false      # Devin auto-resolves its own bug threads; we skip them
# resolve_levels = ["info"]       # Only allow resolving info-level threads from Devin

[reviewers.unblocked]
# enabled = true
# auto_resolve_stale = true       # We batch-resolve Unblocked's stale threads
# resolve_levels = ["info", "warning", "flagged", "bug"]  # All levels allowed

[reviewers.coderabbit]
# enabled = true
# auto_resolve_stale = false      # CodeRabbit handles its own resolution
# resolve_levels = []             # Don't resolve any CodeRabbit threads
"""
