"""Direct GitHub API client using httpx with PAT authentication.

Authentication priority (resolved once at startup, then cached):
1. ``GH_TOKEN`` env var
2. ``GITHUB_TOKEN`` env var
3. ``gh auth token`` subprocess — reads local ``~/.config/gh/hosts.yml``, no network
4. Raises :exc:`GitHubAuthError` with setup URL

New users: https://github.com/settings/tokens/new?scopes=repo&description=codereviewbuddy
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess  # noqa: S404
from typing import Any

import httpx

from codereviewbuddy import cache

logger = logging.getLogger(__name__)

_GITHUB_API_URL = "https://api.github.com"
_GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
TOKEN_CREATE_URL = "https://github.com/settings/tokens/new?scopes=repo&description=codereviewbuddy"  # noqa: S105

_token: str | None = None
_token_resolved: bool = False


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------


class GitHubError(Exception):
    """Raised when a GitHub API call fails."""

    def __init__(self, message: str, status_code: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitHubAuthError(GitHubError):
    """Raised when GitHub authentication fails or no token is available."""

    def __init__(self, detail: str = "") -> None:
        msg = (
            "GitHub token not found. "
            "Set GH_TOKEN or GITHUB_TOKEN env var, or run 'gh auth login'.\n"
            f"Create a token (permissions pre-filled): {TOKEN_CREATE_URL}"
        )
        if detail:
            msg = f"{detail}\n{msg}"
        super().__init__(msg, status_code=401)


# ---------------------------------------------------------------------------
# Repo parsing
# ---------------------------------------------------------------------------


def parse_repo(repo: str) -> tuple[str, str]:
    """Parse an ``owner/repo`` string into a ``(owner, repo_name)`` tuple.

    Raises:
        GitHubError: If the string is not in ``owner/repo`` format.
    """
    owner, _, repo_name = repo.partition("/")
    if not repo_name:
        msg = f"Invalid repo format {repo!r}. Expected 'owner/repo'."
        raise GitHubError(msg)
    return owner, repo_name


# ---------------------------------------------------------------------------
# Token resolution
# ---------------------------------------------------------------------------


def _resolve_token_sync() -> str | None:
    """Resolve GitHub token synchronously. Safe to run in a thread.

    Tries (in order):
    1. ``GH_TOKEN`` env var
    2. ``GITHUB_TOKEN`` env var
    3. ``gh auth token`` — reads local ``~/.config/gh/hosts.yml``, no network
    """
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        logger.debug("GitHub token resolved from env var")
        return token

    try:
        result = subprocess.run(
            ["gh", "auth", "token"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            logger.debug("GitHub token resolved from gh auth token")
            return result.stdout.strip()
    except FileNotFoundError, subprocess.TimeoutExpired, OSError:
        pass

    return None


async def get_token() -> str:
    """Return the GitHub token, resolving it lazily on first call.

    Raises:
        GitHubAuthError: If no token can be found.
    """
    global _token, _token_resolved  # noqa: PLW0603
    if not _token_resolved:
        _token = await asyncio.to_thread(_resolve_token_sync)
        _token_resolved = True
    if _token is None:
        raise GitHubAuthError
    return _token


def reset_token() -> None:
    """Reset cached token (for testing)."""
    global _token, _token_resolved  # noqa: PLW0603
    _token = None
    _token_resolved = False


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


async def _get_headers() -> dict[str, str]:
    """Build GitHub API request headers with resolved auth token."""
    token = await get_token()
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


_HTTP_UNAUTHORIZED = 401
_HTTP_FORBIDDEN = 403


def _raise_for_status(response: httpx.Response) -> None:
    """Raise appropriate :exc:`GitHubError` subclass for non-2xx responses."""
    if response.is_success:
        return

    if response.status_code == _HTTP_UNAUTHORIZED:
        raise GitHubAuthError

    try:
        body = response.json()
        msg = body.get("message", response.text)
    except Exception:
        msg = response.text

    if response.status_code == _HTTP_FORBIDDEN:
        if "rate limit" in msg.lower():
            msg = f"GitHub API rate limit exceeded: {msg}"
            raise GitHubError(msg, status_code=_HTTP_FORBIDDEN)
        msg = f"GitHub API access forbidden: {msg}"
        raise GitHubAuthError(msg)

    msg = f"GitHub API error {response.status_code}: {msg}"
    raise GitHubError(msg, status_code=response.status_code)


def _parse_next_link(link_header: str) -> str | None:
    """Parse a ``Link:`` header and return the ``next`` URL if present."""
    if not link_header:
        return None
    match = re.search(r'<([^>]+)>;\s*rel="next"', link_header)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# GraphQL
# ---------------------------------------------------------------------------


async def graphql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    """Execute a GitHub GraphQL query or mutation.

    Queries are cached with a short TTL. Mutations bypass and invalidate the cache.

    Args:
        query: GraphQL query or mutation string.
        variables: Optional variables dict.

    Returns:
        Parsed JSON response dict (full envelope including ``data``).

    Raises:
        GitHubError: On GraphQL errors or HTTP failure.
        GitHubAuthError: On authentication failure.
    """
    is_mutation = query.strip().lower().startswith("mutation")

    if not is_mutation:
        key = cache.make_key("graphql", query, variables)
        cached = cache.get(key)
        if cached is not cache._SENTINEL:
            return cached  # type: ignore[return-value]

    headers = await _get_headers()
    payload: dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = variables

    logger.debug("GraphQL %s", "mutation" if is_mutation else "query")
    async with httpx.AsyncClient() as client:
        response = await client.post(_GITHUB_GRAPHQL_URL, headers=headers, json=payload)

    _raise_for_status(response)
    result: dict[str, Any] = response.json()

    errors = result.get("errors")
    if errors:
        messages = "; ".join(e.get("message", str(e)) for e in errors)
        msg = f"GraphQL error: {messages}"
        raise GitHubError(msg)

    if is_mutation:
        cache.clear()
    else:
        cache.put(key, result)  # type: ignore[possibly-undefined]

    return result


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


async def rest(
    endpoint: str,
    method: str = "GET",
    *,
    paginate: bool = False,
    **kwargs: Any,
) -> Any:
    """Execute a GitHub REST API call.

    GET requests are cached. Non-GET requests bypass and invalidate the cache.

    Args:
        endpoint: REST API endpoint path (e.g. ``/repos/owner/repo/pulls``).
            Query parameters may be embedded directly in the path.
        method: HTTP method (default ``GET``).
        paginate: If ``True``, follow ``Link:`` headers to collect all pages.
            Returns a flat list combining all page results.
        **kwargs: Additional query parameters (GET) or JSON body fields (non-GET).

    Returns:
        Parsed JSON response, or a flat list when ``paginate=True``.

    Raises:
        GitHubError: On HTTP failure.
        GitHubAuthError: On authentication failure.
    """
    is_read = method.upper() == "GET"

    if is_read:
        key = cache.make_key("rest", endpoint, method, paginate, kwargs)
        cached = cache.get(key)
        if cached is not cache._SENTINEL:
            return cached

    url = f"{_GITHUB_API_URL}{endpoint}"
    headers = await _get_headers()

    if paginate:
        result = await _paginate_rest(url, headers, **kwargs)
    else:
        result = await _single_rest(url, method, headers, **kwargs)

    if is_read:
        cache.put(key, result)  # type: ignore[possibly-undefined]
    else:
        cache.clear()

    return result


async def _single_rest(url: str, method: str, headers: dict[str, str], **kwargs: Any) -> Any:
    """Make a single REST request and return parsed JSON."""
    upper = method.upper()
    params = dict(kwargs) if upper == "GET" and kwargs else None
    json_body = dict(kwargs) if upper != "GET" and kwargs else None

    async with httpx.AsyncClient() as client:
        response = await client.request(method, url, headers=headers, params=params, json=json_body)

    _raise_for_status(response)
    if not response.content:
        return None
    return response.json()


async def _paginate_rest(url: str, headers: dict[str, str], **kwargs: Any) -> list[Any]:
    """Follow ``Link:`` headers to collect all pages into a flat list."""
    results: list[Any] = []
    next_url: str | None = url
    first = True

    async with httpx.AsyncClient() as client:
        while next_url:
            params = dict(kwargs) if first and kwargs else None
            response = await client.get(next_url, headers=headers, params=params)
            _raise_for_status(response)
            page = response.json()
            if isinstance(page, list):
                results.extend(page)
            elif page is not None:
                results.append(page)
            next_url = _parse_next_link(response.headers.get("link", ""))
            first = False

    return results


# ---------------------------------------------------------------------------
# Binary downloads
# ---------------------------------------------------------------------------


async def download_bytes(url: str) -> bytes:
    """Download raw bytes from a URL, following redirects.

    Used for GitHub Actions log zip downloads.

    Args:
        url: The URL to download from (may redirect).

    Returns:
        Raw response bytes.

    Raises:
        GitHubError: On HTTP failure.
        GitHubAuthError: On authentication failure.
    """
    headers = await _get_headers()
    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.get(url, headers=headers)
    _raise_for_status(response)
    return response.content
