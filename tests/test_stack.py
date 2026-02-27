"""Tests for stack discovery and review status summarization."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


from codereviewbuddy.models import StackPR
from codereviewbuddy.tools.stack import (
    _build_stack,
    _classify_severity,
    _fetch_merged_prs,
    _fetch_pr_summary,
    discover_stack,
    list_recent_unresolved,
    summarize_review_status,
)

# -- Sample PR data -----------------------------------------------------------

SAMPLE_PRS = [
    {"number": 73, "title": "feat: base PR", "headRefName": "feat/base", "baseRefName": "main", "url": "https://github.com/o/r/pull/73"},
    {
        "number": 74,
        "title": "feat: middle PR",
        "headRefName": "feat/middle",
        "baseRefName": "feat/base",
        "url": "https://github.com/o/r/pull/74",
    },
    {"number": 80, "title": "fix: top PR", "headRefName": "fix/top", "baseRefName": "feat/middle", "url": "https://github.com/o/r/pull/80"},
    {
        "number": 99,
        "title": "chore: unrelated",
        "headRefName": "chore/other",
        "baseRefName": "main",
        "url": "https://github.com/o/r/pull/99",
    },
]


# -- _build_stack tests -------------------------------------------------------


class TestBuildStack:
    def test_finds_full_stack_from_middle(self):
        stack = _build_stack(74, SAMPLE_PRS)
        assert len(stack) == 3
        assert [p.pr_number for p in stack] == [73, 74, 80]

    def test_finds_full_stack_from_bottom(self):
        stack = _build_stack(73, SAMPLE_PRS)
        assert len(stack) == 3
        assert [p.pr_number for p in stack] == [73, 74, 80]

    def test_finds_full_stack_from_top(self):
        stack = _build_stack(80, SAMPLE_PRS)
        assert len(stack) == 3
        assert [p.pr_number for p in stack] == [73, 74, 80]

    def test_single_pr_not_in_stack(self):
        stack = _build_stack(99, SAMPLE_PRS)
        assert len(stack) == 1
        assert stack[0].pr_number == 99

    def test_unknown_pr_returns_empty(self):
        stack = _build_stack(999, SAMPLE_PRS)
        assert stack == []

    def test_empty_prs_returns_empty(self):
        stack = _build_stack(73, [])
        assert stack == []

    def test_returns_stack_pr_models(self):
        stack = _build_stack(73, SAMPLE_PRS)
        for pr in stack:
            assert isinstance(pr, StackPR)
        assert stack[0].title == "feat: base PR"
        assert stack[0].branch == "feat/base"
        assert stack[0].url == "https://github.com/o/r/pull/73"


# -- discover_stack tests -----------------------------------------------------


class TestDiscoverStack:
    async def test_discovers_and_caches(self, mocker: MockerFixture):
        mocker.patch(
            "codereviewbuddy.tools.stack._fetch_open_prs",
            return_value=SAMPLE_PRS,
        )
        ctx = AsyncMock()
        ctx.get_state = AsyncMock(return_value=None)
        ctx.set_state = AsyncMock()
        ctx.info = AsyncMock()

        stack = await discover_stack(74, repo="o/r", ctx=ctx)
        assert len(stack) == 3
        assert [p.pr_number for p in stack] == [73, 74, 80]
        # Should cache the result
        ctx.set_state.assert_called_once()
        assert ctx.set_state.call_args[0][0] == "stack_prs"

    async def test_uses_cache_on_second_call(self, mocker: MockerFixture):
        cached = [
            {"pr_number": 73, "branch": "feat/base", "title": "feat: base PR", "url": ""},
            {"pr_number": 74, "branch": "feat/middle", "title": "feat: middle PR", "url": ""},
        ]
        mock_fetch = mocker.patch("codereviewbuddy.tools.stack._fetch_open_prs")
        ctx = AsyncMock()
        ctx.get_state = AsyncMock(return_value=cached)

        stack = await discover_stack(74, repo="o/r", ctx=ctx)
        assert len(stack) == 2
        mock_fetch.assert_not_called()

    async def test_works_without_ctx(self, mocker: MockerFixture):
        mocker.patch(
            "codereviewbuddy.tools.stack._fetch_open_prs",
            return_value=SAMPLE_PRS,
        )
        stack = await discover_stack(74, repo="o/r")
        assert len(stack) == 3


# -- _classify_severity tests -------------------------------------------------

SAMPLE_SUMMARY_GRAPHQL_RESPONSE = {
    "data": {
        "repository": {
            "pullRequest": {
                "title": "feat: test",
                "url": "https://github.com/o/r/pull/42",
                "reviewThreads": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {
                            "isResolved": False,
                            "isOutdated": True,
                            "comments": {
                                "nodes": [
                                    {
                                        "author": {"login": "devin-ai-integration[bot]"},
                                        "body": "🔴 **Bug:** something broken",
                                        "path": "a.py",
                                        "createdAt": "2026-02-10T10:00:00Z",
                                    },
                                ],
                            },
                        },
                        {
                            "isResolved": False,
                            "isOutdated": True,
                            "comments": {
                                "nodes": [
                                    {
                                        "author": {"login": "devin-ai-integration[bot]"},
                                        "body": "🟡 Consider refactoring",
                                        "path": "b.py",
                                        "createdAt": "2026-02-10T10:00:00Z",
                                    },
                                ],
                            },
                        },
                        {
                            "isResolved": True,
                            "isOutdated": True,
                            "comments": {
                                "nodes": [
                                    {
                                        "author": {"login": "devin-ai-integration[bot]"},
                                        "body": "📝 Looks good",
                                        "path": "c.py",
                                        "createdAt": "2026-02-10T10:00:00Z",
                                    },
                                ],
                            },
                        },
                    ],
                },
            }
        }
    },
}


class TestClassifySeverity:
    def test_unknown_reviewer_returns_info(self):
        from codereviewbuddy.config import Severity

        assert _classify_severity("unknown", "anything") == Severity.INFO

    def test_devin_bug(self):
        from codereviewbuddy.config import Severity

        assert _classify_severity("devin", "🔴 **Bug:** something broken") == Severity.BUG

    def test_devin_warning(self):
        from codereviewbuddy.config import Severity

        assert _classify_severity("devin", "🟡 Consider refactoring") == Severity.WARNING


class TestFetchPrSummary:
    async def test_counts_severity(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.stack.github_api.graphql", new_callable=AsyncMock, return_value=SAMPLE_SUMMARY_GRAPHQL_RESPONSE)

        summary = await _fetch_pr_summary("o", "r", 42)
        assert summary.pr_number == 42
        assert summary.title == "feat: test"
        assert summary.unresolved == 2
        assert summary.resolved == 1
        assert summary.bugs == 1
        assert summary.warnings == 1

    async def test_skips_disabled_reviewers(self, mocker: MockerFixture):
        from codereviewbuddy.config import Config, ReviewerConfig, set_config

        set_config(Config(reviewers={"devin": ReviewerConfig(enabled=False)}))
        try:
            mocker.patch(
                "codereviewbuddy.tools.stack.github_api.graphql",
                new_callable=AsyncMock,
                return_value=SAMPLE_SUMMARY_GRAPHQL_RESPONSE,
            )

            summary = await _fetch_pr_summary("o", "r", 42)
            assert summary.unresolved == 0
            assert summary.resolved == 0
        finally:
            set_config(Config())

    async def test_stale_count_excludes_resolved_threads(self, mocker: MockerFixture):
        """Regression (#94): stale count must only include unresolved threads."""
        mocker.patch("codereviewbuddy.tools.stack.github_api.graphql", new_callable=AsyncMock, return_value=SAMPLE_SUMMARY_GRAPHQL_RESPONSE)

        summary = await _fetch_pr_summary("o", "r", 42)
        # Sample data: all 3 threads have isOutdated=True, but only 2 are unresolved.
        assert summary.stale == 2


class TestSummarizeReviewStatus:
    async def test_with_explicit_pr_numbers(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.stack.github_api.graphql", new_callable=AsyncMock, return_value=SAMPLE_SUMMARY_GRAPHQL_RESPONSE)
        mocker.patch("codereviewbuddy.tools.stack.gh.get_repo_info", return_value=("o", "r"))

        result = await summarize_review_status(pr_numbers=[42])
        assert result.error is None
        assert len(result.prs) == 1
        assert result.prs[0].pr_number == 42
        assert result.total_unresolved == 2

    async def test_auto_discovers_stack(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.stack.github_api.graphql", new_callable=AsyncMock, return_value=SAMPLE_SUMMARY_GRAPHQL_RESPONSE)
        mocker.patch("codereviewbuddy.tools.stack.gh.get_repo_info", return_value=("o", "r"))
        mocker.patch("codereviewbuddy.tools.stack.gh.get_current_pr_number", return_value=74)
        mocker.patch("codereviewbuddy.tools.stack._fetch_open_prs", return_value=SAMPLE_PRS)

        result = await summarize_review_status(repo="o/r")
        assert result.error is None
        # Stack has 3 PRs (73, 74, 80)
        assert len(result.prs) == 3

    async def test_auto_discovery_repo_mismatch_returns_error(self, mocker: MockerFixture):
        """Regression (#115): when repo is explicitly provided but cwd is a different repo,
        auto-discovery must fail with a clear error instead of silently returning empty."""
        mocker.patch("codereviewbuddy.tools.stack.gh.get_repo_info", return_value=("other_owner", "other_repo"))

        result = await summarize_review_status(repo="o/r")
        assert result.error is not None
        assert "Auto-discovery unavailable" in result.error
        assert "other_owner/other_repo" in result.error
        assert "o/r" in result.error

    async def test_auto_discovery_gh_error_returns_mismatch(self, mocker: MockerFixture):
        """When get_repo_info raises GhError, treat cwd as unknown and report mismatch."""
        from codereviewbuddy.gh import GhError

        mocker.patch("codereviewbuddy.tools.stack.gh.get_repo_info", side_effect=GhError("not a repo"))

        result = await summarize_review_status(repo="o/r")
        assert result.error is not None
        assert "Auto-discovery unavailable" in result.error
        assert "unknown" in result.error

    async def test_empty_pr_list(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.stack.gh.get_repo_info", return_value=("o", "r"))
        result = await summarize_review_status(pr_numbers=[])
        assert result.error is not None
        assert "No PRs" in result.error


# -- list_recent_unresolved tests --------------------------------------------

SAMPLE_MERGED_PRS = [
    {"number": 176, "title": "build: copier update", "url": "https://github.com/o/r/pull/176", "mergedAt": "2026-02-18T09:00:00Z"},
    {"number": 177, "title": "feat: install command", "url": "https://github.com/o/r/pull/177", "mergedAt": "2026-02-18T09:01:00Z"},
]

# Response with zero unresolved threads
SAMPLE_CLEAN_GRAPHQL_RESPONSE = {
    "data": {
        "repository": {
            "pullRequest": {
                "title": "build: copier update",
                "url": "https://github.com/o/r/pull/176",
                "reviewThreads": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [],
                },
            }
        }
    },
}


class TestListRecentUnresolved:
    async def test_returns_only_prs_with_unresolved(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.stack._fetch_merged_prs", new_callable=AsyncMock, return_value=SAMPLE_MERGED_PRS)
        mocker.patch("codereviewbuddy.tools.stack.gh.get_repo_info", return_value=("o", "r"))
        # PR 176 has no unresolved, PR 177 has 2 unresolved
        mocker.patch(
            "codereviewbuddy.tools.stack.github_api.graphql",
            new_callable=AsyncMock,
            side_effect=[SAMPLE_CLEAN_GRAPHQL_RESPONSE, SAMPLE_SUMMARY_GRAPHQL_RESPONSE],
        )

        result = await list_recent_unresolved(repo="o/r", limit=5)
        assert result.error is None
        assert len(result.prs) == 1
        assert result.prs[0].unresolved == 2
        assert result.total_unresolved == 2

    async def test_empty_when_no_merged_prs(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.stack._fetch_merged_prs", new_callable=AsyncMock, return_value=[])
        mocker.patch("codereviewbuddy.tools.stack.gh.get_repo_info", return_value=("o", "r"))

        result = await list_recent_unresolved(repo="o/r")
        assert result.error is None
        assert result.prs == []
        assert result.total_unresolved == 0

    async def test_empty_when_all_resolved(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.stack._fetch_merged_prs", new_callable=AsyncMock, return_value=SAMPLE_MERGED_PRS)
        mocker.patch("codereviewbuddy.tools.stack.gh.get_repo_info", return_value=("o", "r"))
        mocker.patch("codereviewbuddy.tools.stack.github_api.graphql", new_callable=AsyncMock, return_value=SAMPLE_CLEAN_GRAPHQL_RESPONSE)

        result = await list_recent_unresolved(repo="o/r")
        assert result.prs == []
        assert result.total_unresolved == 0

    async def test_auto_detects_repo(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.stack._fetch_merged_prs", new_callable=AsyncMock, return_value=SAMPLE_MERGED_PRS)
        mocker.patch("codereviewbuddy.tools.stack.gh.get_repo_info", return_value=("o", "r"))
        mocker.patch("codereviewbuddy.tools.stack.github_api.graphql", new_callable=AsyncMock, return_value=SAMPLE_CLEAN_GRAPHQL_RESPONSE)

        result = await list_recent_unresolved()  # no repo arg
        assert result.error is None

    async def test_reports_progress_with_ctx(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.stack._fetch_merged_prs", new_callable=AsyncMock, return_value=SAMPLE_MERGED_PRS)
        mocker.patch("codereviewbuddy.tools.stack.gh.get_repo_info", return_value=("o", "r"))
        mocker.patch("codereviewbuddy.tools.stack.github_api.graphql", new_callable=AsyncMock, return_value=SAMPLE_SUMMARY_GRAPHQL_RESPONSE)

        ctx = AsyncMock()
        result = await list_recent_unresolved(repo="o/r", ctx=ctx)
        assert result.total_unresolved > 0
        # Progress reported: once per PR + final
        assert ctx.report_progress.await_count == len(SAMPLE_MERGED_PRS) + 1

    async def test_fetch_merged_prs_passes_repo_and_limit(self, mocker: MockerFixture):
        mock_rest = mocker.patch("codereviewbuddy.tools.stack.github_api.rest", new_callable=AsyncMock, return_value=[])
        await _fetch_merged_prs(repo="o/r", limit=5)
        mock_rest.assert_called_once()
        url = mock_rest.call_args.args[0]
        assert "/repos/o/r/pulls" in url
        assert "per_page=5" in url or "5" in str(mock_rest.call_args)

    async def test_fetch_merged_prs_clamps_negative_to_one(self, mocker: MockerFixture):
        mock_rest = mocker.patch("codereviewbuddy.tools.stack.github_api.rest", new_callable=AsyncMock, return_value=[])
        await _fetch_merged_prs(repo="o/r", limit=-5)
        url = mock_rest.call_args.args[0]
        assert "per_page=1" in url

    async def test_fetch_merged_prs_clamps_zero_to_one(self, mocker: MockerFixture):
        mock_rest = mocker.patch("codereviewbuddy.tools.stack.github_api.rest", new_callable=AsyncMock, return_value=[])
        await _fetch_merged_prs(repo="o/r", limit=0)
        url = mock_rest.call_args.args[0]
        assert "per_page=1" in url

    async def test_fetch_merged_prs_caps_at_max(self, mocker: MockerFixture):
        mock_rest = mocker.patch("codereviewbuddy.tools.stack.github_api.rest", new_callable=AsyncMock, return_value=[])
        await _fetch_merged_prs(repo="o/r", limit=100)
        url = mock_rest.call_args.args[0]
        assert "per_page=50" in url
