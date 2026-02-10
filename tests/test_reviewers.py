"""Tests for reviewer adapters."""

from __future__ import annotations

import pytest

from codereviewbuddy.config import ReviewerConfig, Severity
from codereviewbuddy.reviewers import (
    REVIEWERS,
    CodeRabbitAdapter,
    DevinAdapter,
    UnblockedAdapter,
    get_reviewer,
    identify_reviewer,
)


class TestIdentifyReviewer:
    @pytest.mark.parametrize(
        ("author", "expected"),
        [
            ("unblocked[bot]", "unblocked"),
            ("unblocked-bot", "unblocked"),
            ("Unblocked[bot]", "unblocked"),
            ("devin-ai-integration[bot]", "devin"),
            ("devin-ai", "devin"),
            ("Devin", "devin"),
            ("coderabbitai[bot]", "coderabbit"),
            ("CodeRabbit", "coderabbit"),
            ("randomuser", "unknown"),
            ("github-actions[bot]", "unknown"),
        ],
    )
    def test_identify(self, author: str, expected: str):
        assert identify_reviewer(author) == expected


class TestGetReviewer:
    def test_known_reviewer(self):
        adapter = get_reviewer("unblocked")
        assert adapter is not None
        assert isinstance(adapter, UnblockedAdapter)

    def test_unknown_reviewer(self):
        assert get_reviewer("nonexistent") is None


class TestUnblockedAdapter:
    def test_properties(self):
        adapter = UnblockedAdapter()
        assert adapter.name == "unblocked"
        assert adapter.needs_manual_rereview is True
        assert adapter.auto_resolves_comments is False

    def test_rereview_trigger_default_message(self):
        adapter = UnblockedAdapter()
        args = adapter.rereview_trigger(42, "owner", "repo")
        assert args[0] == "pr"
        assert "42" in args
        assert "--body" in args
        assert "@unblocked please re-review" in args

    def test_rereview_trigger_custom_message(self):
        adapter = UnblockedAdapter()
        adapter.configure(ReviewerConfig(rereview_message="@unblocked re-review please, with context"))
        args = adapter.rereview_trigger(42, "owner", "repo")
        assert "@unblocked re-review please, with context" in args

    def test_rereview_trigger_config_without_message_uses_default(self):
        adapter = UnblockedAdapter()
        adapter.configure(ReviewerConfig())
        args = adapter.rereview_trigger(42, "owner", "repo")
        assert "@unblocked please re-review" in args


class TestDevinAdapter:
    def test_properties(self):
        adapter = DevinAdapter()
        assert adapter.name == "devin"
        assert adapter.needs_manual_rereview is False
        assert adapter.auto_resolves_comments is True

    def test_auto_resolves_bug_thread(self):
        adapter = DevinAdapter()
        assert adapter.auto_resolves_thread("🔴 **Bug: null pointer**") is True

    def test_auto_resolves_flag_thread(self):
        adapter = DevinAdapter()
        assert adapter.auto_resolves_thread("🚩 **check_for_updates not wrapped**") is True

    def test_does_not_auto_resolve_info_thread(self):
        adapter = DevinAdapter()
        assert adapter.auto_resolves_thread("📝 **Info: This is informational**") is False

    def test_rereview_trigger_empty(self):
        adapter = DevinAdapter()
        assert adapter.rereview_trigger(42, "owner", "repo") == []

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            ("🔴 **Bug: null pointer**", Severity.BUG),
            ("🚩 **check_for_updates not wrapped**", Severity.FLAGGED),
            ("🟡 Consider adding a docstring", Severity.WARNING),
            ("📝 **Info: This is informational**", Severity.INFO),
            ("Some plain comment text", Severity.INFO),
            ("🔴 bug and 📝 info in same comment", Severity.BUG),
            ("", Severity.INFO),
        ],
    )
    def test_classify_severity(self, body: str, expected: Severity):
        adapter = DevinAdapter()
        assert adapter.classify_severity(body) == expected


class TestUnblockedSeverity:
    def test_defaults_to_info(self):
        """Unblocked has no known severity format — base class returns info."""
        adapter = UnblockedAdapter()
        assert adapter.classify_severity("🔴 some bug text") == Severity.INFO
        assert adapter.classify_severity("plain comment") == Severity.INFO


class TestCodeRabbitAdapter:
    def test_properties(self):
        adapter = CodeRabbitAdapter()
        assert adapter.name == "coderabbit"
        assert adapter.needs_manual_rereview is False
        assert adapter.auto_resolves_comments is True

    def test_rereview_trigger_empty(self):
        adapter = CodeRabbitAdapter()
        assert adapter.rereview_trigger(42, "owner", "repo") == []

    def test_severity_defaults_to_info(self):
        """CodeRabbit has no known severity format — base class returns info."""
        adapter = CodeRabbitAdapter()
        assert adapter.classify_severity("any comment") == Severity.INFO


class TestReviewerRegistry:
    def test_all_reviewers_present(self):
        names = {r.name for r in REVIEWERS}
        assert names == {"unblocked", "devin", "coderabbit"}
