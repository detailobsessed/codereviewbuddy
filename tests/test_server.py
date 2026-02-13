"""Tests for server.py — entrypoint, init command, and prerequisites."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture

from codereviewbuddy.config import CONFIG_FILENAME, init_config, update_config
from codereviewbuddy.gh import GhError, GhNotAuthenticatedError, GhNotFoundError
from codereviewbuddy.server import _config_cmd, _resolve_pr_number, check_fastmcp_runtime, check_prerequisites


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


class TestInitConfig:
    def test_creates_config_file(self, tmp_path: Path):
        path = init_config(cwd=tmp_path)
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "[reviewers.devin]" in content
        assert "[pr_descriptions]" in content
        assert "[self_improvement]" in content
        assert "[diagnostics]" in content
        assert "io_tap = true" in content
        assert 'repo = "detailobsessed/codereviewbuddy"' in content

    def test_fails_if_file_exists(self, tmp_path: Path):
        (tmp_path / CONFIG_FILENAME).write_text("existing", encoding="utf-8")
        with pytest.raises(SystemExit):
            init_config(cwd=tmp_path)

    def test_hint_mentions_update(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
        (tmp_path / CONFIG_FILENAME).write_text("existing", encoding="utf-8")
        with pytest.raises(SystemExit):
            init_config(cwd=tmp_path)
        assert "--update" in capsys.readouterr().out


class TestUpdateConfig:
    def test_appends_missing_sections(self, tmp_path: Path):
        config_file = tmp_path / CONFIG_FILENAME
        config_file.write_text("[reviewers.devin]\nenabled = true\n", encoding="utf-8")
        _, added, _deprecated = update_config(cwd=tmp_path)
        assert "[pr_descriptions]" in added
        assert "[reviewers.unblocked]" in added
        assert "[reviewers.devin]" not in added  # already existed
        updated = config_file.read_text(encoding="utf-8")
        assert "[pr_descriptions]" in updated
        assert "enabled = true" in updated  # original value preserved

    def test_no_changes_when_up_to_date(self, tmp_path: Path):
        init_config(cwd=tmp_path)
        _, added, deprecated = update_config(cwd=tmp_path)
        assert added == []
        assert deprecated == []

    def test_fails_if_no_config(self, tmp_path: Path):
        with pytest.raises(SystemExit):
            update_config(cwd=tmp_path)

    def test_hint_mentions_init(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
        with pytest.raises(SystemExit):
            update_config(cwd=tmp_path)
        assert "--init" in capsys.readouterr().out


class TestConfigCmd:
    def test_no_flags_shows_usage(self):
        with pytest.raises(SystemExit):
            _config_cmd([])

    def test_init_flag(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        _config_cmd(["--init"])
        assert (tmp_path / CONFIG_FILENAME).exists()

    def test_update_flag(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / CONFIG_FILENAME).write_text("[reviewers.devin]\n", encoding="utf-8")
        _config_cmd(["--update"])

    def test_clean_flag(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / CONFIG_FILENAME).write_text("[pr_descriptions]\nrequire_review = false\n", encoding="utf-8")
        _config_cmd(["--clean"])
        content = (tmp_path / CONFIG_FILENAME).read_text(encoding="utf-8")
        assert "require_review" not in content


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
    def test_config_init_subcommand(self, mocker: MockerFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        mocker.patch("sys.argv", ["codereviewbuddy", "config", "--init"])
        from codereviewbuddy.server import main

        main()
        assert (tmp_path / CONFIG_FILENAME).exists()

    def test_run_server(self, mocker: MockerFixture):
        mocker.patch("sys.argv", ["codereviewbuddy"])
        mock_run = mocker.patch("codereviewbuddy.server.mcp.run")
        mocker.patch("codereviewbuddy.io_tap.install_io_tap", return_value=False)
        from codereviewbuddy.server import main

        main()
        mock_run.assert_called_once()
