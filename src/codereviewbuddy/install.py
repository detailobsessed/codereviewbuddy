"""Install codereviewbuddy in MCP clients — one command setup.

Reuses FastMCP's ``StdioMCPServer`` model and ``update_config_file()`` utility
for config-file clients (Claude Desktop, Windsurf, Windsurf Next), but generates
a ``uvx``-based command instead of FastMCP's ``uv run ... fastmcp run`` pattern.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess  # noqa: S404
import sys
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

import cyclopts
from fastmcp.mcp_config import StdioMCPServer, update_config_file
from rich import print as rprint

SERVER_NAME = "codereviewbuddy"

install_app = cyclopts.App(
    name="install",
    help="Install codereviewbuddy in an MCP client.",
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _build_server_config(
    env_vars: list[str] | None = None,
    env_file: Path | None = None,
) -> StdioMCPServer:
    """Build the canonical MCP server config for codereviewbuddy."""
    env: dict[str, str] = {}

    # Load from .env file first (if provided)
    if env_file:
        try:
            from dotenv import dotenv_values  # noqa: PLC0415

            env |= {k: v for k, v in dotenv_values(env_file).items() if v is not None}
        except Exception as exc:
            rprint(f"[red]Failed to load .env file: {exc}[/red]")
            sys.exit(1)

    # CLI --env flags override file values
    for item in env_vars or []:
        if "=" not in item:
            rprint(f"[red]Invalid env var format: '{item}'. Must be KEY=VALUE[/red]")
            sys.exit(1)
        key, value = item.split("=", 1)
        env[key.strip()] = value.strip()

    return StdioMCPServer(
        command="uvx",
        args=["--prerelease=allow", "codereviewbuddy@latest"],
        env=env,
    )


def _write_config_file(
    config_path: Path,
    server_config: StdioMCPServer,
    *,
    client_name: str,
) -> bool:
    """Write server config into an mcpServers JSON file, creating it if needed."""
    try:
        # Ensure parent dirs exist
        config_path.parent.mkdir(parents=True, exist_ok=True)

        # Create file with empty mcpServers if it doesn't exist or is empty
        if not config_path.exists() or not config_path.read_text(encoding="utf-8").strip():
            config_path.write_text('{"mcpServers": {}}', encoding="utf-8")

        update_config_file(config_path, SERVER_NAME, server_config)
    except Exception as exc:
        rprint(f"[red]Failed to install in {client_name}: {exc}[/red]")
        return False
    else:
        rprint(f"[green]✅ Successfully installed '{SERVER_NAME}' in {client_name}[/green]")
        rprint(f"[blue]   Config: {config_path}[/blue]")
        rprint(f"[blue]   Restart {client_name} to activate.[/blue]")
        return True


# ---------------------------------------------------------------------------
# Config path helpers
# ---------------------------------------------------------------------------


def _get_claude_desktop_config_path() -> Path | None:
    """Get Claude Desktop config file path (cross-platform)."""
    if sys.platform == "win32":
        base = Path.home() / "AppData" / "Roaming" / "Claude"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "Claude"
    elif sys.platform.startswith("linux"):
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"), "Claude")
    else:
        return None

    if base.exists():
        return base / "claude_desktop_config.json"
    return None


def _get_windsurf_config_path(variant: str = "windsurf") -> Path:
    """Get Windsurf/Windsurf Next config file path."""
    return Path.home() / ".codeium" / variant / "mcp_config.json"


# ---------------------------------------------------------------------------
# Client: Claude Desktop
# ---------------------------------------------------------------------------


@install_app.command(name="claude-desktop")
def cmd_claude_desktop(
    *,
    env: Annotated[
        list[str] | None,
        cyclopts.Parameter(name="--env", help="Environment variables in KEY=VALUE format"),
    ] = None,
    env_file: Annotated[
        Path | None,
        cyclopts.Parameter(name=["--env-file", "-f"], help="Load environment variables from .env file"),
    ] = None,
) -> None:
    """Install codereviewbuddy in Claude Desktop."""
    config_path = _get_claude_desktop_config_path()
    if config_path is None:
        rprint(
            "[red]Claude Desktop config directory not found.[/red]\n"
            "[blue]Ensure Claude Desktop is installed and has been run at least once.[/blue]"
        )
        sys.exit(1)

    server_config = _build_server_config(env_vars=env, env_file=env_file)
    if not _write_config_file(config_path, server_config, client_name="Claude Desktop"):
        sys.exit(1)


# ---------------------------------------------------------------------------
# Client: Claude Code
# ---------------------------------------------------------------------------


def _find_claude_command() -> str | None:
    """Find the Claude Code CLI executable."""
    claude_in_path = shutil.which("claude")
    if claude_in_path:
        try:
            result = subprocess.run(  # noqa: S603
                [claude_in_path, "--version"],
                check=True,
                capture_output=True,
                text=True,
            )
            if "Claude Code" in result.stdout:
                return claude_in_path
        except subprocess.CalledProcessError, FileNotFoundError:
            pass

    potential_paths = [
        Path.home() / ".claude" / "local" / "claude",
        Path("/usr/local/bin/claude"),
        Path.home() / ".npm-global" / "bin" / "claude",
    ]
    for path in potential_paths:
        if path.exists():
            try:
                result = subprocess.run(  # noqa: S603
                    [str(path), "--version"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                if "Claude Code" in result.stdout:
                    return str(path)
            except subprocess.CalledProcessError, FileNotFoundError:
                continue
    return None


@install_app.command(name="claude-code")
def cmd_claude_code(
    *,
    env: Annotated[
        list[str] | None,
        cyclopts.Parameter(name="--env", help="Environment variables in KEY=VALUE format"),
    ] = None,
    env_file: Annotated[
        Path | None,
        cyclopts.Parameter(name=["--env-file", "-f"], help="Load environment variables from .env file"),
    ] = None,
) -> None:
    """Install codereviewbuddy in Claude Code."""
    claude_cmd = _find_claude_command()
    if not claude_cmd:
        rprint(
            "[red]Claude Code CLI not found.[/red]\n[blue]Ensure Claude Code is installed. Try running 'claude --version' to verify.[/blue]"
        )
        sys.exit(1)

    server_config = _build_server_config(env_vars=env, env_file=env_file)

    cmd_parts: list[str] = [claude_cmd, "mcp", "add", SERVER_NAME]
    if server_config.env:
        for key, value in server_config.env.items():
            cmd_parts.extend(["-e", f"{key}={value}"])
    cmd_parts.extend(("--", server_config.command))
    cmd_parts.extend(server_config.args)

    try:
        subprocess.run(cmd_parts, check=True, capture_output=True, text=True)  # noqa: S603
        rprint(f"[green]✅ Successfully installed '{SERVER_NAME}' in Claude Code[/green]")
    except subprocess.CalledProcessError as exc:
        rprint(f"[red]Failed to install in Claude Code: {exc.stderr.strip() if exc.stderr else exc}[/red]")
        sys.exit(1)


# ---- Client: Cursor ----


def _open_deeplink(url: str) -> bool:
    """Open a deeplink URL using the system handler."""
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", url], check=True, capture_output=True)  # noqa: S603, S607
        elif sys.platform == "win32":
            os.startfile(url)  # noqa: S606
        else:
            subprocess.run(["xdg-open", url], check=True, capture_output=True)  # noqa: S603, S607
    except subprocess.CalledProcessError, FileNotFoundError, OSError:
        return False
    else:
        return True


@install_app.command(name="cursor")
def cmd_cursor(
    *,
    env: Annotated[
        list[str] | None,
        cyclopts.Parameter(name="--env", help="Environment variables in KEY=VALUE format"),
    ] = None,
    env_file: Annotated[
        Path | None,
        cyclopts.Parameter(name=["--env-file", "-f"], help="Load environment variables from .env file"),
    ] = None,
) -> None:
    """Install codereviewbuddy in Cursor (opens deeplink)."""
    server_config = _build_server_config(env_vars=env, env_file=env_file)

    config_json = server_config.model_dump_json(exclude_none=True)
    config_b64 = base64.urlsafe_b64encode(config_json.encode()).decode()
    encoded_name = quote(SERVER_NAME, safe="")
    deeplink = f"cursor://anysphere.cursor-deeplink/mcp/install?name={encoded_name}&config={config_b64}"

    rprint(f"[blue]Opening Cursor to install '{SERVER_NAME}'...[/blue]")
    if _open_deeplink(deeplink):
        rprint("[green]✅ Cursor should now show the installation dialog.[/green]")
    else:
        rprint(f"[red]Could not open Cursor automatically.[/red]\n[blue]Copy this link and open it in Cursor:\n{deeplink}[/blue]")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Client: Windsurf / Windsurf Next
# ---------------------------------------------------------------------------


@install_app.command(name="windsurf")
def cmd_windsurf(
    *,
    env: Annotated[
        list[str] | None,
        cyclopts.Parameter(name="--env", help="Environment variables in KEY=VALUE format"),
    ] = None,
    env_file: Annotated[
        Path | None,
        cyclopts.Parameter(name=["--env-file", "-f"], help="Load environment variables from .env file"),
    ] = None,
) -> None:
    """Install codereviewbuddy in Windsurf."""
    config_path = _get_windsurf_config_path("windsurf")
    server_config = _build_server_config(env_vars=env, env_file=env_file)
    if not _write_config_file(config_path, server_config, client_name="Windsurf"):
        sys.exit(1)


@install_app.command(name="windsurf-next")
def cmd_windsurf_next(
    *,
    env: Annotated[
        list[str] | None,
        cyclopts.Parameter(name="--env", help="Environment variables in KEY=VALUE format"),
    ] = None,
    env_file: Annotated[
        Path | None,
        cyclopts.Parameter(name=["--env-file", "-f"], help="Load environment variables from .env file"),
    ] = None,
) -> None:
    """Install codereviewbuddy in Windsurf Next."""
    config_path = _get_windsurf_config_path("windsurf-next")
    server_config = _build_server_config(env_vars=env, env_file=env_file)
    if not _write_config_file(config_path, server_config, client_name="Windsurf Next"):
        sys.exit(1)


# ---------------------------------------------------------------------------
# Client: Generic MCP JSON
# ---------------------------------------------------------------------------


@install_app.command(name="mcp-json")
def cmd_mcp_json(
    *,
    env: Annotated[
        list[str] | None,
        cyclopts.Parameter(name="--env", help="Environment variables in KEY=VALUE format"),
    ] = None,
    env_file: Annotated[
        Path | None,
        cyclopts.Parameter(name=["--env-file", "-f"], help="Load environment variables from .env file"),
    ] = None,
    copy: Annotated[
        bool,
        cyclopts.Parameter(name="--copy", help="Copy to clipboard instead of printing to stdout"),
    ] = False,
) -> None:
    """Generate MCP JSON config for any client."""
    server_config = _build_server_config(env_vars=env, env_file=env_file)

    config_dict: dict[str, object] = {
        "command": server_config.command,
        "args": server_config.args,
    }
    if server_config.env:
        config_dict["env"] = server_config.env

    output = json.dumps({SERVER_NAME: config_dict}, indent=2)

    if copy:
        try:
            import pyperclip  # noqa: PLC0415

            pyperclip.copy(output)
            rprint(f"[green]✅ MCP config for '{SERVER_NAME}' copied to clipboard[/green]")
        except ImportError:
            rprint("[red]pyperclip not installed. Install with: pip install pyperclip[/red]")
            rprint(output)
            sys.exit(1)
    else:
        # Print raw JSON to stdout (no rich formatting — for piping)
        print(output)  # noqa: T201
