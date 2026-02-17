"""Tests for server.py — entrypoint, init command, and prerequisites."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

from codereviewbuddy.gh import GhError, GhNotAuthenticatedError, GhNotFoundError
from codereviewbuddy.server import (
    _check_auto_detect_prerequisites,
    _get_workspace_cwd,
    _resolve_pr_number,
    check_fastmcp_runtime,
    check_prerequisites,
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
        mocker.patch("codereviewbuddy.io_tap.install_io_tap", return_value=False)
        from codereviewbuddy.cli import serve

        serve()
        mock_run.assert_called_once()


class TestGetWorkspaceCwd:
    """Tests for _get_workspace_cwd — MCP roots → CRB_WORKSPACE → None cascade (#142)."""

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

    async def test_returns_none_without_context_or_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("CRB_WORKSPACE", raising=False)
        assert await _get_workspace_cwd(None) is None

    async def test_returns_none_when_roots_empty_no_env(self, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture):
        from unittest.mock import AsyncMock

        monkeypatch.delenv("CRB_WORKSPACE", raising=False)
        ctx = mocker.MagicMock()
        ctx.list_roots = AsyncMock(return_value=[])

        assert await _get_workspace_cwd(ctx) is None

    async def test_env_var_fallback_on_roots_exception(self, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture):
        from unittest.mock import AsyncMock

        monkeypatch.setenv("CRB_WORKSPACE", "/from/env")
        ctx = mocker.MagicMock()
        ctx.list_roots = AsyncMock(side_effect=Exception("roots not supported"))

        assert await _get_workspace_cwd(ctx) == "/from/env"

    async def test_returns_none_on_roots_exception_no_env(self, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture):
        from unittest.mock import AsyncMock

        monkeypatch.delenv("CRB_WORKSPACE", raising=False)
        ctx = mocker.MagicMock()
        ctx.list_roots = AsyncMock(side_effect=Exception("roots not supported"))

        assert await _get_workspace_cwd(ctx) is None

    async def test_unsupported_scheme_falls_through(self, monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture):
        from unittest.mock import AsyncMock

        monkeypatch.delenv("CRB_WORKSPACE", raising=False)
        root = mocker.MagicMock()
        root.uri = "https://example.com/repo"
        ctx = mocker.MagicMock()
        ctx.list_roots = AsyncMock(return_value=[root])

        assert await _get_workspace_cwd(ctx) is None


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
