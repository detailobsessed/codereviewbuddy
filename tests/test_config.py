"""Tests for the per-reviewer configuration system."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from codereviewbuddy.config import (
    Config,
    ReviewerConfig,
    Severity,
    _collect_unknown_keys,
    clean_config,
    load_config,
    update_config,
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
        """Empty TOML section (no fields set) should behave like zero-config for that reviewer."""
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
        assert config.self_improvement.repo == ""

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
        assert config.repo == ""

    def test_diagnostics_defaults(self):
        from codereviewbuddy.config import DiagnosticsConfig

        config = DiagnosticsConfig()
        assert config.io_tap is False

    def test_diagnostics_enabled(self):
        from codereviewbuddy.config import DiagnosticsConfig

        config = DiagnosticsConfig(io_tap=True)
        assert config.io_tap is True

    def test_diagnostics_from_toml(self, tmp_path: Path):
        toml_file = tmp_path / ".codereviewbuddy.toml"
        toml_file.write_text("[diagnostics]\nio_tap = true\n")
        config = load_config(cwd=tmp_path)
        assert config.diagnostics.io_tap is True


class TestCanResolve:
    def test_allowed_severity(self):
        config = Config()
        allowed, reason = config.can_resolve("unblocked", Severity.INFO)
        assert allowed is True
        assert reason == ""

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


class TestLoadConfig:
    def test_missing_file_returns_defaults(self, tmp_path: Path):
        config = load_config(cwd=tmp_path)
        assert "devin" in config.reviewers
        assert config.reviewers["devin"].resolve_levels == [Severity.INFO]

    def test_load_valid_toml(self, tmp_path: Path):
        # Create a git root so the walk-up stops
        (tmp_path / ".git").mkdir()
        config_file = tmp_path / ".codereviewbuddy.toml"
        config_file.write_text(
            """\
[reviewers.devin]
resolve_levels = ["info", "warning"]

[reviewers.unblocked]
auto_resolve_stale = false
""",
            encoding="utf-8",
        )
        config = load_config(cwd=tmp_path)
        assert config.reviewers["devin"].resolve_levels == [Severity.INFO, Severity.WARNING]
        assert config.reviewers["unblocked"].auto_resolve_stale is False
        # coderabbit still gets defaults
        assert config.reviewers["coderabbit"].auto_resolve_stale is False

    def test_load_self_improvement_from_toml(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".codereviewbuddy.toml").write_text(
            """\
[self_improvement]
enabled = true
repo = "detailobsessed/codereviewbuddy"
""",
            encoding="utf-8",
        )
        config = load_config(cwd=tmp_path)
        assert config.self_improvement.enabled is True
        assert config.self_improvement.repo == "detailobsessed/codereviewbuddy"

    def test_load_walks_up_to_git_root(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".codereviewbuddy.toml").write_text(
            "[reviewers.devin]\nenabled = false\n",
            encoding="utf-8",
        )
        subdir = tmp_path / "src" / "deep"
        subdir.mkdir(parents=True)
        config = load_config(cwd=subdir)
        assert config.reviewers["devin"].enabled is False

    def test_stops_at_git_root(self, tmp_path: Path):
        # Config above git root should not be found
        (tmp_path / ".codereviewbuddy.toml").write_text(
            "[reviewers.devin]\nenabled = false\n",
            encoding="utf-8",
        )
        project = tmp_path / "project"
        project.mkdir()
        (project / ".git").mkdir()
        config = load_config(cwd=project)
        # Should NOT find the config above .git — devin stays enabled (default)
        assert config.reviewers["devin"].enabled is True

    def test_invalid_toml_raises(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".codereviewbuddy.toml").write_text("{{invalid toml", encoding="utf-8")
        with pytest.raises(ValueError, match="Invalid TOML"):
            load_config(cwd=tmp_path)

    def test_invalid_config_values_raises(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".codereviewbuddy.toml").write_text(
            '[reviewers.devin]\nresolve_levels = ["not_a_severity"]\n',
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="Invalid config"):
            load_config(cwd=tmp_path)

    def test_empty_config_file_returns_defaults(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".codereviewbuddy.toml").write_text("", encoding="utf-8")
        config = load_config(cwd=tmp_path)
        assert "devin" in config.reviewers

    def test_init_template_empty_sections_match_zero_config(self, tmp_path: Path):
        """Init template has [reviewers.devin] etc. with all values commented out.

        TOML parses these as empty dicts. Verify the result matches zero-config defaults.
        """
        (tmp_path / ".git").mkdir()
        (tmp_path / ".codereviewbuddy.toml").write_text(
            """\
[reviewers.devin]

[reviewers.unblocked]

[reviewers.coderabbit]
""",
            encoding="utf-8",
        )
        config = load_config(cwd=tmp_path)
        zero = Config()
        for name in ("devin", "unblocked", "coderabbit"):
            assert config.reviewers[name].enabled == zero.reviewers[name].enabled, name
            assert config.reviewers[name].auto_resolve_stale == zero.reviewers[name].auto_resolve_stale, name
            assert config.reviewers[name].resolve_levels == zero.reviewers[name].resolve_levels, name


class TestCollectUnknownKeys:
    def test_top_level_unknown(self):
        data = {"reviewers": {}, "bogus_key": True}
        assert _collect_unknown_keys(data, Config) == ["bogus_key"]

    def test_nested_unknown_in_pr_descriptions(self):
        data = {"pr_descriptions": {"enabled": True, "require_review": False}}
        assert _collect_unknown_keys(data, Config) == ["pr_descriptions.require_review"]

    def test_no_unknowns(self):
        data = {"pr_descriptions": {"enabled": True}}
        assert _collect_unknown_keys(data, Config) == []

    def test_unknown_reviewer_names_are_allowed(self):
        """Unknown reviewer names under [reviewers.*] should NOT be flagged."""
        data = {"reviewers": {"future_bot": {"enabled": True}}}
        assert _collect_unknown_keys(data, Config) == []

    def test_multiple_unknowns(self):
        data = {"pr_descriptions": {"require_review": False}, "foo": 1}
        unknown = _collect_unknown_keys(data, Config)
        assert "pr_descriptions.require_review" in unknown
        assert "foo" in unknown


class TestLoadConfigWarnings:
    def test_warns_on_unknown_keys(self, tmp_path: Path, caplog: pytest.LogCaptureFixture):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".codereviewbuddy.toml").write_text(
            "[pr_descriptions]\nrequire_review = false\n",
            encoding="utf-8",
        )
        import logging

        with caplog.at_level(logging.WARNING, logger="codereviewbuddy.config"):
            load_config(cwd=tmp_path)
        assert any("require_review" in r.message for r in caplog.records)
        assert any("--update" in r.message for r in caplog.records)


class TestUpdateConfigDeprecation:
    def test_comments_out_deprecated_keys(self, tmp_path: Path):
        config_file = tmp_path / ".codereviewbuddy.toml"
        config_file.write_text(
            "[pr_descriptions]\nenabled = true\nrequire_review = false\n",
            encoding="utf-8",
        )
        _, _added, deprecated = update_config(cwd=tmp_path)
        assert "pr_descriptions.require_review" in deprecated
        content = config_file.read_text(encoding="utf-8")
        assert "DEPRECATED" in content
        assert "require_review" in content  # still present as comment

    def test_no_deprecations(self, tmp_path: Path):
        config_file = tmp_path / ".codereviewbuddy.toml"
        config_file.write_text("[pr_descriptions]\nenabled = true\n", encoding="utf-8")
        _, _added, deprecated = update_config(cwd=tmp_path)
        assert deprecated == []

    def test_comments_out_unknown_table_section(self, tmp_path: Path):
        config_file = tmp_path / ".codereviewbuddy.toml"
        config_file.write_text(
            '[pr_descriptions]\nenabled = true\n\n[old_section]\nkey = "val"\n',
            encoding="utf-8",
        )
        _, _added, deprecated = update_config(cwd=tmp_path)
        assert "old_section" in deprecated
        content = config_file.read_text(encoding="utf-8")
        assert "DEPRECATED" in content

    def test_comments_out_empty_unknown_table(self, tmp_path: Path):
        """Regression: empty unknown table used to crash with IndexError."""
        config_file = tmp_path / ".codereviewbuddy.toml"
        config_file.write_text(
            "[pr_descriptions]\nenabled = true\n\n[deprecated_section]\n",
            encoding="utf-8",
        )
        _, _added, deprecated = update_config(cwd=tmp_path)
        assert "deprecated_section" in deprecated
        content = config_file.read_text(encoding="utf-8")
        assert "DEPRECATED" in content


class TestCleanConfig:
    def test_removes_deprecated_keys(self, tmp_path: Path):
        config_file = tmp_path / ".codereviewbuddy.toml"
        config_file.write_text(
            "[pr_descriptions]\nenabled = true\nrequire_review = false\n",
            encoding="utf-8",
        )
        _, removed = clean_config(cwd=tmp_path)
        assert "pr_descriptions.require_review" in removed
        content = config_file.read_text(encoding="utf-8")
        assert "require_review" not in content
        assert "enabled" in content  # known key preserved

    def test_no_deprecated_keys(self, tmp_path: Path):
        config_file = tmp_path / ".codereviewbuddy.toml"
        config_file.write_text("[pr_descriptions]\nenabled = true\n", encoding="utf-8")
        _, removed = clean_config(cwd=tmp_path)
        assert removed == []

    def test_fails_if_no_config(self, tmp_path: Path):
        with pytest.raises(SystemExit):
            clean_config(cwd=tmp_path)
