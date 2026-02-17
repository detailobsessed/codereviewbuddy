"""Tests for the per-reviewer configuration system."""

from __future__ import annotations

import pytest

from codereviewbuddy.config import (
    Config,
    ReviewerConfig,
    Severity,
    load_config,
)


class TestReviewerConfig:
    def test_defaults(self):
        rc = ReviewerConfig()
        assert rc.enabled is True
        assert rc.auto_resolve_stale is True
        assert set(rc.resolve_levels) == set(Severity)

    def test_custom_resolve_levels(self):
        rc = ReviewerConfig(resolve_levels=[Severity.INFO, Severity.WARNING])
        assert rc.resolve_levels == [Severity.INFO, Severity.WARNING]

    def test_empty_resolve_levels(self):
        rc = ReviewerConfig(resolve_levels=[])
        assert rc.resolve_levels == []


class TestConfig:
    def test_defaults_fill_known_reviewers(self):
        config = Config()
        assert "devin" in config.reviewers
        assert "unblocked" in config.reviewers
        assert "coderabbit" in config.reviewers
        assert "greptile" in config.reviewers

    def test_greptile_defaults(self):
        config = Config()
        greptile = config.reviewers["greptile"]
        assert greptile.enabled is True
        assert greptile.auto_resolve_stale is True
        assert set(greptile.resolve_levels) == set(Severity)

    def test_devin_defaults(self):
        config = Config()
        devin = config.reviewers["devin"]
        assert devin.enabled is True
        assert devin.auto_resolve_stale is False
        assert devin.resolve_levels == [Severity.INFO]

    def test_unblocked_defaults(self):
        config = Config()
        unblocked = config.reviewers["unblocked"]
        assert unblocked.enabled is True
        assert unblocked.auto_resolve_stale is True
        assert set(unblocked.resolve_levels) == set(Severity)

    def test_coderabbit_defaults(self):
        config = Config()
        coderabbit = config.reviewers["coderabbit"]
        assert coderabbit.enabled is True
        assert coderabbit.auto_resolve_stale is False
        assert coderabbit.resolve_levels == []

    def test_partial_override_preserves_other_defaults(self):
        config = Config(reviewers={"devin": ReviewerConfig(resolve_levels=[Severity.INFO, Severity.WARNING])})
        # Devin override applied
        assert config.reviewers["devin"].resolve_levels == [Severity.INFO, Severity.WARNING]
        assert config.reviewers["devin"].enabled is True  # field default
        # Unset fields get reviewer-specific defaults, NOT generic ReviewerConfig defaults
        assert config.reviewers["devin"].auto_resolve_stale is False  # Devin's safe default
        # Other reviewers still get their defaults
        assert config.reviewers["unblocked"].auto_resolve_stale is True
        assert config.reviewers["coderabbit"].resolve_levels == []

    def test_partial_override_devin_disabled_preserves_safe_defaults(self):
        """Setting only enabled=False should keep Devin's restrictive resolve_levels and auto_resolve_stale."""
        config = Config(reviewers={"devin": ReviewerConfig(enabled=False)})
        assert config.reviewers["devin"].enabled is False
        assert config.reviewers["devin"].auto_resolve_stale is False  # Devin-specific, not generic True
        assert config.reviewers["devin"].resolve_levels == [Severity.INFO]  # Devin-specific, not all severities

    def test_empty_section_preserves_reviewer_defaults(self):
        """Empty reviewer config (no fields set) should behave like zero-config for that reviewer."""
        config = Config(reviewers={"devin": ReviewerConfig()})
        assert config.reviewers["devin"].auto_resolve_stale is False  # Devin-specific default
        assert config.reviewers["devin"].resolve_levels == [Severity.INFO]  # Devin-specific default

    def test_unknown_reviewer_gets_permissive_defaults(self):
        config = Config()
        rc = config.get_reviewer("some-new-reviewer")
        assert rc.enabled is True
        assert rc.auto_resolve_stale is True
        assert set(rc.resolve_levels) == set(Severity)

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


class TestCanResolve:
    def test_allowed_severity(self):
        config = Config()
        allowed, reason = config.can_resolve("unblocked", Severity.INFO)
        assert allowed is True
        assert not reason

    def test_blocked_severity(self):
        config = Config()
        allowed, reason = config.can_resolve("devin", Severity.BUG)
        assert allowed is False
        assert "bug" in reason.lower()
        assert "devin" in reason.lower()

    def test_blocked_disabled_reviewer(self):
        config = Config(reviewers={"devin": ReviewerConfig(enabled=False)})
        allowed, reason = config.can_resolve("devin", Severity.INFO)
        assert allowed is False
        assert "disabled" in reason.lower()

    def test_coderabbit_blocks_all(self):
        config = Config()
        allowed, reason = config.can_resolve("coderabbit", Severity.INFO)
        assert allowed is False
        assert "info" in reason.lower()

    def test_devin_allows_info_only(self):
        config = Config()
        for severity, should_allow in [
            (Severity.INFO, True),
            (Severity.WARNING, False),
            (Severity.FLAGGED, False),
            (Severity.BUG, False),
        ]:
            allowed, _ = config.can_resolve("devin", severity)
            assert allowed is should_allow, f"Expected {should_allow} for severity={severity!r}"

    def test_unblocked_allows_all(self):
        config = Config()
        for severity in Severity:
            allowed, _ = config.can_resolve("unblocked", severity)
            assert allowed is True, f"Expected True for severity={severity!r}"


class TestLoadConfigFromEnv:
    """Tests for load_config() reading CRB_* environment variables."""

    def test_no_env_vars_returns_defaults(self):
        config = load_config()
        assert "devin" in config.reviewers
        assert config.reviewers["devin"].resolve_levels == [Severity.INFO]

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

    def test_reviewer_defaults_still_applied(self, monkeypatch: pytest.MonkeyPatch):
        """Even with env vars set, reviewer defaults should be applied."""
        monkeypatch.setenv("CRB_DIAGNOSTICS__IO_TAP", "true")
        config = load_config()
        assert "devin" in config.reviewers
        assert config.reviewers["devin"].auto_resolve_stale is False
        assert config.reviewers["coderabbit"].resolve_levels == []

    def test_unknown_env_vars_ignored(self, monkeypatch: pytest.MonkeyPatch):
        """CRB_ env vars for unknown fields should be silently ignored."""
        monkeypatch.setenv("CRB_BOGUS_SETTING", "whatever")
        config = load_config()  # Should not raise
        assert "devin" in config.reviewers


class TestGetSetConfig:
    """Tests for the global config state (get_config / set_config)."""

    def test_get_config_returns_set_config(self):
        from codereviewbuddy.config import get_config, set_config

        custom = Config(pr_descriptions=Config.model_fields["pr_descriptions"].default_factory())
        custom.pr_descriptions.enabled = False
        set_config(custom)
        assert get_config().pr_descriptions.enabled is False

    def test_default_config_has_reviewer_defaults(self):
        from codereviewbuddy.config import get_config

        config = get_config()
        assert "devin" in config.reviewers
        assert "unblocked" in config.reviewers
        assert "greptile" in config.reviewers
