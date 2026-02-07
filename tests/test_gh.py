"""Tests for the gh CLI wrapper."""

from __future__ import annotations

import json
import subprocess  # noqa: S404
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

from codereviewbuddy.gh import (
    GhError,
    GhNotAuthenticatedError,
    GhNotFoundError,
    check_auth,
    get_repo_info,
    graphql,
    rest,
    run_gh,
)


def _patch_run(
    mocker: MockerFixture,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
):
    """Patch subprocess.run and return the mock."""
    result = subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)
    return mocker.patch("codereviewbuddy.gh.subprocess.run", return_value=result)


class TestRunGh:
    def test_success(self, mocker: MockerFixture):
        mock = _patch_run(mocker, stdout="hello\n")
        result = run_gh("auth", "status")
        assert result == "hello\n"
        mock.assert_called_once()
        args = mock.call_args[0][0]
        assert args == ["gh", "auth", "status"]

    def test_not_found(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.gh.subprocess.run", side_effect=FileNotFoundError)
        with pytest.raises(GhNotFoundError, match="gh CLI not found"):
            run_gh("auth", "status")

    def test_command_failure(self, mocker: MockerFixture):
        _patch_run(mocker, stderr="bad request", returncode=1)
        with pytest.raises(GhError, match="bad request"):
            run_gh("api", "graphql")

    def test_cwd_passed(self, mocker: MockerFixture):
        mock = _patch_run(mocker, stdout="ok")
        run_gh("repo", "view", cwd="/tmp")  # noqa: S108
        assert mock.call_args[1]["cwd"] == "/tmp"  # noqa: S108


class TestGraphql:
    def test_simple_query(self, mocker: MockerFixture):
        response = {"data": {"viewer": {"login": "testuser"}}}
        _patch_run(mocker, stdout=json.dumps(response))
        result = graphql("query { viewer { login } }")
        assert result == response

    def test_variables_string(self, mocker: MockerFixture):
        response = {"data": {}}
        mock = _patch_run(mocker, stdout=json.dumps(response))
        graphql("query($owner: String!) { }", variables={"owner": "test"})
        args = mock.call_args[0][0]
        assert "-f" in args
        assert "owner=test" in args

    def test_variables_int(self, mocker: MockerFixture):
        response = {"data": {}}
        mock = _patch_run(mocker, stdout=json.dumps(response))
        graphql("query($pr: Int!) { }", variables={"pr": 42})
        args = mock.call_args[0][0]
        assert "-F" in args
        assert "pr=42" in args


class TestRest:
    def test_get(self, mocker: MockerFixture):
        response = [{"number": 1}]
        _patch_run(mocker, stdout=json.dumps(response))
        result = rest("/repos/owner/repo/pulls")
        assert result == response

    def test_empty_response(self, mocker: MockerFixture):
        _patch_run(mocker, stdout="  ")
        result = rest("/repos/owner/repo/pulls/1/merge", method="PUT")
        assert result is None


class TestCheckAuth:
    def test_authenticated(self, mocker: MockerFixture):
        output = "✓ Logged in to github.com account testuser (keyring)"
        _patch_run(mocker, stdout=output)
        username = check_auth()
        assert username == "testuser"

    def test_not_authenticated(self, mocker: MockerFixture):
        _patch_run(mocker, stderr="not logged in", returncode=1)
        with pytest.raises(GhNotAuthenticatedError):
            check_auth()


class TestGetRepoInfo:
    def test_success(self, mocker: MockerFixture):
        _patch_run(mocker, stdout="detailobsessed/codereviewbuddy\n")
        owner, repo = get_repo_info()
        assert owner == "detailobsessed"
        assert repo == "codereviewbuddy"
