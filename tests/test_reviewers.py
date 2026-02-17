"""Tests for reviewer adapters."""

from __future__ import annotations

import pytest

from codereviewbuddy.config import Severity
from codereviewbuddy.reviewers import (
    REVIEWERS,
    CodeRabbitAdapter,
    DevinAdapter,
    GreptileAdapter,
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
            ("greptile-apps", "greptile"),
            ("greptile-apps[bot]", "greptile"),
            ("Greptile", "greptile"),
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
        assert adapter.auto_resolves_comments is False


class TestDevinAdapter:
    def test_properties(self):
        adapter = DevinAdapter()
        assert adapter.name == "devin"
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
        assert adapter.auto_resolves_comments is True

    def test_severity_defaults_to_info(self):
        """CodeRabbit has no known severity format — base class returns info."""
        adapter = CodeRabbitAdapter()
        assert adapter.classify_severity("any comment") == Severity.INFO


class TestGreptileAdapter:
    def test_properties(self):
        adapter = GreptileAdapter()
        assert adapter.name == "greptile"
        assert adapter.auto_resolves_comments is False

    def test_default_resolve_levels(self):
        adapter = GreptileAdapter()
        assert set(adapter.default_resolve_levels) == set(Severity)

    def test_default_auto_resolve_stale(self):
        adapter = GreptileAdapter()
        assert adapter.default_auto_resolve_stale is True

    def test_severity_defaults_to_info(self):
        adapter = GreptileAdapter()
        assert adapter.classify_severity("any comment") == Severity.INFO


class TestAdapterDefaults:
    """Test that adapters declare correct default guardrail values."""

    def test_devin_defaults(self):
        adapter = DevinAdapter()
        assert adapter.default_auto_resolve_stale is False
        assert adapter.default_resolve_levels == [Severity.INFO]

    def test_unblocked_defaults(self):
        adapter = UnblockedAdapter()
        assert adapter.default_auto_resolve_stale is True
        assert set(adapter.default_resolve_levels) == set(Severity)

    def test_coderabbit_defaults(self):
        adapter = CodeRabbitAdapter()
        assert adapter.default_auto_resolve_stale is False
        assert adapter.default_resolve_levels == []

    def test_greptile_defaults(self):
        adapter = GreptileAdapter()
        assert adapter.default_auto_resolve_stale is True
        assert set(adapter.default_resolve_levels) == set(Severity)


class TestReviewerRegistry:
    def test_all_reviewers_present(self):
        names = {r.name for r in REVIEWERS}
        assert names == {"unblocked", "devin", "coderabbit", "greptile"}
