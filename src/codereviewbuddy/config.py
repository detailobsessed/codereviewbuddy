"""Per-reviewer configuration system.

All configuration is via ``CRB_*`` environment variables, set in MCP client config.
Zero-config still works — all settings have sensible defaults.

Uses ``pydantic-settings`` ``BaseSettings`` (same pattern as FastMCP's own settings)
so env vars are read automatically with the ``CRB_`` prefix and ``__`` nesting.
"""

from __future__ import annotations

import logging
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Severity(StrEnum):
    """Comment severity levels, ordered from least to most critical."""

    INFO = "info"
    WARNING = "warning"
    FLAGGED = "flagged"
    BUG = "bug"


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
        """Fill in missing reviewers with adapter-defined defaults.

        Each adapter declares ``default_auto_resolve_stale`` and
        ``default_resolve_levels`` properties.  For partially-specified
        reviewers, unset fields are filled from the adapter so that e.g.
        ``{"devin": {"enabled": false}}`` still gets
        ``auto_resolve_stale=False`` (Devin's safe default) rather than
        the generic ``ReviewerConfig`` field default (``True``).
        """
        from codereviewbuddy.reviewers.registry import REVIEWERS  # noqa: PLC0415

        for adapter in REVIEWERS:
            if adapter.name not in self.reviewers:
                self.reviewers[adapter.name] = ReviewerConfig(
                    auto_resolve_stale=adapter.default_auto_resolve_stale,
                    resolve_levels=adapter.default_resolve_levels,
                )
            else:
                rc = self.reviewers[adapter.name]
                if "auto_resolve_stale" not in rc.model_fields_set:
                    rc.auto_resolve_stale = adapter.default_auto_resolve_stale
                if "resolve_levels" not in rc.model_fields_set:
                    rc.resolve_levels = adapter.default_resolve_levels
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

    Reviewer overrides can be a JSON string (recommended for MCP client config)::

        CRB_REVIEWERS = '{"devin": {"enabled": false}}'

    Or use ``__`` nesting for individual fields::

        CRB_REVIEWERS__DEVIN__ENABLED = false

    Other examples::

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
