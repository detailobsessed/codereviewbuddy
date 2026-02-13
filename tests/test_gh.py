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
    _summarize_cmd,
    check_auth,
    get_current_pr_number,
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
    mocker.patch("codereviewbuddy.gh._log_gh_call")
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

    def test_paginate_flag_passed(self, mocker: MockerFixture):
        response = [{"sha": "abc"}, {"sha": "def"}]
        mock = _patch_run(mocker, stdout=json.dumps(response))
        result = rest("/repos/o/r/pulls/1/commits?per_page=100", paginate=True)
        assert result == response
        args = mock.call_args[0][0]
        assert "--paginate" in args
        assert "--slurp" in args

    def test_paginate_false_omits_flag(self, mocker: MockerFixture):
        response = [{"sha": "abc"}]
        mock = _patch_run(mocker, stdout=json.dumps(response))
        rest("/repos/o/r/pulls/1/commits")
        args = mock.call_args[0][0]
        assert "--paginate" not in args
        assert "--slurp" not in args

    def test_paginate_uses_separate_cache_key(self, mocker: MockerFixture):
        response = [{"sha": "abc"}]
        mock = _patch_run(mocker, stdout=json.dumps(response))
        rest("/repos/o/r/pulls/1/commits", paginate=False)
        rest("/repos/o/r/pulls/1/commits", paginate=True)
        assert mock.call_count == 2  # different cache keys

    def test_paginate_flattens_multi_page(self, mocker: MockerFixture):
        """--slurp wraps pages in outer array; rest() must flatten."""
        multi_page = [[{"sha": "a"}, {"sha": "b"}], [{"sha": "c"}]]
        _patch_run(mocker, stdout=json.dumps(multi_page))
        result = rest("/repos/o/r/pulls/1/commits", paginate=True)
        assert result == [{"sha": "a"}, {"sha": "b"}, {"sha": "c"}]

    def test_paginate_single_page_wrapped(self, mocker: MockerFixture):
        """--slurp wraps even a single page in an outer array; must flatten."""
        single_page_wrapped = [[{"sha": "a"}, {"sha": "b"}]]
        _patch_run(mocker, stdout=json.dumps(single_page_wrapped))
        result = rest("/repos/o/r/pulls/1/commits", paginate=True)
        assert result == [{"sha": "a"}, {"sha": "b"}]


class TestSummarizeCmd:
    def test_api_graphql(self):
        assert _summarize_cmd(("api", "graphql", "-f", "query=...")) == "api graphql"

    def test_pr_comment(self):
        assert _summarize_cmd(("pr", "comment", "42", "--repo", "o/r")) == "pr comment 42"

    def test_empty(self):
        assert _summarize_cmd(()) == "unknown"

    def test_flag_first(self):
        assert _summarize_cmd(("-f", "query=...")) == "unknown"


class TestRunGhLogging:
    def test_success_logs_call(self, mocker: MockerFixture):
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
        mocker.patch("codereviewbuddy.gh.subprocess.run", return_value=result)
        log_mock = mocker.patch("codereviewbuddy.gh._log_gh_call")
        run_gh("api", "graphql", "-f", "query=test")
        log_mock.assert_called_once()
        entry = log_mock.call_args[0][0]
        assert entry["cmd"] == "api graphql"
        assert entry["exit_code"] == 0
        assert entry["stdout_bytes"] == 2
        assert entry["duration_ms"] >= 0
        assert "ts" in entry
        assert "ts_end" in entry

    def test_failure_logs_stderr(self, mocker: MockerFixture):
        result = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="rate limit exceeded")
        mocker.patch("codereviewbuddy.gh.subprocess.run", return_value=result)
        log_mock = mocker.patch("codereviewbuddy.gh._log_gh_call")
        with pytest.raises(GhError):
            run_gh("api", "graphql")
        entry = log_mock.call_args[0][0]
        assert entry["exit_code"] == 1
        assert entry["stderr"] == "rate limit exceeded"

    def test_not_found_logs_error(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.gh.subprocess.run", side_effect=FileNotFoundError)
        log_mock = mocker.patch("codereviewbuddy.gh._log_gh_call")
        with pytest.raises(GhNotFoundError):
            run_gh("auth", "status")
        entry = log_mock.call_args[0][0]
        assert entry["error"] == "FileNotFoundError"
        assert entry["cmd"] == "auth status"

    def test_log_rotation_keeps_last_entries(self, mocker: MockerFixture, tmp_path):
        from codereviewbuddy import gh as gh_module

        log_file = tmp_path / "gh_calls.jsonl"
        mocker.patch("codereviewbuddy.gh._GH_LOG_DIR", tmp_path)
        mocker.patch("codereviewbuddy.gh._GH_LOG_FILE", log_file)
        mocker.patch("codereviewbuddy.gh._MAX_GH_LOG_LINES", 3)
        mocker.patch("codereviewbuddy.gh._GH_ROTATE_EVERY_WRITES", 1)
        mocker.patch("codereviewbuddy.gh._gh_log_state", {"write_count": 0})

        for i in range(5):
            gh_module._log_gh_call({"i": i})

        lines = log_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3
        assert [json.loads(line)["i"] for line in lines] == [2, 3, 4]


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


class TestGetCurrentPrNumber:
    def test_success(self, mocker: MockerFixture):
        _patch_run(mocker, stdout="42\n")
        assert get_current_pr_number() == 42

    def test_no_pr_for_branch(self, mocker: MockerFixture):
        _patch_run(mocker, stderr="no pull requests found", returncode=1)
        with pytest.raises(GhError, match="no pull requests found"):
            get_current_pr_number()


class TestGetRepoInfo:
    def test_success(self, mocker: MockerFixture):
        _patch_run(mocker, stdout="detailobsessed/codereviewbuddy\n")
        owner, repo = get_repo_info()
        assert owner == "detailobsessed"
        assert repo == "codereviewbuddy"
