"""GitHub CLI (gh) wrapper for codereviewbuddy.

All GitHub API calls go through the `gh` CLI, which handles authentication
transparently. No PAT tokens or .env files needed.
"""

from __future__ import annotations

import json
import logging
import subprocess  # noqa: S404
from typing import Any

from codereviewbuddy import cache

logger = logging.getLogger(__name__)


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
    logger.debug("Running: %s", " ".join(cmd))
    try:
        result = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            check=False,
            cwd=cwd,
        )
    except FileNotFoundError:
        raise GhNotFoundError from None

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


def rest(endpoint: str, method: str = "GET", cwd: str | None = None, **kwargs: str) -> Any:
    """Execute a GitHub REST API call via gh api.

    GET requests are cached with a short TTL. Non-GET requests
    bypass and invalidate the cache.

    Args:
        endpoint: REST API endpoint (e.g. "/repos/{owner}/{repo}/pulls").
        method: HTTP method.
        cwd: Working directory.
        **kwargs: Additional -f parameters.

    Returns:
        Parsed JSON response.
    """
    is_read = method.upper() == "GET"

    if is_read:
        key = cache.make_key("rest", endpoint, method, kwargs)
        cached = cache.get(key)
        if cached is not cache._SENTINEL:
            return cached

    args = ["api", endpoint, "--method", method]
    for key_, value in kwargs.items():
        args.extend(["-f", f"{key_}={value}"])

    raw = run_gh(*args, cwd=cwd)
    result = None if not raw.strip() else json.loads(raw)

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


def get_repo_info(cwd: str | None = None) -> tuple[str, str]:
    """Get the owner and repo name for the current repository.

    Returns:
        Tuple of (owner, repo).
    """
    raw = run_gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner", cwd=cwd)
    owner_repo = raw.strip()
    owner, repo = owner_repo.split("/", 1)
    return owner, repo
