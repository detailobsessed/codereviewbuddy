"""Tests for server.py — entrypoint, init command, and prerequisites."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture

from codereviewbuddy.gh import GhNotAuthenticatedError, GhNotFoundError
from codereviewbuddy.server import _init_config, check_prerequisites


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


class TestInitConfig:
    def test_creates_config_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        _init_config()
        from codereviewbuddy.config import CONFIG_FILENAME

        assert (tmp_path / CONFIG_FILENAME).exists()

    def test_fails_if_file_exists(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        from codereviewbuddy.config import CONFIG_FILENAME

        (tmp_path / CONFIG_FILENAME).write_text("existing", encoding="utf-8")
        with pytest.raises(SystemExit):
            _init_config()


class TestMain:
    def test_init_subcommand(self, mocker: MockerFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        mocker.patch("sys.argv", ["codereviewbuddy", "init"])
        from codereviewbuddy.server import main

        main()
        from codereviewbuddy.config import CONFIG_FILENAME

        assert (tmp_path / CONFIG_FILENAME).exists()

    def test_run_server(self, mocker: MockerFixture):
        mocker.patch("sys.argv", ["codereviewbuddy"])
        mock_run = mocker.patch("codereviewbuddy.server.mcp.run")
        mocker.patch("codereviewbuddy.io_tap.install_io_tap", return_value=False)
        from codereviewbuddy.server import main

        main()
        mock_run.assert_called_once()
