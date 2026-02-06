"""Tests for the rereview tool."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from codereviewbuddy.tools.rereview import request_rereview


class TestRequestRereview:
    def test_trigger_unblocked(self):
        with (
            patch("codereviewbuddy.tools.rereview.gh.get_repo_info", return_value=("owner", "repo")),
            patch("codereviewbuddy.tools.rereview.gh.run_gh") as mock_run,
        ):
            result = request_rereview(42, reviewer="unblocked")
            assert "unblocked" in result["triggered"]
            assert result["auto_triggers"] == []
            mock_run.assert_called_once()
            args = mock_run.call_args[0]
            assert "42" in args
            assert "@unblocked please re-review" in args

    def test_devin_auto_triggers(self):
        with patch("codereviewbuddy.tools.rereview.gh.get_repo_info", return_value=("owner", "repo")):
            result = request_rereview(42, reviewer="devin")
            assert result["triggered"] == []
            assert "devin" in result["auto_triggers"]

    def test_coderabbit_auto_triggers(self):
        with patch("codereviewbuddy.tools.rereview.gh.get_repo_info", return_value=("owner", "repo")):
            result = request_rereview(42, reviewer="coderabbit")
            assert result["triggered"] == []
            assert "coderabbit" in result["auto_triggers"]

    def test_trigger_all(self):
        with (
            patch("codereviewbuddy.tools.rereview.gh.get_repo_info", return_value=("owner", "repo")),
            patch("codereviewbuddy.tools.rereview.gh.run_gh"),
        ):
            result = request_rereview(42)
            assert "unblocked" in result["triggered"]
            assert "devin" in result["auto_triggers"]
            assert "coderabbit" in result["auto_triggers"]

    def test_unknown_reviewer(self):
        with (
            patch("codereviewbuddy.tools.rereview.gh.get_repo_info", return_value=("owner", "repo")),
            pytest.raises(ValueError, match="Unknown reviewer"),
        ):
            request_rereview(42, reviewer="nonexistent")

    def test_explicit_repo(self):
        with patch("codereviewbuddy.tools.rereview.gh.run_gh") as mock_run:
            result = request_rereview(42, reviewer="unblocked", repo="myorg/myrepo")
            assert "unblocked" in result["triggered"]
            args = mock_run.call_args[0]
            assert "myorg/myrepo" in args
