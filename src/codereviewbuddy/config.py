"""Per-reviewer configuration system.

All configuration is via ``CRB_*`` environment variables, set in MCP client config.
Zero-config still works — all settings have sensible defaults.

Uses ``pydantic-settings`` ``BaseSettings`` (same pattern as FastMCP's own settings)
so env vars are read automatically with the ``CRB_`` prefix and ``__`` nesting.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


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

    model_config = ConfigDict(extra="ignore")

    enabled: bool = Field(default=True, description="Whether this reviewer integration is active")
    auto_resolve_stale: bool = Field(
        default=True,
        description="Whether resolve_stale_comments touches this reviewer's threads",
    )
    resolve_levels: list[Severity] = Field(
        default_factory=lambda: list(Severity),
        description="Severity levels that are allowed to be resolved",
    )
    require_reply_before_resolve: bool = Field(
        default=True,
        description="Block resolve_comment unless the thread has a non-reviewer reply explaining how the feedback was addressed",
    )
    rereview_message: str | None = Field(
        default=None,
        min_length=1,
        description="Custom message to post when triggering a re-review (only for manual-trigger reviewers)",
    )


class PRDescriptionsConfig(BaseModel):
    """Configuration for PR description management tools."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = Field(default=True, description="Whether PR description tools are available")


class SelfImprovementConfig(BaseModel):
    """Configuration for agent-driven self-improvement feedback loop."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = Field(default=False, description="Whether agents should file issues for server gaps they encounter")
    repo: str = Field(default="", description="Repository to file issues against (e.g. 'owner/codereviewbuddy')")

    @model_validator(mode="after")
    def _validate_repo_when_enabled(self) -> SelfImprovementConfig:
        if self.enabled and not self.repo.strip():
            msg = "[self_improvement] enabled=true requires a non-empty 'repo' field"
            raise ValueError(msg)
        return self


class DiagnosticsConfig(BaseModel):
    """Configuration for diagnostic and debugging features."""

    model_config = ConfigDict(extra="ignore")

    io_tap: bool = Field(default=False, description="Enable stdin/stdout logging for transport debugging (#65)")
    tool_call_heartbeat: bool = Field(
        default=False,
        description="Emit periodic in-flight heartbeat entries while tool calls are pending",
    )
    heartbeat_interval_ms: int = Field(
        default=5000,
        ge=100,
        description="Heartbeat interval in milliseconds for in-flight tool calls",
    )
    include_args_fingerprint: bool = Field(
        default=True,
        description="Include argument payload fingerprint and size metadata in tool call logs",
    )


class Config(BaseModel):
    """Top-level codereviewbuddy configuration."""

    model_config = ConfigDict(extra="ignore")

    reviewers: dict[str, ReviewerConfig] = Field(
        default_factory=dict,
        description="Per-reviewer configuration sections",
    )
    pr_descriptions: PRDescriptionsConfig = Field(
        default_factory=PRDescriptionsConfig,
        description="PR description management settings",
    )
    self_improvement: SelfImprovementConfig = Field(
        default_factory=SelfImprovementConfig,
        description="Agent self-improvement feedback loop settings",
    )
    diagnostics: DiagnosticsConfig = Field(
        default_factory=DiagnosticsConfig,
        description="Diagnostic and debugging settings",
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


def load_config() -> Config:
    """Load configuration from ``CRB_*`` environment variables.

    Uses ``pydantic-settings`` so all fields are populated from env vars
    automatically.  Zero-config still works — all fields have defaults.

    Reviewer-specific env vars use ``__`` nesting, e.g.::

        CRB_REVIEWERS__DEVIN__ENABLED = false
        CRB_SELF_IMPROVEMENT__ENABLED = true
        CRB_SELF_IMPROVEMENT__REPO = owner / repo
        CRB_DIAGNOSTICS__IO_TAP = true
    """

    class _EnvConfig(BaseSettings):
        """Thin wrapper that reads ``CRB_*`` env vars into a ``Config``."""

        model_config = SettingsConfigDict(
            env_prefix="CRB_",
            env_nested_delimiter="__",
            extra="ignore",
        )

        reviewers: dict[str, ReviewerConfig] = Field(default_factory=dict)
        pr_descriptions: PRDescriptionsConfig = Field(default_factory=PRDescriptionsConfig)
        self_improvement: SelfImprovementConfig = Field(default_factory=SelfImprovementConfig)
        diagnostics: DiagnosticsConfig = Field(default_factory=DiagnosticsConfig)

    env = _EnvConfig()
    # Build a proper Config (which applies reviewer defaults via model_validator)
    config = Config(
        reviewers=env.reviewers,
        pr_descriptions=env.pr_descriptions,
        self_improvement=env.self_improvement,
        diagnostics=env.diagnostics,
    )
    logger.info("Config loaded from CRB_* env vars")
    return config


# -- Global config state (set once at startup) ---------------------------------

_active_config: Config = Config()


def get_config() -> Config:
    """Return the active configuration (set at server startup via ``set_config``)."""
    return _active_config


def set_config(config: Config) -> None:
    """Set the active configuration (called during server startup)."""
    global _active_config  # noqa: PLW0603
    _active_config = config
