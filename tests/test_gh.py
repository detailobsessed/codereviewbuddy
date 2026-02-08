"""Tests for the gh CLI wrapper."""

from __future__ import annotations

import json
import subprocess  # noqa: S404
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

from codereviewbuddy import cache
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
    def setup_method(self):
        cache.clear()

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

    def test_query_cached_on_second_call(self, mocker: MockerFixture):
        response = {"data": {"viewer": {"login": "testuser"}}}
        mock = _patch_run(mocker, stdout=json.dumps(response))
        r1 = graphql("query { viewer { login } }")
        r2 = graphql("query { viewer { login } }")
        assert r1 == r2
        assert mock.call_count == 1  # only one subprocess call

    def test_mutation_not_cached(self, mocker: MockerFixture):
        response = {"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}}
        mock = _patch_run(mocker, stdout=json.dumps(response))
        graphql("mutation { resolveReviewThread(input: {}) { thread { isResolved } } }")
        graphql("mutation { resolveReviewThread(input: {}) { thread { isResolved } } }")
        assert mock.call_count == 2  # both calls hit subprocess

    def test_mutation_clears_cache(self, mocker: MockerFixture):
        query_resp = {"data": {"threads": []}}
        mutation_resp = {"data": {"resolve": True}}
        mock = _patch_run(mocker, stdout=json.dumps(query_resp))
        graphql("query { threads }")
        assert cache.size() == 1
        mock.return_value.stdout = json.dumps(mutation_resp)
        graphql("mutation { resolve }")
        assert cache.size() == 0  # mutation cleared cache

    def test_different_variables_different_cache(self, mocker: MockerFixture):
        response = {"data": {}}
        mock = _patch_run(mocker, stdout=json.dumps(response))
        graphql("query($pr: Int!) { }", variables={"pr": 42})
        graphql("query($pr: Int!) { }", variables={"pr": 99})
        assert mock.call_count == 2  # different variables = different cache keys


class TestRest:
    def setup_method(self):
        cache.clear()

    def test_get(self, mocker: MockerFixture):
        response = [{"number": 1}]
        _patch_run(mocker, stdout=json.dumps(response))
        result = rest("/repos/owner/repo/pulls")
        assert result == response

    def test_empty_response(self, mocker: MockerFixture):
        _patch_run(mocker, stdout="  ")
        result = rest("/repos/owner/repo/pulls/1/merge", method="PUT")
        assert result is None

    def test_get_cached_on_second_call(self, mocker: MockerFixture):
        response = [{"number": 1}]
        mock = _patch_run(mocker, stdout=json.dumps(response))
        r1 = rest("/repos/owner/repo/pulls")
        r2 = rest("/repos/owner/repo/pulls")
        assert r1 == r2
        assert mock.call_count == 1

    def test_non_get_not_cached(self, mocker: MockerFixture):
        _patch_run(mocker, stdout="  ")
        rest("/repos/owner/repo/pulls/1/merge", method="PUT")
        assert cache.size() == 0  # PUT not cached

    def test_non_get_clears_cache(self, mocker: MockerFixture):
        get_resp = [{"number": 1}]
        mock = _patch_run(mocker, stdout=json.dumps(get_resp))
        rest("/repos/owner/repo/pulls")
        assert cache.size() == 1
        mock.return_value.stdout = "  "
        rest("/repos/owner/repo/pulls/1/merge", method="PUT")
        assert cache.size() == 0


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
