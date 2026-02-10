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
    _fetch_pr_summary,
    discover_stack,
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
    def test_counts_severity(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.stack.gh.graphql", return_value=SAMPLE_SUMMARY_GRAPHQL_RESPONSE)
        mocker.patch("codereviewbuddy.tools.comments._compute_staleness")

        summary = _fetch_pr_summary("o", "r", 42, commits=[])
        assert summary.pr_number == 42
        assert summary.title == "feat: test"
        assert summary.unresolved == 2
        assert summary.resolved == 1
        assert summary.bugs == 1
        assert summary.warnings == 1

    def test_reviews_in_progress_when_push_after_review(self, mocker: MockerFixture):
        """Regression: createdAt must be in the GraphQL query for status detection."""

        mocker.patch("codereviewbuddy.tools.stack.gh.graphql", return_value=SAMPLE_SUMMARY_GRAPHQL_RESPONSE)
        mocker.patch("codereviewbuddy.tools.comments._compute_staleness")
        # Push happened AFTER the review comments (10:00) → reviews_in_progress=True
        commits = [{"sha": "abc", "commit": {"committer": {"date": "2026-02-10T12:00:00Z"}}}]

        summary = _fetch_pr_summary("o", "r", 42, commits=commits)
        assert summary.reviews_in_progress is True

    def test_skips_disabled_reviewers(self, mocker: MockerFixture):
        from codereviewbuddy.config import Config, ReviewerConfig, set_config

        set_config(Config(reviewers={"devin": ReviewerConfig(enabled=False)}))
        try:
            mocker.patch("codereviewbuddy.tools.stack.gh.graphql", return_value=SAMPLE_SUMMARY_GRAPHQL_RESPONSE)
            mocker.patch("codereviewbuddy.tools.comments._compute_staleness")

            summary = _fetch_pr_summary("o", "r", 42, commits=[])
            assert summary.unresolved == 0
            assert summary.resolved == 0
        finally:
            set_config(Config())

    def test_stale_count_excludes_resolved_threads(self, mocker: MockerFixture):
        """Regression (#94): stale count must only include unresolved threads."""

        def mark_all_stale(threads, _commits, _owner, _repo, **_kw):
            for t in threads:
                t.is_stale = True

        mocker.patch("codereviewbuddy.tools.comments._compute_staleness", side_effect=mark_all_stale)
        mocker.patch("codereviewbuddy.tools.stack.gh.graphql", return_value=SAMPLE_SUMMARY_GRAPHQL_RESPONSE)

        summary = _fetch_pr_summary("o", "r", 42, commits=[])
        # Sample data: 2 unresolved + 1 resolved. All marked stale.
        # Only the 2 unresolved should count.
        assert summary.stale == 2

    def test_each_thread_gets_own_file_path(self, mocker: MockerFixture):
        """Regression: mini_threads must read file path per-node, not reuse a leaked variable."""
        staleness_mock = mocker.patch("codereviewbuddy.tools.comments._compute_staleness")
        mocker.patch("codereviewbuddy.tools.stack.gh.graphql", return_value=SAMPLE_SUMMARY_GRAPHQL_RESPONSE)

        _fetch_pr_summary("o", "r", 42, commits=[])

        # _compute_staleness is called with mini_threads — verify file paths are per-thread
        mini_threads = staleness_mock.call_args[0][0]
        files = [t.file for t in mini_threads]
        assert files == ["a.py", "b.py", "c.py"]


class TestSummarizeReviewStatus:
    async def test_with_explicit_pr_numbers(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.stack.gh.graphql", return_value=SAMPLE_SUMMARY_GRAPHQL_RESPONSE)
        mocker.patch("codereviewbuddy.tools.stack.gh.get_repo_info", return_value=("o", "r"))
        mocker.patch("codereviewbuddy.tools.comments._compute_staleness")
        mocker.patch("codereviewbuddy.tools.comments._get_pr_commits", return_value=[])

        result = await summarize_review_status(pr_numbers=[42])
        assert result.error is None
        assert len(result.prs) == 1
        assert result.prs[0].pr_number == 42
        assert result.total_unresolved == 2

    async def test_auto_discovers_stack(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.stack.gh.graphql", return_value=SAMPLE_SUMMARY_GRAPHQL_RESPONSE)
        mocker.patch("codereviewbuddy.tools.stack.gh.get_repo_info", return_value=("o", "r"))
        mocker.patch("codereviewbuddy.tools.stack.gh.get_current_pr_number", return_value=74)
        mocker.patch("codereviewbuddy.tools.stack._fetch_open_prs", return_value=SAMPLE_PRS)
        mocker.patch("codereviewbuddy.tools.comments._compute_staleness")
        mocker.patch("codereviewbuddy.tools.comments._get_pr_commits", return_value=[])

        result = await summarize_review_status(repo="o/r")
        assert result.error is None
        # Stack has 3 PRs (73, 74, 80)
        assert len(result.prs) == 3

    async def test_empty_pr_list(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.stack.gh.get_repo_info", return_value=("o", "r"))
        result = await summarize_review_status(pr_numbers=[])
        assert result.error is not None
        assert "No PRs" in result.error
