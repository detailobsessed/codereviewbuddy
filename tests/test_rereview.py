"""Tests for the rereview tool."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

from codereviewbuddy.tools.rereview import request_rereview


class TestRequestRereview:
    @pytest.fixture(autouse=True)
    def _mock_gh(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.rereview.gh.get_repo_info", return_value=("owner", "repo"))
        self.mock_run = mocker.patch("codereviewbuddy.tools.rereview.gh.run_gh")

    async def test_trigger_unblocked(self):
        result = await request_rereview(42, reviewer="unblocked")
        assert "unblocked" in result.triggered
        assert result.auto_triggers == []
        self.mock_run.assert_called_once()
        args = self.mock_run.call_args[0]
        assert "42" in args
        assert "@unblocked please re-review" in args

    async def test_devin_auto_triggers(self):
        result = await request_rereview(42, reviewer="devin")
        assert result.triggered == []
        assert "devin" in result.auto_triggers

    async def test_coderabbit_auto_triggers(self):
        result = await request_rereview(42, reviewer="coderabbit")
        assert result.triggered == []
        assert "coderabbit" in result.auto_triggers

    async def test_trigger_all(self):
        result = await request_rereview(42)
        assert "unblocked" in result.triggered
        assert "devin" in result.auto_triggers
        assert "coderabbit" in result.auto_triggers

    async def test_unknown_reviewer(self):
        with pytest.raises(ValueError, match="Unknown reviewer"):
            await request_rereview(42, reviewer="nonexistent")

    async def test_explicit_repo(self, mocker: MockerFixture):
        mock_run = mocker.patch("codereviewbuddy.tools.rereview.gh.run_gh")
        result = await request_rereview(42, reviewer="unblocked", repo="myorg/myrepo")
        assert "unblocked" in result.triggered
        args = mock_run.call_args[0]
        assert "myorg/myrepo" in args
