"""CLI for codereviewbuddy — built on cyclopts (same framework as FastMCP's CLI)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import cyclopts

app = cyclopts.App(
    name="codereviewbuddy",
    help="codereviewbuddy — AI code review buddy MCP server.",
)

# Register install subcommand group
from codereviewbuddy.install import install_app  # noqa: E402

app.command(install_app)


@app.default
def serve() -> None:
    """Run the codereviewbuddy MCP server (default command)."""
    from codereviewbuddy.server import mcp  # noqa: PLC0415

    mcp.run()


@app.command(name="check-env")
def check_env() -> None:
    """Validate CRB_* environment variables and print a diagnostic summary.

    Lists all recognized CRB_* env vars and their current values (masking
    sensitive ones), validates types and constraints, and warns about
    unrecognized CRB_* env vars (typo detection).
    """
    known_prefixes = _build_known_prefixes()

    print("codereviewbuddy check-env")
    print("=" * 40)

    # 1. Collect all CRB_* env vars
    crb_vars = {k: v for k, v in sorted(os.environ.items()) if k.startswith("CRB_")}

    # Check for .env file
    has_dotenv_vars = _report_dotenv_vars(known_prefixes)

    if not crb_vars:
        if has_dotenv_vars:
            print("\nNo CRB_* environment variables set (values from .env will be used).")
        else:
            print("\nNo CRB_* environment variables set.")
            print("Using all defaults (zero-config mode).")
    else:
        print(f"\nFound {len(crb_vars)} CRB_* variable(s):\n")
        for key, value in crb_vars.items():
            display = _mask_value(key, value)
            known = _is_known_var(key, known_prefixes)
            marker = "" if known else "  ⚠️  UNRECOGNIZED"
            print(f"  {key} = {display}{marker}")

    # 2. Check for unrecognized vars
    unknown = [k for k in crb_vars if not _is_known_var(k, known_prefixes)]
    if unknown:
        print(f"\n⚠️  {len(unknown)} unrecognized variable(s) (possible typos):")
        for k in unknown:
            print(f"  - {k}")

    # 3. Try loading config and validate
    print("\n" + "-" * 40)
    print("Validating configuration...\n")
    try:
        from codereviewbuddy.config import load_config  # noqa: PLC0415

        config = load_config()
    except Exception as exc:
        print(f"❌ Configuration error: {exc}")
        sys.exit(1)

    _print_config_summary(config)

    # 4. Check gh CLI
    print("-" * 40)
    print("Checking gh CLI...\n")
    try:
        from codereviewbuddy import gh  # noqa: PLC0415

        username = gh.check_auth()
        print(f"  ✅ gh CLI authenticated as: {username}")
    except Exception as exc:
        print(f"  ❌ gh CLI error: {exc}")

    print()


def _report_dotenv_vars(known_prefixes: frozenset[str]) -> bool:
    """Print CRB_* variables found in a local .env file, if any. Returns True if found."""
    env_file = Path(".env")
    if not env_file.is_file():
        return False
    from dotenv import dotenv_values  # noqa: PLC0415

    all_vars = dotenv_values(env_file)
    env_crb = sorted(k for k in all_vars if k.startswith("CRB_"))
    if not env_crb:
        return False
    unknown = [k for k in env_crb if not _is_known_var(k, known_prefixes)]
    print(f"\n  .env file found with {len(env_crb)} CRB_* variable(s): {', '.join(env_crb)}")
    if unknown:
        for k in unknown:
            print(f"    ⚠️  {k} — UNRECOGNIZED (possible typo)")
    print("  (These are loaded by pydantic-settings; explicit env vars take priority)")
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MASK_MIN_LENGTH = 4
_TRUNCATE_LENGTH = 80

_KNOWN_ENV_PREFIXES = frozenset({
    "CRB_PR_DESCRIPTIONS",
    "CRB_SELF_IMPROVEMENT",
    "CRB_OWNER_LOGINS",
    "CRB_WORKSPACE",
})


def _build_known_prefixes() -> frozenset[str]:
    """Return set of known CRB_* env var prefixes."""
    return _KNOWN_ENV_PREFIXES


def _is_known_var(key: str, known_prefixes: frozenset[str]) -> bool:
    """Check if a CRB_* var matches a known config prefix."""
    return any(key == prefix or key.startswith(prefix + "__") for prefix in known_prefixes)


def _mask_value(key: str, value: str) -> str:
    """Mask sensitive values."""
    sensitive_keywords = ("token", "secret", "key", "password")
    if any(kw in key.lower() for kw in sensitive_keywords):
        if len(value) > _MASK_MIN_LENGTH:
            return value[:2] + "*" * (len(value) - _MASK_MIN_LENGTH) + value[-2:]
        return "****"
    # Truncate very long values (e.g. JSON blobs)
    if len(value) > _TRUNCATE_LENGTH:
        return value[: _TRUNCATE_LENGTH - 3] + "..."
    return value


def _print_config_summary(config: object) -> None:
    """Print a human-readable config summary."""
    from codereviewbuddy.config import Config  # noqa: PLC0415

    if not isinstance(config, Config):  # pragma: no cover
        return

    print(f"  PR descriptions: {'enabled' if config.pr_descriptions.enabled else 'DISABLED'}")

    si = config.self_improvement
    if si.enabled:
        print("  Self-improvement: enabled")
    else:
        print("  Self-improvement: disabled")

    if config.owner_logins:
        print(f"  Owner logins: {', '.join(config.owner_logins)}")
    else:
        print("  Owner logins: not set (owner-reply filtering disabled)")
    print()
