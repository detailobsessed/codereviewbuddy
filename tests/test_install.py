"""Tests for the install CLI commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codereviewbuddy.install import (
    SERVER_NAME,
    _build_server_config,
    _get_claude_desktop_config_path,
    _get_windsurf_config_path,
    _write_config_file,
)

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


# ---------------------------------------------------------------------------
# _build_server_config
# ---------------------------------------------------------------------------


class TestBuildServerConfig:
    def test_default_config(self):
        config = _build_server_config()
        assert config.command == "uvx"
        assert config.args == ["--prerelease=allow", "codereviewbuddy@latest"]
        assert config.env == {}

    def test_with_env_vars(self):
        config = _build_server_config(env_vars=["KEY1=val1", "KEY2=val2"])
        assert config.env == {"KEY1": "val1", "KEY2": "val2"}

    def test_env_var_with_equals_in_value(self):
        config = _build_server_config(env_vars=['CRB_REVIEWERS={"a": "b=c"}'])
        assert config.env["CRB_REVIEWERS"] == '{"a": "b=c"}'

    def test_invalid_env_var_exits(self):
        with pytest.raises(SystemExit):
            _build_server_config(env_vars=["NOEQUALS"])

    def test_env_file(self, tmp_path: Path):
        env_file = tmp_path / ".env"
        env_file.write_text("FROM_FILE=hello\nANOTHER=world\n", encoding="utf-8")
        config = _build_server_config(env_file=env_file)
        assert config.env == {"FROM_FILE": "hello", "ANOTHER": "world"}

    def test_env_cli_overrides_file(self, tmp_path: Path):
        env_file = tmp_path / ".env"
        env_file.write_text("KEY=from_file\n", encoding="utf-8")
        config = _build_server_config(env_vars=["KEY=from_cli"], env_file=env_file)
        assert config.env["KEY"] == "from_cli"


# ---------------------------------------------------------------------------
# _write_config_file
# ---------------------------------------------------------------------------


class TestWriteConfigFile:
    def test_creates_new_config(self, tmp_path: Path):
        config_path = tmp_path / "mcp_config.json"
        server_config = _build_server_config()

        result = _write_config_file(config_path, server_config, client_name="Test")
        assert result is True

        data = json.loads(config_path.read_text(encoding="utf-8"))
        assert SERVER_NAME in data["mcpServers"]
        assert data["mcpServers"][SERVER_NAME]["command"] == "uvx"

    def test_preserves_existing_servers(self, tmp_path: Path):
        config_path = tmp_path / "mcp_config.json"
        config_path.write_text(
            json.dumps({
                "mcpServers": {
                    "other-server": {"command": "node", "args": ["server.js"]},
                }
            }),
            encoding="utf-8",
        )

        server_config = _build_server_config()
        _write_config_file(config_path, server_config, client_name="Test")

        data = json.loads(config_path.read_text(encoding="utf-8"))
        assert "other-server" in data["mcpServers"]
        assert SERVER_NAME in data["mcpServers"]

    def test_updates_existing_entry(self, tmp_path: Path):
        config_path = tmp_path / "mcp_config.json"
        config_path.write_text(
            json.dumps({
                "mcpServers": {
                    SERVER_NAME: {
                        "command": "uvx",
                        "args": ["codereviewbuddy"],
                        "env": {"OLD_KEY": "old"},
                    },
                }
            }),
            encoding="utf-8",
        )

        server_config = _build_server_config(env_vars=["NEW_KEY=new"])
        _write_config_file(config_path, server_config, client_name="Test")

        data = json.loads(config_path.read_text(encoding="utf-8"))
        entry = data["mcpServers"][SERVER_NAME]
        assert entry["args"] == ["--prerelease=allow", "codereviewbuddy@latest"]
        assert entry["env"]["NEW_KEY"] == "new"

    def test_creates_parent_dirs(self, tmp_path: Path):
        config_path = tmp_path / "deep" / "nested" / "mcp_config.json"
        server_config = _build_server_config()

        result = _write_config_file(config_path, server_config, client_name="Test")
        assert result is True
        assert config_path.exists()

    def test_handles_empty_file(self, tmp_path: Path):
        config_path = tmp_path / "mcp_config.json"
        config_path.write_text("", encoding="utf-8")

        server_config = _build_server_config()
        result = _write_config_file(config_path, server_config, client_name="Test")
        assert result is True

        data = json.loads(config_path.read_text(encoding="utf-8"))
        assert SERVER_NAME in data["mcpServers"]


# ---------------------------------------------------------------------------
# Config path helpers
# ---------------------------------------------------------------------------


class TestConfigPaths:
    def test_windsurf_path(self):
        path = _get_windsurf_config_path("windsurf")
        assert path == Path.home() / ".codeium" / "windsurf" / "mcp_config.json"

    def test_windsurf_next_path(self):
        path = _get_windsurf_config_path("windsurf-next")
        assert path == Path.home() / ".codeium" / "windsurf-next" / "mcp_config.json"

    @pytest.mark.skipif(
        __import__("sys").platform != "darwin",
        reason="macOS-only test",
    )
    def test_claude_desktop_path_macos(self):
        path = _get_claude_desktop_config_path()
        # Only check the path structure, not existence
        if path is not None:
            assert "Claude" in str(path)
            assert path.name == "claude_desktop_config.json"


# ---------------------------------------------------------------------------
# MCP JSON output
# ---------------------------------------------------------------------------


class TestMcpJsonCommand:
    def test_json_output(self, capsys):
        """mcp-json command prints valid JSON to stdout."""
        from codereviewbuddy.install import cmd_mcp_json

        cmd_mcp_json()
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert SERVER_NAME in data
        assert data[SERVER_NAME]["command"] == "uvx"
        assert data[SERVER_NAME]["args"] == ["--prerelease=allow", "codereviewbuddy@latest"]

    def test_json_with_env(self, capsys):
        from codereviewbuddy.install import cmd_mcp_json

        cmd_mcp_json(env=["CRB_FOO=bar"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data[SERVER_NAME]["env"] == {"CRB_FOO": "bar"}

    def test_json_no_env_key_when_empty(self, capsys):
        from codereviewbuddy.install import cmd_mcp_json

        cmd_mcp_json()
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "env" not in data[SERVER_NAME]


# ---------------------------------------------------------------------------
# Windsurf commands (filesystem-based — easy to test)
# ---------------------------------------------------------------------------


class TestWindsurfCommands:
    def test_windsurf_install(self, tmp_path: Path, mocker: MockerFixture):
        mocker.patch(
            "codereviewbuddy.install._get_windsurf_config_path",
            return_value=tmp_path / "mcp_config.json",
        )
        from codereviewbuddy.install import cmd_windsurf

        cmd_windsurf()
        data = json.loads((tmp_path / "mcp_config.json").read_text(encoding="utf-8"))
        assert SERVER_NAME in data["mcpServers"]

    def test_windsurf_next_install(self, tmp_path: Path, mocker: MockerFixture):
        mocker.patch(
            "codereviewbuddy.install._get_windsurf_config_path",
            return_value=tmp_path / "mcp_config.json",
        )
        from codereviewbuddy.install import cmd_windsurf_next

        cmd_windsurf_next()
        data = json.loads((tmp_path / "mcp_config.json").read_text(encoding="utf-8"))
        assert SERVER_NAME in data["mcpServers"]


# ---------------------------------------------------------------------------
# Claude Desktop command
# ---------------------------------------------------------------------------


class TestClaudeDesktopCommand:
    def test_installs_to_config_file(self, tmp_path: Path, mocker: MockerFixture):
        config_file = tmp_path / "claude_desktop_config.json"
        config_file.write_text('{"mcpServers": {}}', encoding="utf-8")

        mocker.patch(
            "codereviewbuddy.install._get_claude_desktop_config_path",
            return_value=config_file,
        )
        from codereviewbuddy.install import cmd_claude_desktop

        cmd_claude_desktop()
        data = json.loads(config_file.read_text(encoding="utf-8"))
        assert SERVER_NAME in data["mcpServers"]

    def test_exits_when_config_not_found(self, mocker: MockerFixture):
        mocker.patch(
            "codereviewbuddy.install._get_claude_desktop_config_path",
            return_value=None,
        )
        from codereviewbuddy.install import cmd_claude_desktop

        with pytest.raises(SystemExit):
            cmd_claude_desktop()


# ---------------------------------------------------------------------------
# Claude Code command
# ---------------------------------------------------------------------------


class TestClaudeCodeCommand:
    def test_runs_claude_mcp_add(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.install._find_claude_command", return_value="/usr/bin/claude")
        mock_run = mocker.patch("codereviewbuddy.install.subprocess.run")

        from codereviewbuddy.install import cmd_claude_code

        cmd_claude_code()
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "/usr/bin/claude"
        assert call_args[1:3] == ["mcp", "add"]
        assert SERVER_NAME in call_args
        assert "uvx" in call_args

    def test_runs_claude_mcp_add_with_env(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.install._find_claude_command", return_value="/usr/bin/claude")
        mock_run = mocker.patch("codereviewbuddy.install.subprocess.run")

        from codereviewbuddy.install import cmd_claude_code

        cmd_claude_code(env=["CRB_FOO=bar"])
        call_args = mock_run.call_args[0][0]
        assert "-e" in call_args
        assert "CRB_FOO=bar" in call_args

    def test_exits_when_claude_not_found(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.install._find_claude_command", return_value=None)
        from codereviewbuddy.install import cmd_claude_code

        with pytest.raises(SystemExit):
            cmd_claude_code()

    def test_exits_on_subprocess_error(self, mocker: MockerFixture):
        import subprocess

        mocker.patch("codereviewbuddy.install._find_claude_command", return_value="/usr/bin/claude")
        mocker.patch(
            "codereviewbuddy.install.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "claude", stderr="fail"),
        )
        from codereviewbuddy.install import cmd_claude_code

        with pytest.raises(SystemExit):
            cmd_claude_code()


# ---------------------------------------------------------------------------
# _find_claude_command
# ---------------------------------------------------------------------------


class TestFindClaudeCommand:
    def test_found_in_path(self, mocker: MockerFixture):
        from codereviewbuddy.install import _find_claude_command

        mocker.patch("codereviewbuddy.install.shutil.which", return_value="/usr/bin/claude")
        mock_run = mocker.patch("codereviewbuddy.install.subprocess.run")
        mock_run.return_value.stdout = "Claude Code v1.0"

        result = _find_claude_command()
        assert result == "/usr/bin/claude"

    def test_not_claude_code(self, mocker: MockerFixture):
        from codereviewbuddy.install import _find_claude_command

        mocker.patch("codereviewbuddy.install.shutil.which", return_value="/usr/bin/claude")
        mock_run = mocker.patch("codereviewbuddy.install.subprocess.run")
        mock_run.return_value.stdout = "some other claude"

        # Also mock potential_paths to not exist
        mocker.patch("pathlib.Path.exists", return_value=False)
        result = _find_claude_command()
        assert result is None

    def test_not_in_path_not_in_known_locations(self, mocker: MockerFixture):
        from codereviewbuddy.install import _find_claude_command

        mocker.patch("codereviewbuddy.install.shutil.which", return_value=None)
        mocker.patch("pathlib.Path.exists", return_value=False)
        result = _find_claude_command()
        assert result is None

    def test_subprocess_error_in_path(self, mocker: MockerFixture):
        import subprocess

        from codereviewbuddy.install import _find_claude_command

        mocker.patch("codereviewbuddy.install.shutil.which", return_value="/usr/bin/claude")
        mocker.patch(
            "codereviewbuddy.install.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "claude"),
        )
        mocker.patch("pathlib.Path.exists", return_value=False)
        result = _find_claude_command()
        assert result is None


# ---------------------------------------------------------------------------
# _open_deeplink
# ---------------------------------------------------------------------------


class TestOpenDeeplink:
    def test_opens_on_macos(self, mocker: MockerFixture):
        from codereviewbuddy.install import _open_deeplink

        mocker.patch("codereviewbuddy.install.sys.platform", "darwin")
        mock_run = mocker.patch("codereviewbuddy.install.subprocess.run")
        assert _open_deeplink("cursor://test") is True
        mock_run.assert_called_once()

    def test_opens_on_linux(self, mocker: MockerFixture):
        from codereviewbuddy.install import _open_deeplink

        mocker.patch("codereviewbuddy.install.sys.platform", "linux")
        mock_run = mocker.patch("codereviewbuddy.install.subprocess.run")
        assert _open_deeplink("cursor://test") is True
        mock_run.assert_called_once()

    def test_returns_false_on_error(self, mocker: MockerFixture):
        import subprocess

        from codereviewbuddy.install import _open_deeplink

        mocker.patch("codereviewbuddy.install.sys.platform", "darwin")
        mocker.patch(
            "codereviewbuddy.install.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "open"),
        )
        assert _open_deeplink("cursor://test") is False


# ---------------------------------------------------------------------------
# Cursor command
# ---------------------------------------------------------------------------


class TestCursorCommand:
    def test_opens_deeplink(self, mocker: MockerFixture):
        mock_open = mocker.patch("codereviewbuddy.install._open_deeplink", return_value=True)
        from codereviewbuddy.install import cmd_cursor

        cmd_cursor()
        mock_open.assert_called_once()
        url = mock_open.call_args[0][0]
        assert url.startswith("cursor://")
        assert SERVER_NAME in url

    def test_exits_on_deeplink_failure(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.install._open_deeplink", return_value=False)
        from codereviewbuddy.install import cmd_cursor

        with pytest.raises(SystemExit):
            cmd_cursor()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_write_config_file_returns_false_on_error(self, tmp_path: Path):
        config_path = tmp_path / "readonly" / "mcp_config.json"
        # Create a directory where the file would be, so write_text fails
        config_path.parent.mkdir(parents=True)
        config_path.mkdir()  # make config_path a dir so write_text raises

        server_config = _build_server_config()
        result = _write_config_file(config_path, server_config, client_name="Test")
        assert result is False

    def test_build_server_config_dotenv_error(self, tmp_path: Path, mocker: MockerFixture):
        env_file = tmp_path / ".env"
        env_file.write_text("VALID=yes\n", encoding="utf-8")

        mocker.patch(
            "dotenv.dotenv_values",
            side_effect=OSError("permission denied"),
        )
        with pytest.raises(SystemExit):
            _build_server_config(env_file=env_file)

    def test_mcp_json_copy_without_pyperclip(self, mocker: MockerFixture):
        mocker.patch.dict("sys.modules", {"pyperclip": None})
        from codereviewbuddy.install import cmd_mcp_json

        with pytest.raises(SystemExit):
            cmd_mcp_json(copy=True)

    def test_claude_desktop_write_failure_exits(self, tmp_path: Path, mocker: MockerFixture):
        config_file = tmp_path / "claude_desktop_config.json"
        # Make config_file a directory so write_text inside _write_config_file fails
        config_file.mkdir(parents=True)

        mocker.patch(
            "codereviewbuddy.install._get_claude_desktop_config_path",
            return_value=config_file,
        )
        from codereviewbuddy.install import cmd_claude_desktop

        with pytest.raises(SystemExit):
            cmd_claude_desktop()
