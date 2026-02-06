"""Tests for the gh CLI wrapper."""

from __future__ import annotations

import json
import subprocess  # noqa: S404
from unittest.mock import patch

import pytest

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


def _mock_run(stdout: str = "", stderr: str = "", returncode: int = 0):
    """Create a mock subprocess.run result."""
    result = subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)
    return patch("codereviewbuddy.gh.subprocess.run", return_value=result)


class TestRunGh:
    def test_success(self):
        with _mock_run(stdout="hello\n") as mock:
            result = run_gh("auth", "status")
            assert result == "hello\n"
            mock.assert_called_once()
            args = mock.call_args[0][0]
            assert args == ["gh", "auth", "status"]

    def test_not_found(self):
        with (
            patch("codereviewbuddy.gh.subprocess.run", side_effect=FileNotFoundError),
            pytest.raises(GhNotFoundError, match="gh CLI not found"),
        ):
            run_gh("auth", "status")

    def test_command_failure(self):
        with _mock_run(stderr="bad request", returncode=1), pytest.raises(GhError, match="bad request"):
            run_gh("api", "graphql")

    def test_cwd_passed(self):
        with _mock_run(stdout="ok") as mock:
            run_gh("repo", "view", cwd="/tmp")  # noqa: S108
            assert mock.call_args[1]["cwd"] == "/tmp"  # noqa: S108


class TestGraphql:
    def test_simple_query(self):
        response = {"data": {"viewer": {"login": "testuser"}}}
        with _mock_run(stdout=json.dumps(response)):
            result = graphql("query { viewer { login } }")
            assert result == response

    def test_variables_string(self):
        response = {"data": {}}
        with _mock_run(stdout=json.dumps(response)) as mock:
            graphql("query($owner: String!) { }", variables={"owner": "test"})
            args = mock.call_args[0][0]
            assert "-f" in args
            assert "owner=test" in args

    def test_variables_int(self):
        response = {"data": {}}
        with _mock_run(stdout=json.dumps(response)) as mock:
            graphql("query($pr: Int!) { }", variables={"pr": 42})
            args = mock.call_args[0][0]
            assert "-F" in args
            assert "pr=42" in args


class TestRest:
    def test_get(self):
        response = [{"number": 1}]
        with _mock_run(stdout=json.dumps(response)):
            result = rest("/repos/owner/repo/pulls")
            assert result == response

    def test_empty_response(self):
        with _mock_run(stdout="  "):
            result = rest("/repos/owner/repo/pulls/1/merge", method="PUT")
            assert result is None


class TestCheckAuth:
    def test_authenticated(self):
        output = "✓ Logged in to github.com account testuser (keyring)"
        with _mock_run(stdout=output):
            username = check_auth()
            assert username == "testuser"

    def test_not_authenticated(self):
        with _mock_run(stderr="not logged in", returncode=1), pytest.raises(GhNotAuthenticatedError):
            check_auth()


class TestGetRepoInfo:
    def test_success(self):
        with _mock_run(stdout="detailobsessed/codereviewbuddy\n"):
            owner, repo = get_repo_info()
            assert owner == "detailobsessed"
            assert repo == "codereviewbuddy"
