"""Tests for server.py — entrypoint, init command, and prerequisites."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

from codereviewbuddy.gh import GhError, GhNotAuthenticatedError, GhNotFoundError
from codereviewbuddy.server import (
    _check_auto_detect_prerequisites,
    _get_workspace_cwd,
    _recovery_error,
    _resolve_pr_number,
    _resolve_thread_pr_number,
    check_ci_status,
    check_fastmcp_runtime,
    check_prerequisites,
    diagnose_ci,
    get_thread,
    list_recent_unresolved,
    reply_to_comment,
    review_pr_descriptions,
    show_config,
    stack_activity,
    summarize_review_status,
    triage_review_comments,
)


class TestCheckPrerequisites:
    def test_success(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.server.gh.check_auth", return_value="testuser")
        check_prerequisites()  # should not raise

    def test_gh_not_found(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.server.gh.check_auth", side_effect=GhNotFoundError())
        with pytest.raises(GhNotFoundError):
            check_prerequisites()

    def test_gh_not_authenticated(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.server.gh.check_auth", side_effect=GhNotAuthenticatedError("not auth"))
        with pytest.raises(GhNotAuthenticatedError):
            check_prerequisites()


class TestCheckFastMcpRuntime:
    def test_success(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.server.importlib.util.find_spec", return_value=object())
        mocker.patch("codereviewbuddy.server.importlib.import_module", return_value=object())
        check_fastmcp_runtime()  # should not raise

    def test_find_spec_module_not_found_treated_as_missing(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.server.importlib.util.find_spec", side_effect=ModuleNotFoundError("no module"))
        with pytest.raises(RuntimeError, match=r"missing fastmcp\.server\.tasks\.routing"):
            check_fastmcp_runtime()

    def test_missing_task_routing_module(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.server.importlib.util.find_spec", return_value=None)
        with pytest.raises(RuntimeError, match=r"missing fastmcp\.server\.tasks\.routing"):
            check_fastmcp_runtime()

    def test_import_module_failure_raises_runtime_error(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.server.importlib.util.find_spec", return_value=object())
        mocker.patch("codereviewbuddy.server.importlib.import_module", side_effect=ImportError("bad import"))
        with pytest.raises(RuntimeError, match=r"failed to import"):
            check_fastmcp_runtime()


class TestResolvePrNumber:
    def test_returns_explicit_number(self):
        assert _resolve_pr_number(42) == 42

    def test_auto_detects_from_branch(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.server.gh.get_current_pr_number", return_value=99)
        assert _resolve_pr_number(None) == 99

    def test_raises_when_no_pr(self, mocker: MockerFixture):
        mocker.patch(
            "codereviewbuddy.server.gh.get_current_pr_number",
            side_effect=GhError("no pull requests found"),
        )
        with pytest.raises(GhError, match="no pull requests found"):
            _resolve_pr_number(None)


class TestMain:
    def test_run_server(self, mocker: MockerFixture):
        mocker.patch("sys.argv", ["codereviewbuddy"])
        mock_run = mocker.patch("codereviewbuddy.server.mcp.run")
        from codereviewbuddy.cli import serve

        serve()
        mock_run.assert_called_once()


class TestGetWorkspaceCwd:
    """Tests for _get_workspace_cwd — MCP roots → CRB_WORKSPACE → process cwd cascade (#142, #174)."""

    async def test_roots_take_priority_over_env_var(self, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture):
        from unittest.mock import AsyncMock

        from pydantic import FileUrl

        monkeypatch.setenv("CRB_WORKSPACE", "/from/env")
        root = mocker.MagicMock()
        root.uri = FileUrl("file:///from/roots")
        ctx = mocker.MagicMock()
        ctx.list_roots = AsyncMock(return_value=[root])

        result = await _get_workspace_cwd(ctx)
        assert result == "/from/roots"

    async def test_env_var_fallback_without_context(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CRB_WORKSPACE", "/from/env")
        assert await _get_workspace_cwd(None) == "/from/env"

    async def test_roots_used_when_no_env_var(self, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture):
        from unittest.mock import AsyncMock

        from pydantic import FileUrl

        monkeypatch.delenv("CRB_WORKSPACE", raising=False)
        root = mocker.MagicMock()
        root.uri = FileUrl("file:///Users/alice/repos/myproject")
        ctx = mocker.MagicMock()
        ctx.list_roots = AsyncMock(return_value=[root])

        result = await _get_workspace_cwd(ctx)
        assert result == "/Users/alice/repos/myproject"

    async def test_env_var_fallback_when_roots_empty(self, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture):
        from unittest.mock import AsyncMock

        monkeypatch.setenv("CRB_WORKSPACE", "/from/env")
        ctx = mocker.MagicMock()
        ctx.list_roots = AsyncMock(return_value=[])

        assert await _get_workspace_cwd(ctx) == "/from/env"

    async def test_falls_back_to_git_root_without_context_or_env(self, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture):
        monkeypatch.delenv("CRB_WORKSPACE", raising=False)
        mocker.patch("codereviewbuddy.server.gh._git_root_for_cwd", return_value="/git/root")
        assert await _get_workspace_cwd(None) == "/git/root"

    async def test_falls_back_to_git_root_when_roots_empty_no_env(self, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture):
        from unittest.mock import AsyncMock

        monkeypatch.delenv("CRB_WORKSPACE", raising=False)
        mocker.patch("codereviewbuddy.server.gh._git_root_for_cwd", return_value="/git/root")
        ctx = mocker.MagicMock()
        ctx.list_roots = AsyncMock(return_value=[])

        assert await _get_workspace_cwd(ctx) == "/git/root"

    async def test_env_var_fallback_on_roots_exception(self, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture):
        from unittest.mock import AsyncMock

        monkeypatch.setenv("CRB_WORKSPACE", "/from/env")
        ctx = mocker.MagicMock()
        ctx.list_roots = AsyncMock(side_effect=Exception("roots not supported"))

        assert await _get_workspace_cwd(ctx) == "/from/env"

    async def test_falls_back_to_git_root_on_roots_exception_no_env(self, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture):
        from unittest.mock import AsyncMock

        monkeypatch.delenv("CRB_WORKSPACE", raising=False)
        mocker.patch("codereviewbuddy.server.gh._git_root_for_cwd", return_value="/git/root")
        ctx = mocker.MagicMock()
        ctx.list_roots = AsyncMock(side_effect=Exception("roots not supported"))

        assert await _get_workspace_cwd(ctx) == "/git/root"

    async def test_timeout_falls_back_to_env_var(self, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture):
        from unittest.mock import AsyncMock

        monkeypatch.setenv("CRB_WORKSPACE", "/from/env")
        ctx = mocker.MagicMock()
        ctx.list_roots = AsyncMock(side_effect=TimeoutError)

        result = await _get_workspace_cwd(ctx)
        assert result == "/from/env"

    async def test_unsupported_scheme_falls_through_to_git_root(self, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture):
        from unittest.mock import AsyncMock

        monkeypatch.delenv("CRB_WORKSPACE", raising=False)
        mocker.patch("codereviewbuddy.server.gh._git_root_for_cwd", return_value="/git/root")
        root = mocker.MagicMock()
        root.uri = "https://example.com/repo"
        ctx = mocker.MagicMock()
        ctx.list_roots = AsyncMock(return_value=[root])

        assert await _get_workspace_cwd(ctx) == "/git/root"

    async def test_returns_none_when_not_in_git_repo(self, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture):
        monkeypatch.delenv("CRB_WORKSPACE", raising=False)
        mocker.patch("codereviewbuddy.server.gh._git_root_for_cwd", return_value=None)
        assert await _get_workspace_cwd(None) is None


class TestCheckAutoDetectPrerequisites:
    """Tests for _check_auto_detect_prerequisites — prevents wrong-cwd auto-detection (#174)."""

    def test_noop_when_cwd_detected(self):
        _check_auto_detect_prerequisites("/some/path", has_pr=False, has_repo=False)

    def test_noop_when_all_params_explicit(self):
        _check_auto_detect_prerequisites(None, has_pr=True, has_repo=True)

    def test_raises_when_cwd_none_and_pr_missing(self):
        with pytest.raises(GhError, match="Workspace not detected"):
            _check_auto_detect_prerequisites(None, has_pr=False, has_repo=True)

    def test_raises_when_cwd_none_and_repo_missing(self):
        with pytest.raises(GhError, match="Workspace not detected"):
            _check_auto_detect_prerequisites(None, has_pr=True, has_repo=False)

    def test_raises_when_cwd_none_and_both_missing(self):
        with pytest.raises(GhError, match="Workspace not detected") as exc_info:
            _check_auto_detect_prerequisites(None, has_pr=False, has_repo=False)
        msg = str(exc_info.value)
        assert "`pr_number`" in msg
        assert "`repo`" in msg

    def test_error_includes_fix_guidance(self):
        with pytest.raises(GhError, match=r"pass `repo` and `pr_number`"):
            _check_auto_detect_prerequisites(None, has_pr=False, has_repo=False)

    def test_error_lists_only_missing_params(self):
        with pytest.raises(GhError, match="`repo`") as exc_info:
            _check_auto_detect_prerequisites(None, has_pr=True, has_repo=False)
        # The "Missing:" line should only list repo, not pr_number
        missing_line = next(line for line in str(exc_info.value).splitlines() if line.startswith("Missing:"))
        assert "`repo`" in missing_line
        assert "`pr_number`" not in missing_line


class TestRecoveryError:
    """Tests for the _recovery_error helper that builds actionable error messages."""

    def test_gh_not_found(self):
        result = _recovery_error(GhNotFoundError(), tool_name="test_tool")
        assert "gh CLI not found" in result
        assert "https://cli.github.com/" in result

    def test_gh_not_authenticated(self):
        result = _recovery_error(GhNotAuthenticatedError("not auth"), tool_name="test_tool")
        assert "not authenticated" in result
        assert "gh auth login" in result

    def test_rate_limit(self):
        result = _recovery_error(Exception("API rate limit exceeded"), tool_name="test_tool")
        assert "rate limit" in result
        assert "Wait 60 seconds" in result

    def test_not_found_with_pr(self):
        result = _recovery_error(Exception("not found"), tool_name="test_tool", pr_number=42)
        assert "not found" in result
        assert "PR #42" in result

    def test_not_found_without_repo(self):
        result = _recovery_error(Exception("resource not found"), tool_name="test_tool")
        assert "repo='owner/repo' explicitly" in result

    def test_not_found_with_repo(self):
        result = _recovery_error(Exception("not found"), tool_name="test_tool", repo="owner/repo")
        assert "'owner/repo' is correct" in result

    def test_workspace_detection(self):
        result = _recovery_error(Exception("workspace not detected"), tool_name="test_tool")
        assert "CRB_WORKSPACE" in result

    def test_graphql_error(self):
        result = _recovery_error(Exception("GraphQL error in fetch"), tool_name="test_tool")
        assert "GraphQL error" in result
        assert "retry once" in result

    def test_generic_fallback_with_pr(self):
        result = _recovery_error(Exception("something went wrong"), tool_name="test_tool", pr_number=99)
        assert "test_tool failed" in result
        assert "PR #99" in result

    def test_generic_fallback_without_repo(self):
        result = _recovery_error(Exception("something went wrong"), tool_name="test_tool")
        assert "repo='owner/repo' explicitly" in result

    def test_generic_fallback_with_repo(self):
        result = _recovery_error(Exception("something"), tool_name="test_tool", repo="o/r")
        assert "repo='owner/repo'" not in result

    def test_403_triggers_rate_limit(self):
        result = _recovery_error(Exception("HTTP 403 Forbidden"), tool_name="test_tool")
        assert "rate limit" in result


@pytest.mark.usefixtures("patch_server_context")
class TestCancellationHandlers:
    """Ensure all tool handlers return a clean error on asyncio.CancelledError."""

    async def test_reply_to_comment_cancelled(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.server.comments.reply_to_comment", side_effect=asyncio.CancelledError)
        result = await reply_to_comment(thread_id="PRRT_abc", body="test", pr_number=1)
        assert result == "Cancelled"

    async def test_diagnose_ci_cancelled(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.server.call_sync_fn_in_threadpool", side_effect=asyncio.CancelledError)
        result = await diagnose_ci(pr_number=1)
        assert result.error == "Cancelled"

    async def test_get_thread_cancelled(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.server.comments.get_thread", side_effect=asyncio.CancelledError)
        result = await get_thread(thread_id="PRRT_abc")
        assert result == "Cancelled"

    async def test_get_thread_error(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.server.comments.get_thread", side_effect=RuntimeError("boom"))
        result = await get_thread(thread_id="PRRT_abc")
        assert "get_thread failed" in result

    async def test_review_pr_descriptions_cancelled(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.server.descriptions.review_pr_descriptions", side_effect=asyncio.CancelledError)
        result = await review_pr_descriptions(pr_numbers=[42])
        assert result.error == "Cancelled"

    async def test_summarize_review_status_cancelled(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.server.stack.summarize_review_status", side_effect=asyncio.CancelledError)
        result = await summarize_review_status(pr_numbers=[42])
        assert result.error == "Cancelled"

    async def test_list_recent_unresolved_cancelled(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.server.stack.list_recent_unresolved", side_effect=asyncio.CancelledError)
        result = await list_recent_unresolved(repo="o/r")
        assert result.error == "Cancelled"

    async def test_stack_activity_cancelled(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.server.stack.stack_activity", side_effect=asyncio.CancelledError)
        result = await stack_activity(pr_numbers=[42])
        assert result.error == "Cancelled"

    async def test_check_ci_status_cancelled(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.server.call_sync_fn_in_threadpool", side_effect=asyncio.CancelledError)
        result = await check_ci_status(pr_number=1)
        assert result.error == "Cancelled"

    async def test_triage_review_comments_cancelled(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.server.comments.triage_review_comments", side_effect=asyncio.CancelledError)
        result = await triage_review_comments(pr_numbers=[42])
        assert result.error == "Cancelled"


@pytest.mark.usefixtures("patch_server_context")
class TestErrorHandlers:
    """Ensure tool wrappers return structured errors on Exception (not just CancelledError)."""

    async def test_review_pr_descriptions_error(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.server.descriptions.review_pr_descriptions", side_effect=RuntimeError("boom"))
        result = await review_pr_descriptions(pr_numbers=[42])
        assert result.error is not None
        assert "review_pr_descriptions failed" in result.error

    async def test_summarize_review_status_error(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.server.stack.summarize_review_status", side_effect=RuntimeError("boom"))
        result = await summarize_review_status(pr_numbers=[42])
        assert result.error is not None
        assert "summarize_review_status failed" in result.error

    async def test_list_recent_unresolved_error(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.server.stack.list_recent_unresolved", side_effect=RuntimeError("boom"))
        result = await list_recent_unresolved(repo="o/r")
        assert result.error is not None
        assert "list_recent_unresolved failed" in result.error

    async def test_stack_activity_error(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.server.stack.stack_activity", side_effect=RuntimeError("boom"))
        result = await stack_activity(pr_numbers=[42])
        assert result.error is not None
        assert "stack_activity failed" in result.error

    async def test_check_ci_status_error(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.server.call_sync_fn_in_threadpool", side_effect=RuntimeError("boom"))
        result = await check_ci_status(pr_number=1)
        assert result.error is not None
        assert "check_ci_status failed" in result.error

    async def test_diagnose_ci_error(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.server.call_sync_fn_in_threadpool", side_effect=RuntimeError("boom"))
        result = await diagnose_ci(pr_number=1)
        assert result.error is not None
        assert "diagnose_ci failed" in result.error

    async def test_triage_review_comments_error(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.server.comments.triage_review_comments", side_effect=RuntimeError("boom"))
        result = await triage_review_comments(pr_numbers=[42])
        assert result.error is not None
        assert "triage_review_comments failed" in result.error

    async def test_reply_to_comment_error(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.server.comments.reply_to_comment", side_effect=RuntimeError("boom"))
        result = await reply_to_comment(thread_id="PRRT_abc", body="test", pr_number=1)
        assert "reply_to_comment failed" in result


@pytest.mark.usefixtures("patch_server_context")
class TestElicitAmbiguousItems:
    """Tests for _elicit_ambiguous_items — elicitation flow with fallback."""

    async def test_elicit_updates_ambiguous_action(self, mocker: MockerFixture):
        from fastmcp.server.elicitation import AcceptedElicitation

        from codereviewbuddy.models import TriageItem, TriageResult
        from codereviewbuddy.server import _elicit_ambiguous_items

        ctx = mocker.MagicMock()
        accepted = AcceptedElicitation(data="fix")
        ctx.elicit = AsyncMock(return_value=accepted)

        result = TriageResult(
            items=[TriageItem(thread_id="PRRT_1", pr_number=42, reviewer="bot", action="ambiguous", file="a.py", line=1)],
            total=1,
        )
        await _elicit_ambiguous_items(result, ctx)
        assert result.items[0].action == "fix"

    async def test_elicit_skips_non_ambiguous(self, mocker: MockerFixture):
        from codereviewbuddy.models import TriageItem, TriageResult
        from codereviewbuddy.server import _elicit_ambiguous_items

        ctx = mocker.MagicMock()
        ctx.elicit = AsyncMock()

        result = TriageResult(
            items=[TriageItem(thread_id="PRRT_1", pr_number=42, reviewer="bot", action="fix")],
            total=1,
        )
        await _elicit_ambiguous_items(result, ctx)
        ctx.elicit.assert_not_called()
        assert result.items[0].action == "fix"

    async def test_elicit_fallback_on_unsupported_client(self, mocker: MockerFixture):
        from codereviewbuddy.models import TriageItem, TriageResult
        from codereviewbuddy.server import _elicit_ambiguous_items

        ctx = mocker.MagicMock()
        ctx.elicit = AsyncMock(side_effect=RuntimeError("Elicitation not supported"))

        result = TriageResult(
            items=[TriageItem(thread_id="PRRT_1", pr_number=42, reviewer="bot", action="ambiguous")],
            total=1,
        )
        await _elicit_ambiguous_items(result, ctx)
        # Should stay "ambiguous" — graceful fallback
        assert result.items[0].action == "ambiguous"

    async def test_elicit_noop_without_ctx(self):
        from codereviewbuddy.models import TriageItem, TriageResult
        from codereviewbuddy.server import _elicit_ambiguous_items

        result = TriageResult(
            items=[TriageItem(thread_id="PRRT_1", pr_number=42, reviewer="bot", action="ambiguous")],
            total=1,
        )
        await _elicit_ambiguous_items(result, None)
        assert result.items[0].action == "ambiguous"


class TestResolveThreadPrNumber:
    """Tests for _resolve_thread_pr_number — PRRT_ vs PRR_/IC_ routing."""

    def test_prrt_returns_pr_number_as_is(self):
        assert _resolve_thread_pr_number("PRRT_abc", 42, "/tmp", has_repo=True) == 42  # noqa: S108

    def test_prrt_returns_none_when_none(self):
        assert _resolve_thread_pr_number("PRRT_abc", None, "/tmp", has_repo=True) is None  # noqa: S108

    def test_prr_resolves_pr_number(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.server.gh.get_current_pr_number", return_value=99)
        result = _resolve_thread_pr_number("PRR_abc", None, "/tmp", has_repo=True)  # noqa: S108
        assert result == 99


class TestShowConfigSelfImprovement:
    """Test show_config with self-improvement enabled."""

    def test_self_improvement_enabled(self, mocker: MockerFixture):
        from codereviewbuddy.config import Config, SelfImprovementConfig, set_config

        set_config(Config(self_improvement=SelfImprovementConfig(enabled=True)))
        try:
            result = show_config()
            assert "enabled" in result.explanation
        finally:
            set_config(Config())
