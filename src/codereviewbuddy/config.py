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
        # rereview_message intentionally omitted — None means "use adapter default"
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
    rereview_message: str | None = Field(
        default=None,
        min_length=1,
        description="Custom message to post when triggering a re-review (only for manual-trigger reviewers)",
    )


class PRDescriptionsConfig(BaseModel):
    """Configuration for PR description management tools."""

    enabled: bool = Field(default=True, description="Whether PR description tools are available")
    require_review: bool = Field(
        default=False,
        description="If true, return a preview instead of directly updating — user must approve changes",
    )


class Config(BaseModel):
    """Top-level codereviewbuddy configuration."""

    reviewers: dict[str, ReviewerConfig] = Field(
        default_factory=dict,
        description="Per-reviewer configuration sections",
    )
    pr_descriptions: PRDescriptionsConfig = Field(
        default_factory=PRDescriptionsConfig,
        description="PR description management settings",
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


# -- Template sections for ``codereviewbuddy config`` --------------------------

# Each section is a (header_pattern, text_block) pair. The header_pattern is
# used to check whether the section already exists in the user's config file.
# The text_block is what gets written for --init or appended for --update.

_TEMPLATE_HEADER = """\
# .codereviewbuddy.toml — Per-reviewer configuration for codereviewbuddy
# All settings are optional. Omitted values use sensible defaults.
# Place this file in your project root (next to .git/).
#
# Severity levels used by resolve_levels:
#   bug      — 🔴 critical issues, must fix before merge
#   flagged  — 🚩 likely needs a code change
#   warning  — 🟡 worth addressing but not blocking
#   info     — 📝 informational, no action required
"""

_TEMPLATE_SECTIONS: list[tuple[str, str]] = [
    (
        "[reviewers.devin]",
        """\
[reviewers.devin]
# enabled = true                  # Set to false to ignore Devin comments entirely
# auto_resolve_stale = false      # Devin auto-resolves its own bug threads; we skip them
# resolve_levels = ["info"]       # Only allow resolving info-level threads from Devin
""",
    ),
    (
        "[reviewers.unblocked]",
        """\
[reviewers.unblocked]
# enabled = true
# auto_resolve_stale = true       # We batch-resolve Unblocked's stale threads
# resolve_levels = ["info", "warning", "flagged", "bug"]  # All levels allowed
# rereview_message = "@unblocked please re-review"  # Message posted to trigger re-review
""",
    ),
    (
        "[reviewers.coderabbit]",
        """\
[reviewers.coderabbit]
# enabled = true
# auto_resolve_stale = false      # CodeRabbit handles its own resolution
# resolve_levels = []             # Don't resolve any CodeRabbit threads
""",
    ),
    (
        "[pr_descriptions]",
        """\
[pr_descriptions]
# enabled = true                  # Set to false to disable PR description tools entirely
# require_review = false          # Set to true to require user approval before updating descriptions
""",
    ),
]

DEFAULT_CONFIG_TEMPLATE = _TEMPLATE_HEADER + "\n".join(block for _, block in _TEMPLATE_SECTIONS)


def init_config(cwd: Path | None = None) -> Path:
    """Create a new ``.codereviewbuddy.toml`` in the given directory.

    Raises ``SystemExit(1)`` if the file already exists.

    Returns:
        Path to the created file.
    """
    target = (cwd or Path.cwd()) / CONFIG_FILENAME
    if target.exists():
        print(f"Error: {CONFIG_FILENAME} already exists in {target.parent}")  # noqa: T201
        print("Hint: use 'codereviewbuddy config --update' to add new sections")  # noqa: T201
        raise SystemExit(1)
    target.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
    print(f"Created {target}")  # noqa: T201
    return target


def update_config(cwd: Path | None = None) -> tuple[Path, list[str]]:
    """Append missing sections to an existing ``.codereviewbuddy.toml``.

    Reads the current config file, checks which template sections are
    missing, and appends them. Does NOT modify existing values.

    Raises ``SystemExit(1)`` if the config file doesn't exist.

    Returns:
        Tuple of (config path, list of added section headers).
    """
    target = (cwd or Path.cwd()) / CONFIG_FILENAME
    if not target.exists():
        print(f"Error: {CONFIG_FILENAME} not found in {target.parent}")  # noqa: T201
        print("Hint: use 'codereviewbuddy config --init' to create one")  # noqa: T201
        raise SystemExit(1)

    existing = target.read_text(encoding="utf-8")
    added: list[str] = []

    for header, _block in _TEMPLATE_SECTIONS:
        if header not in existing:
            added.append(header)

    if not added:
        print(f"{CONFIG_FILENAME} is up to date — no new sections to add")  # noqa: T201
        return target, added

    # Ensure file ends with a newline before appending
    appendix = "" if existing.endswith("\n") else "\n"
    appendix += "\n# --- New sections added by 'codereviewbuddy config --update' ---\n\n"
    for header, block in _TEMPLATE_SECTIONS:
        if header in added:
            appendix += block + "\n"

    target.write_text(existing + appendix, encoding="utf-8")
    print(f"Updated {target} — added {len(added)} section(s):")  # noqa: T201
    for h in added:
        print(f"  + {h}")  # noqa: T201

    return target, added
