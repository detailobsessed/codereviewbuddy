"""Tests for CI diagnosis tool."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

from codereviewbuddy.gh import GhError
from codereviewbuddy.tools.ci import (
    _clean_log_line,
    _extract_error_lines,
    _is_error_line,
    _is_noise,
    diagnose_ci,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RUN_LIST_SUCCESS = json.dumps([
    {
        "databaseId": 111,
        "status": "completed",
        "conclusion": "success",
        "name": "ci",
        "headBranch": "main",
        "url": "https://github.com/org/repo/actions/runs/111",
    },
])

_RUN_LIST_FAILURE = json.dumps([
    {
        "databaseId": 222,
        "status": "completed",
        "conclusion": "failure",
        "name": "ci",
        "headBranch": "feat-branch",
        "url": "https://github.com/org/repo/actions/runs/222",
    },
])

_RUN_DETAILS = json.dumps({
    "name": "ci",
    "headBranch": "feat-branch",
    "conclusion": "failure",
    "url": "https://github.com/org/repo/actions/runs/222",
    "jobs": [
        {
            "name": "test",
            "conclusion": "success",
            "steps": [],
        },
        {
            "name": "lint",
            "conclusion": "failure",
            "steps": [
                {"name": "Checkout", "conclusion": "success"},
                {"name": "Run ruff", "conclusion": "failure"},
            ],
        },
    ],
})

_RUN_DETAILS_MULTI_FAILURE = json.dumps({
    "name": "ci",
    "headBranch": "feat-branch",
    "conclusion": "failure",
    "url": "https://github.com/org/repo/actions/runs/222",
    "jobs": [
        {
            "name": "lint",
            "conclusion": "failure",
            "steps": [
                {"name": "Checkout", "conclusion": "success"},
                {"name": "Run ruff", "conclusion": "failure"},
            ],
        },
        {
            "name": "test",
            "conclusion": "failure",
            "steps": [
                {"name": "Checkout", "conclusion": "success"},
                {"name": "Run pytest", "conclusion": "failure"},
            ],
        },
    ],
})

_FAILED_LOGS_MULTI = """\
lint    UNKNOWN STEP    2026-02-19T08:30:36.6504054Z lint: src/app.py:10:1: E302 expected 2 blank lines
lint    UNKNOWN STEP    2026-02-19T08:30:36.6506191Z lint: Found 1 error.
test    UNKNOWN STEP    2026-02-19T08:31:36.6504054Z test: FAILED tests/test_app.py::test_main
test    UNKNOWN STEP    2026-02-19T08:31:36.6506191Z test: AssertionError: expected 1 got 2
"""

_RUN_DETAILS_NO_FAILED_JOBS = json.dumps({
    "name": "ci",
    "headBranch": "feat-branch",
    "conclusion": "failure",
    "url": "https://github.com/org/repo/actions/runs/222",
    "jobs": [
        {"name": "test", "conclusion": "success", "steps": []},
    ],
})

_FAILED_LOGS = """\
lint    UNKNOWN STEP    2026-02-19T08:30:36.3494085Z ##[group]Run ruff
lint    UNKNOWN STEP    2026-02-19T08:30:36.6504054Z src/app.py:10:1: E302 expected 2 blank lines
lint    UNKNOWN STEP    2026-02-19T08:30:36.6505056Z src/app.py:25:80: E501 line too long
lint    UNKNOWN STEP    2026-02-19T08:30:36.6506191Z Found 2 errors.
lint    UNKNOWN STEP    2026-02-19T08:33:36.6946723Z ##[error]Process completed with exit code 1.
lint    UNKNOWN STEP    2026-02-19T08:33:36.7034513Z Post job cleanup.
lint    UNKNOWN STEP    2026-02-19T08:33:36.8301351Z Cleaning up orphan processes
"""


def _mock_gh(mocker: MockerFixture, side_effects: list[str]) -> None:
    """Mock gh.run_gh to return successive values."""
    mocker.patch("codereviewbuddy.tools.ci.gh.run_gh", side_effect=side_effects)


# ---------------------------------------------------------------------------
# Unit tests for helpers
# ---------------------------------------------------------------------------


class TestIsNoise:
    def test_group_markers(self):
        assert _is_noise("##[group]Run ruff")
        assert _is_noise("##[endgroup]")

    def test_post_job_cleanup(self):
        assert _is_noise("Post job cleanup.")

    def test_cleaning_up(self):
        assert _is_noise("Cleaning up orphan processes")

    def test_shell_line(self):
        assert _is_noise("  shell: /usr/bin/bash --noprofile")

    def test_normal_line(self):
        assert not _is_noise("src/app.py:10:1: E302 expected 2 blank lines")


class TestIsErrorLine:
    def test_github_error_annotation(self):
        assert _is_error_line("##[error]Process completed with exit code 1.")

    def test_error_word(self):
        assert _is_error_line("Found 2 errors.")

    def test_failed_word(self):
        assert _is_error_line("Tests Failed")

    def test_exit_code(self):
        assert _is_error_line("exit code 1")

    def test_normal_line(self):
        assert not _is_error_line("All checks passed")


class TestCleanLogLine:
    def test_strips_job_prefix_and_timestamp(self):
        raw = "lint    UNKNOWN STEP    2026-02-19T08:30:36.6504054Z src/app.py:10:1: E302"
        assert _clean_log_line(raw) == "src/app.py:10:1: E302"

    def test_plain_line(self):
        assert _clean_log_line("hello world") == "hello world"


class TestExtractErrorLines:
    def test_extracts_errors_from_logs(self):
        lines = _extract_error_lines(_FAILED_LOGS)
        assert len(lines) > 0
        assert any("error" in line.lower() or "Error" in line for line in lines)

    def test_empty_logs(self):
        assert _extract_error_lines("") == []

    def test_no_errors(self):
        assert _extract_error_lines("All checks passed\nDone\n") == []

    def test_strips_noise(self):
        lines = _extract_error_lines(_FAILED_LOGS)
        for line in lines:
            if line == "---":
                continue
            assert not _is_noise(line)


# ---------------------------------------------------------------------------
# Integration tests for diagnose_ci
# ---------------------------------------------------------------------------


class TestDiagnoseCI:
    def test_no_failed_runs(self, mocker: MockerFixture):
        _mock_gh(mocker, [_RUN_LIST_SUCCESS])
        result = diagnose_ci(repo="org/repo")
        assert result.error == "No failed workflow runs found."

    def test_finds_and_diagnoses_failure(self, mocker: MockerFixture):
        _mock_gh(mocker, [_RUN_LIST_FAILURE, _RUN_DETAILS, _FAILED_LOGS])
        result = diagnose_ci(repo="org/repo")
        assert result.run_id == 222
        assert result.workflow == "ci"
        assert result.branch == "feat-branch"
        assert result.conclusion == "failure"
        assert len(result.failures) == 1
        assert result.failures[0].job_name == "lint"
        assert result.failures[0].failed_step == "Run ruff"
        assert len(result.failures[0].error_lines) > 0
        assert result.error is None

    def test_specific_run_id(self, mocker: MockerFixture):
        _mock_gh(mocker, [_RUN_DETAILS, _FAILED_LOGS])
        result = diagnose_ci(run_id=222, repo="org/repo")
        assert result.run_id == 222
        assert result.failures[0].job_name == "lint"

    def test_pr_number_resolves_branch(self, mocker: MockerFixture):
        _mock_gh(mocker, ["feat-branch\n", _RUN_LIST_FAILURE, _RUN_DETAILS, _FAILED_LOGS])
        result = diagnose_ci(pr_number=42, repo="org/repo")
        assert result.run_id == 222

    def test_no_failed_jobs_in_run(self, mocker: MockerFixture):
        _mock_gh(mocker, [_RUN_LIST_FAILURE, _RUN_DETAILS_NO_FAILED_JOBS])
        result = diagnose_ci(repo="org/repo")
        assert result.error is not None
        assert "no individual jobs failed" in result.error.lower()

    def test_run_details_gh_error(self, mocker: MockerFixture):
        mocker.patch(
            "codereviewbuddy.tools.ci.gh.run_gh",
            side_effect=[_RUN_LIST_FAILURE, GhError("API rate limited")],
        )
        result = diagnose_ci(repo="org/repo")
        assert result.run_id == 222
        assert result.error is not None
        assert "rate limited" in result.error.lower()

    def test_log_fetch_fails_gracefully(self, mocker: MockerFixture):
        mocker.patch(
            "codereviewbuddy.tools.ci.gh.run_gh",
            side_effect=[_RUN_LIST_FAILURE, _RUN_DETAILS, GhError("log fetch failed")],
        )
        result = diagnose_ci(repo="org/repo")
        assert result.run_id == 222
        assert len(result.failures) == 1
        assert result.failures[0].error_lines == []
        assert result.error is None

    def test_multi_job_failure_splits_errors(self, mocker: MockerFixture):
        _mock_gh(mocker, [_RUN_LIST_FAILURE, _RUN_DETAILS_MULTI_FAILURE, _FAILED_LOGS_MULTI])
        result = diagnose_ci(repo="org/repo")
        assert len(result.failures) == 2
        lint_failure = next(f for f in result.failures if f.job_name == "lint")
        test_failure = next(f for f in result.failures if f.job_name == "test")
        assert lint_failure.failed_step == "Run ruff"
        assert test_failure.failed_step == "Run pytest"
        assert any("lint" in line.lower() for line in lint_failure.error_lines)
        assert any("test" in line.lower() for line in test_failure.error_lines)

    def test_without_repo_param(self, mocker: MockerFixture):
        _mock_gh(mocker, [_RUN_LIST_FAILURE, _RUN_DETAILS, _FAILED_LOGS])
        result = diagnose_ci(cwd="/some/repo")
        assert result.run_id == 222
        assert result.error is None


class TestExtractErrorLinesEdgeCases:
    def test_truncates_long_logs(self):
        long_log = "normal line\n" * 2500 + "##[error]Something failed\n"
        lines = _extract_error_lines(long_log)
        assert len(lines) > 0
        assert any("failed" in line.lower() for line in lines)

    def test_separator_between_error_groups(self):
        log = (
            "ok line 1\n"
            "ok line 2\n"
            "ok line 3\n"
            "##[error]First error\n"
            "ok line 4\n"
            "ok line 5\n" + "clean line\n" * 10 + "##[error]Second error\n"
            "ok line 6\n"
        )
        lines = _extract_error_lines(log)
        assert "---" in lines
