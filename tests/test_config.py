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

    def test_self_improvement_enabled(self):
        from codereviewbuddy.config import SelfImprovementConfig

        config = Config(self_improvement=SelfImprovementConfig(enabled=True))
        assert config.self_improvement.enabled is True

    def test_owner_logins_defaults_empty(self):
        config = Config()
        assert config.owner_logins == []

    def test_owner_logins_explicit(self):
        config = Config(owner_logins=["alice", "bob"])
        assert config.owner_logins == ["alice", "bob"]


class TestLoadConfigFromEnv:
    """Tests for load_config() reading CRB_* environment variables."""

    @pytest.fixture(autouse=True)
    def _isolate_from_dotenv(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
        """Prevent a local .env file from contaminating config tests."""
        monkeypatch.chdir(tmp_path)

    def test_self_improvement_from_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CRB_SELF_IMPROVEMENT__ENABLED", "true")
        config = load_config()
        assert config.self_improvement.enabled is True

    def test_pr_descriptions_disabled(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CRB_PR_DESCRIPTIONS__ENABLED", "false")
        config = load_config()
        assert config.pr_descriptions.enabled is False

    def test_owner_logins_from_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CRB_OWNER_LOGINS", "alice,bob")
        config = load_config()
        assert config.owner_logins == ["alice", "bob"]

    def test_owner_logins_empty_by_default(self):
        config = load_config()
        assert config.owner_logins == []

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
