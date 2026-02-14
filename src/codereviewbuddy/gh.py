"""GitHub CLI (gh) wrapper for codereviewbuddy.

All GitHub API calls go through the `gh` CLI, which handles authentication
transparently. No PAT tokens or .env files needed.
"""

from __future__ import annotations

import json
import logging
import subprocess  # noqa: S404
import time
from pathlib import Path
from typing import Any

from codereviewbuddy import cache

logger = logging.getLogger(__name__)

_GH_LOG_DIR = Path.home() / ".codereviewbuddy"
_GH_LOG_FILE = _GH_LOG_DIR / "gh_calls.jsonl"
_MAX_GH_LOG_LINES = 10_000
_GH_ROTATE_EVERY_WRITES = 100
_gh_log_state: dict[str, int] = {"write_count": 0}
# Unique sentinel to grep/remove all temporary diagnostics once issue #65 is resolved.
_ISSUE_65_TRACKING_TAG = "CRB-ISSUE-65-TRACKING"


def _truncate_gh_log_if_needed() -> None:
    """Keep only the last N gh call log entries."""
    try:
        if not _GH_LOG_FILE.exists():
            return
        lines = _GH_LOG_FILE.read_text(encoding="utf-8").splitlines()
        if len(lines) <= _MAX_GH_LOG_LINES:
            return
        _GH_LOG_FILE.write_text("\n".join(lines[-_MAX_GH_LOG_LINES:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def _log_gh_call(entry: dict[str, Any]) -> None:
    """Append a JSON log entry to gh_calls.jsonl."""
    try:
        _GH_LOG_DIR.mkdir(parents=True, exist_ok=True)
        with _GH_LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        _gh_log_state["write_count"] += 1
        if _gh_log_state["write_count"] % _GH_ROTATE_EVERY_WRITES == 0:
            _truncate_gh_log_if_needed()
    except OSError:
        pass


def _summarize_cmd(args: tuple[str, ...]) -> str:
    """Build a short summary of the gh command for logging."""
    # e.g. ("api", "graphql", "-f", "query=...") -> "api graphql"
    # e.g. ("pr", "comment", "42", ...) -> "pr comment 42"
    summary_parts: list[str] = []
    for arg in args:
        if arg.startswith("-") or "=" in arg:
            break
        summary_parts.append(arg)
    return " ".join(summary_parts) or "unknown"


class GhError(Exception):
    """Raised when a gh CLI command fails."""

    def __init__(self, message: str, stderr: str = "", returncode: int = 1) -> None:
        super().__init__(message)
        self.stderr = stderr
        self.returncode = returncode


class GhNotFoundError(GhError):
    """Raised when gh CLI is not installed."""

    def __init__(self) -> None:
        super().__init__("gh CLI not found. Install it: https://cli.github.com/ then run: gh auth login")


class GhNotAuthenticatedError(GhError):
    """Raised when gh CLI is not authenticated."""

    def __init__(self, stderr: str = "") -> None:
        super().__init__(
            "gh CLI is not authenticated. Run: gh auth login",
            stderr=stderr,
        )


def run_gh(*args: str, cwd: str | None = None) -> str:
    """Run a gh CLI command and return stdout.

    Args:
        *args: Arguments to pass to gh (e.g. "api", "graphql", "-f", "query=...").
        cwd: Working directory for the command.

    Returns:
        stdout as a string.

    Raises:
        GhNotFoundError: If gh is not installed.
        GhError: If the command fails.
    """
    cmd = ["gh", *args]
    cmd_summary = _summarize_cmd(args)
    logger.debug("Running: %s", " ".join(cmd))
    start = time.perf_counter()
    start_ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    try:
        result = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            check=False,
            cwd=cwd,
        )
    except FileNotFoundError:
        duration_ms = round((time.perf_counter() - start) * 1000)
        _log_gh_call({
            "ts": start_ts,
            "cmd": cmd_summary,
            "duration_ms": duration_ms,
            "error": "FileNotFoundError",
            "tracking_tag": _ISSUE_65_TRACKING_TAG,
        })
        raise GhNotFoundError from None

    duration_ms = round((time.perf_counter() - start) * 1000)
    stderr_text = result.stderr.strip()
    _log_gh_call({
        "ts": start_ts,
        "ts_end": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "cmd": cmd_summary,
        "duration_ms": duration_ms,
        "exit_code": result.returncode,
        "stdout_bytes": len(result.stdout),
        "stderr": stderr_text[:500] if stderr_text else None,
        "tracking_tag": _ISSUE_65_TRACKING_TAG,
    })

    if result.returncode != 0:
        logger.debug("gh stderr: %s", result.stderr)
        raise GhError(
            result.stderr.strip(),
            stderr=result.stderr,
            returncode=result.returncode,
        )
    return result.stdout


def graphql(query: str, variables: dict[str, Any] | None = None, cwd: str | None = None) -> dict[str, Any]:
    """Execute a GitHub GraphQL query via gh api graphql.

    Queries are cached with a short TTL to avoid redundant API calls
    when multiple tools fetch the same data. Mutations bypass and
    invalidate the cache.

    Args:
        query: GraphQL query string.
        variables: Optional variables to pass with -f (strings) or -F (ints/bools).
        cwd: Working directory.

    Returns:
        Parsed JSON response.
    """
    is_mutation = query.strip().lower().startswith("mutation")

    if not is_mutation:
        key = cache.make_key("graphql", query, variables)
        cached = cache.get(key)
        if cached is not cache._SENTINEL:
            return cached

    args = ["api", "graphql", "-f", f"query={query}"]
    for key_, value in (variables or {}).items():
        if isinstance(value, int | bool):
            args.extend(["-F", f"{key_}={value}"])
        else:
            args.extend(["-f", f"{key_}={value}"])

    raw = run_gh(*args, cwd=cwd)
    result = json.loads(raw)

    if is_mutation:
        cache.clear()
    else:
        cache.put(key, result)

    return result


def rest(
    endpoint: str,
    method: str = "GET",
    cwd: str | None = None,
    *,
    paginate: bool = False,
    **kwargs: str,
) -> Any:
    """Execute a GitHub REST API call via gh api.

    GET requests are cached with a short TTL. Non-GET requests
    bypass and invalidate the cache.

    Args:
        endpoint: REST API endpoint (e.g. "/repos/{owner}/{repo}/pulls").
        method: HTTP method.
        cwd: Working directory.
        paginate: If True, pass ``--paginate --slurp`` to gh api to follow
            Link headers. ``--slurp`` wraps pages in an outer JSON array;
            the result is then flattened to a single contiguous list.
        **kwargs: Additional -f parameters.

    Returns:
        Parsed JSON response.
    """
    is_read = method.upper() == "GET"

    if is_read:
        key = cache.make_key("rest", endpoint, method, paginate, kwargs)
        cached = cache.get(key)
        if cached is not cache._SENTINEL:
            return cached

    args = ["api", endpoint, "--method", method]
    if paginate:
        args.extend(["--paginate", "--slurp"])
    for key_, value in kwargs.items():
        args.extend(["-f", f"{key_}={value}"])

    raw = run_gh(*args, cwd=cwd)
    result = None if not raw.strip() else json.loads(raw)

    # --slurp wraps pages in an outer array: [[...page1...], [...page2...]].
    # Flatten to a single list so callers see one contiguous array.
    if paginate and isinstance(result, list) and result and isinstance(result[0], list):
        result = [item for page in result for item in page]

    if is_read:
        cache.put(key, result)
    else:
        cache.clear()

    return result


def check_auth(cwd: str | None = None) -> str:
    """Verify gh CLI is installed and authenticated.

    Returns:
        The authenticated GitHub username.

    Raises:
        GhNotFoundError: If gh is not installed.
        GhNotAuthenticatedError: If not authenticated.
    """
    try:
        result = run_gh("auth", "status", cwd=cwd)
    except GhError as e:
        raise GhNotAuthenticatedError(stderr=e.stderr) from e

    # Extract username from output like "Logged in to github.com account username"
    for line in result.splitlines():
        if "account" in line.lower():
            parts = line.split()
            for i, part in enumerate(parts):
                if part.lower() == "account" and i + 1 < len(parts):
                    return parts[i + 1].strip("()")
    return "authenticated"


def get_current_pr_number(cwd: str | None = None) -> int:
    """Detect the PR number associated with the current git branch.

    Uses ``gh pr view`` which resolves the current branch to its open PR.

    Returns:
        The PR number.

    Raises:
        GhError: If no PR is associated with the current branch.
    """
    raw = run_gh("pr", "view", "--json", "number", "-q", ".number", cwd=cwd)
    return int(raw.strip())


def get_repo_info(cwd: str | None = None) -> tuple[str, str]:
    """Get the owner and repo name for the current repository.

    Returns:
        Tuple of (owner, repo).
    """
    raw = run_gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner", cwd=cwd)
    owner_repo = raw.strip()
    owner, repo = owner_repo.split("/", 1)
    return owner, repo
