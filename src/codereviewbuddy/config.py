"""Configuration system.

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


class Config(BaseModel):
    """Top-level codereviewbuddy configuration."""

    model_config = ConfigDict(extra="ignore")

    pr_descriptions: PRDescriptionsConfig = Field(
        default_factory=PRDescriptionsConfig,
        description="PR description management settings",
    )
    self_improvement: SelfImprovementConfig = Field(
        default_factory=SelfImprovementConfig,
        description="Agent self-improvement feedback loop settings",
    )
    owner_logins: list[str] = Field(
        default_factory=list,
        description="GitHub usernames considered 'ours' for triage filtering (JSON list in env, e.g. '[\"alice\",\"bob\"]')",
    )


def load_config() -> Config:
    """Load configuration from ``CRB_*`` environment variables.

    Uses ``pydantic-settings`` so all fields are populated from env vars
    automatically.  Zero-config still works — all fields have defaults.

    Examples::

        CRB_SELF_IMPROVEMENT__ENABLED = true
        CRB_SELF_IMPROVEMENT__REPO = owner / repo
    """

    class _EnvConfig(BaseSettings):
        """Thin wrapper that reads ``CRB_*`` env vars into a ``Config``."""

        model_config = SettingsConfigDict(
            env_prefix="CRB_",
            env_nested_delimiter="__",
            extra="ignore",
        )

        pr_descriptions: PRDescriptionsConfig = Field(default_factory=PRDescriptionsConfig)
        self_improvement: SelfImprovementConfig = Field(default_factory=SelfImprovementConfig)
        owner_logins: list[str] = Field(default_factory=list)

    env = _EnvConfig()
    config = Config(
        pr_descriptions=env.pr_descriptions,
        self_improvement=env.self_improvement,
        owner_logins=env.owner_logins,
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
