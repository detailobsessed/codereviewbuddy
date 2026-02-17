"""Tests for the CLI module."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

from codereviewbuddy.cli import (
    _is_known_var,
    _mask_value,
    check_env,
)

_KNOWN = frozenset({
    "CRB_REVIEWERS",
    "CRB_PR_DESCRIPTIONS",
    "CRB_SELF_IMPROVEMENT",
    "CRB_DIAGNOSTICS",
    "CRB_WORKSPACE",
})


class TestIsKnownVar:
    def test_exact_match(self):
        assert _is_known_var("CRB_REVIEWERS", _KNOWN) is True

    def test_nested_match(self):
        assert _is_known_var("CRB_REVIEWERS__DEVIN__ENABLED", _KNOWN) is True

    def test_unknown(self):
        assert _is_known_var("CRB_TYPO", _KNOWN) is False

    def test_partial_prefix_not_matched(self):
        assert _is_known_var("CRB_REVIEWER", _KNOWN) is False


class TestMaskValue:
    def test_normal_value(self):
        assert _mask_value("CRB_DIAGNOSTICS__IO_TAP", "true") == "true"

    def test_sensitive_value_masked(self):
        result = _mask_value("CRB_SECRET_TOKEN", "abcdefgh")
        assert result.startswith("ab")
        assert result.endswith("gh")
        assert "****" in result

    def test_short_sensitive_value(self):
        assert _mask_value("CRB_TOKEN", "abc") == "****"

    def test_long_value_truncated(self):
        long_val = "x" * 100
        result = _mask_value("CRB_REVIEWERS", long_val)
        assert len(result) == 80
        assert result.endswith("...")


class TestCheckEnv:
    def test_runs_without_error(self, mocker: MockerFixture, capsys):
        """check_env should run and print output without crashing."""
        mocker.patch("codereviewbuddy.gh.check_auth", return_value="testuser")
        check_env()
        captured = capsys.readouterr()
        assert "codereviewbuddy check-env" in captured.out
        assert "testuser" in captured.out

    def test_detects_unrecognized_vars(self, mocker: MockerFixture, monkeypatch, capsys):
        """Unrecognized CRB_* vars should be flagged."""
        monkeypatch.setenv("CRB_TYPO_VAR", "oops")
        mocker.patch("codereviewbuddy.gh.check_auth", return_value="testuser")
        check_env()
        captured = capsys.readouterr()
        assert "UNRECOGNIZED" in captured.out
        assert "CRB_TYPO_VAR" in captured.out

    def test_gh_cli_error_handled(self, mocker: MockerFixture, capsys):
        """gh CLI errors should be caught and reported, not crash."""
        mocker.patch("codereviewbuddy.gh.check_auth", side_effect=RuntimeError("not installed"))
        check_env()
        captured = capsys.readouterr()
        assert "gh CLI error" in captured.out
