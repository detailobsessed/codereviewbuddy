"""Tests for the configuration system."""

from __future__ import annotations

import pytest

from codereviewbuddy.config import (
    Config,
    load_config,
)


class TestConfig:
    def test_self_improvement_defaults(self):
        config = Config()
        assert config.self_improvement.enabled is False
        assert not config.self_improvement.repo

    def test_self_improvement_configured(self):
        from codereviewbuddy.config import SelfImprovementConfig

        config = Config(self_improvement=SelfImprovementConfig(enabled=True, repo="owner/repo"))
        assert config.self_improvement.enabled is True
        assert config.self_improvement.repo == "owner/repo"

    def test_self_improvement_enabled_without_repo_raises(self):
        from codereviewbuddy.config import SelfImprovementConfig

        with pytest.raises(ValueError, match="requires a non-empty 'repo' field"):
            SelfImprovementConfig(enabled=True)

    def test_self_improvement_enabled_with_whitespace_repo_raises(self):
        """Regression: whitespace-only repo must not bypass validation."""
        from codereviewbuddy.config import SelfImprovementConfig

        with pytest.raises(ValueError, match="requires a non-empty 'repo' field"):
            SelfImprovementConfig(enabled=True, repo="  ")

    def test_self_improvement_disabled_without_repo_ok(self):
        from codereviewbuddy.config import SelfImprovementConfig

        config = SelfImprovementConfig(enabled=False)
        assert not config.repo

    def test_diagnostics_defaults(self):
        from codereviewbuddy.config import DiagnosticsConfig

        config = DiagnosticsConfig()
        assert config.io_tap is False
        assert config.tool_call_heartbeat is False
        assert config.heartbeat_interval_ms == 5000
        assert config.include_args_fingerprint is True

    def test_diagnostics_enabled(self):
        from codereviewbuddy.config import DiagnosticsConfig

        config = DiagnosticsConfig(
            io_tap=True,
            tool_call_heartbeat=True,
            heartbeat_interval_ms=750,
            include_args_fingerprint=False,
        )
        assert config.io_tap is True
        assert config.tool_call_heartbeat is True
        assert config.heartbeat_interval_ms == 750
        assert config.include_args_fingerprint is False

    def test_diagnostics_from_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CRB_DIAGNOSTICS__IO_TAP", "true")
        monkeypatch.setenv("CRB_DIAGNOSTICS__TOOL_CALL_HEARTBEAT", "true")
        monkeypatch.setenv("CRB_DIAGNOSTICS__HEARTBEAT_INTERVAL_MS", "1200")
        monkeypatch.setenv("CRB_DIAGNOSTICS__INCLUDE_ARGS_FINGERPRINT", "false")
        config = load_config()
        assert config.diagnostics.io_tap is True
        assert config.diagnostics.tool_call_heartbeat is True
        assert config.diagnostics.heartbeat_interval_ms == 1200
        assert config.diagnostics.include_args_fingerprint is False


class TestLoadConfigFromEnv:
    """Tests for load_config() reading CRB_* environment variables."""

    def test_self_improvement_from_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CRB_SELF_IMPROVEMENT__ENABLED", "true")
        monkeypatch.setenv("CRB_SELF_IMPROVEMENT__REPO", "owner/myrepo")
        config = load_config()
        assert config.self_improvement.enabled is True
        assert config.self_improvement.repo == "owner/myrepo"

    def test_diagnostics_from_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CRB_DIAGNOSTICS__IO_TAP", "true")
        monkeypatch.setenv("CRB_DIAGNOSTICS__HEARTBEAT_INTERVAL_MS", "750")
        config = load_config()
        assert config.diagnostics.io_tap is True
        assert config.diagnostics.heartbeat_interval_ms == 750

    def test_pr_descriptions_disabled(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CRB_PR_DESCRIPTIONS__ENABLED", "false")
        config = load_config()
        assert config.pr_descriptions.enabled is False

    def test_unknown_env_vars_ignored(self, monkeypatch: pytest.MonkeyPatch):
        """CRB_ env vars for unknown fields should be silently ignored."""
        monkeypatch.setenv("CRB_BOGUS_SETTING", "whatever")
        load_config()  # Should not raise


class TestGetSetConfig:
    """Tests for the global config state (get_config / set_config)."""

    def test_get_config_returns_set_config(self):
        from codereviewbuddy.config import get_config, set_config

        custom = Config(pr_descriptions=Config.model_fields["pr_descriptions"].default_factory())
        custom.pr_descriptions.enabled = False
        set_config(custom)
        assert get_config().pr_descriptions.enabled is False
