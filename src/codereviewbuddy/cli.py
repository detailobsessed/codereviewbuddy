"""CLI for codereviewbuddy — built on cyclopts (same framework as FastMCP's CLI)."""

from __future__ import annotations

import os
import sys

import cyclopts

app = cyclopts.App(
    name="codereviewbuddy",
    help="codereviewbuddy — AI code review buddy MCP server.",
)


@app.default
def serve() -> None:
    """Run the codereviewbuddy MCP server (default command)."""
    from codereviewbuddy.io_tap import install_io_tap  # noqa: PLC0415
    from codereviewbuddy.server import mcp  # noqa: PLC0415

    install_io_tap()
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

    if not crb_vars:
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MASK_MIN_LENGTH = 4
_TRUNCATE_LENGTH = 80

_KNOWN_ENV_PREFIXES = frozenset({
    "CRB_REVIEWERS",
    "CRB_PR_DESCRIPTIONS",
    "CRB_SELF_IMPROVEMENT",
    "CRB_DIAGNOSTICS",
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

    print("  Reviewers:")
    for name, rc in sorted(config.reviewers.items()):
        status = "enabled" if rc.enabled else "DISABLED"
        levels = ", ".join(s.value for s in rc.resolve_levels) or "none"
        stale = "yes" if rc.auto_resolve_stale else "no"
        reply = "required" if rc.require_reply_before_resolve else "not required"
        print(f"    {name}: {status}")
        print(f"      resolve_levels: [{levels}]")
        print(f"      auto_resolve_stale: {stale}")
        print(f"      reply_before_resolve: {reply}")

    print(f"\n  PR descriptions: {'enabled' if config.pr_descriptions.enabled else 'DISABLED'}")

    si = config.self_improvement
    if si.enabled:
        print(f"  Self-improvement: enabled → {si.repo}")
    else:
        print("  Self-improvement: disabled")

    diag = config.diagnostics
    diag_flags = []
    if diag.io_tap:
        diag_flags.append("io_tap")
    if diag.tool_call_heartbeat:
        diag_flags.append(f"heartbeat({diag.heartbeat_interval_ms}ms)")
    if diag.include_args_fingerprint:
        diag_flags.append("args_fingerprint")
    print(f"  Diagnostics: {', '.join(diag_flags) if diag_flags else 'all off'}")
    print()
