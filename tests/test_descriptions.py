"""Tests for PR description review and update tools."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

from codereviewbuddy.config import Config, PRDescriptionsConfig, set_config
from codereviewbuddy.tools.descriptions import (
    _analyze_pr,
    _is_boilerplate,
    review_pr_descriptions,
)

# -- Fixtures ------------------------------------------------------------------

GOOD_PR = {
    "number": 42,
    "title": "feat: add PR description tools",
    "body": (
        "## Summary\n\nAdd tools for reviewing and updating PR descriptions."
        "\n\nCloses #15\n\n## Changes\n\n- Added review_pr_descriptions\n- Added update_pr_description"
    ),
    "url": "https://github.com/owner/repo/pull/42",
}

EMPTY_PR = {
    "number": 10,
    "title": "fix: something",
    "body": "",
    "url": "https://github.com/owner/repo/pull/10",
}

BOILERPLATE_PR = {
    "number": 20,
    "title": "chore: cleanup",
    "body": (
        "## Description\n\n<!-- Brief description of what this PR does -->\n\n"
        "## Checklist\n\n- [ ] Tests added/updated\n- [ ] Documentation updated (if applicable)\n"
        "- [ ] `poe check` passes\n"
        "- [ ] Commit messages follow [conventional commits](../CONTRIBUTING.md#commit-message-convention)\n\n"
        "## Related Issues\n\n<!-- Link related issues: Fixes #123, Related to #456 -->\n"
    ),
    "url": "https://github.com/owner/repo/pull/20",
}

SHORT_PR = {
    "number": 30,
    "title": "docs: typo fix",
    "body": "Fixed a typo in README.",
    "url": "https://github.com/owner/repo/pull/30",
}


# -- Unit tests: _is_boilerplate -----------------------------------------------


class TestIsBoilerplate:
    def test_empty_string_is_boilerplate(self):
        assert _is_boilerplate("") is True

    def test_whitespace_only_is_boilerplate(self):
        assert _is_boilerplate("   \n\n  ") is True

    def test_template_checklist_is_boilerplate(self):
        assert _is_boilerplate(str(BOILERPLATE_PR["body"])) is True

    def test_real_description_is_not_boilerplate(self):
        assert _is_boilerplate(str(GOOD_PR["body"])) is False

    def test_short_description_is_not_boilerplate(self):
        assert _is_boilerplate("Fixed a typo in README.") is False

    def test_unchecked_checkbox_boilerplate(self):
        body = (
            "<!-- Brief description -->\n"
            "## Checklist\n"
            "- [ ] Tests added\n"
            "- [ ] Documentation updated\n"
            "- [ ] Commit messages follow convention\n"
            "<!-- Link related issues -->\n"
        )
        assert _is_boilerplate(body) is True

    def test_multiword_headings_stripped(self):
        body = "## Related Issues\n\n## Breaking Changes\n\n## Additional Notes\n\n"
        assert _is_boilerplate(body) is True


# -- Unit tests: _analyze_pr ---------------------------------------------------


class TestAnalyzePR:
    def test_good_pr(self):
        info = _analyze_pr(GOOD_PR)
        assert info.pr_number == 42
        assert info.has_body is True
        assert info.is_boilerplate is False
        assert "#15" in info.linked_issues
        assert info.missing_elements == []

    def test_empty_pr(self):
        info = _analyze_pr(EMPTY_PR)
        assert info.has_body is False
        assert "empty body" in info.missing_elements
        assert "no linked issues" in info.missing_elements

    def test_boilerplate_pr(self):
        info = _analyze_pr(BOILERPLATE_PR)
        assert info.has_body is True
        assert info.is_boilerplate is True
        assert "body is template boilerplate only" in info.missing_elements
        assert info.linked_issues == []  # HTML comment placeholders should not count
        assert "no linked issues" in info.missing_elements

    def test_short_pr_no_issues(self):
        info = _analyze_pr(SHORT_PR)
        assert info.has_body is True
        assert info.is_boilerplate is False
        assert "no linked issues" in info.missing_elements
        assert "description is very short" in info.missing_elements

    def test_multiple_issue_refs(self):
        data = {
            "number": 1,
            "title": "feat: big change",
            "body": "Closes #10, fixes #20, related to org/repo#30",
            "url": "",
        }
        info = _analyze_pr(data)
        assert "#10" in info.linked_issues
        assert "#20" in info.linked_issues
        assert "#30" in info.linked_issues


# -- Async tests: review_pr_descriptions --------------------------------------


class TestReviewPRDescriptions:
    @pytest.fixture(autouse=True)
    def _reset_config(self):
        set_config(Config())
        yield
        set_config(Config())

    async def test_reviews_multiple_prs(self, mocker: MockerFixture):
        mocker.patch(
            "codereviewbuddy.tools.descriptions._fetch_pr_info",
            side_effect=[GOOD_PR, EMPTY_PR],
        )
        result = await review_pr_descriptions([42, 10])
        assert result.error is None
        assert len(result.descriptions) == 2
        assert result.descriptions[0].pr_number == 42
        assert result.descriptions[1].pr_number == 10
        assert result.descriptions[1].has_body is False

    async def test_reports_progress(self, mocker: MockerFixture):
        mocker.patch(
            "codereviewbuddy.tools.descriptions._fetch_pr_info",
            return_value=GOOD_PR,
        )
        ctx = AsyncMock()
        result = await review_pr_descriptions([42], ctx=ctx)
        assert result.error is None
        ctx.report_progress.assert_any_call(0, 1)
        ctx.report_progress.assert_any_call(1, 1)
        ctx.info.assert_called_once()

    async def test_disabled_returns_error(self):
        set_config(Config(pr_descriptions=PRDescriptionsConfig(enabled=False)))
        result = await review_pr_descriptions([42])
        assert result.error is not None
        assert "disabled" in result.error
