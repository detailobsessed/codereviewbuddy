"""Tests for the github_api module (httpx-based GitHub client)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import respx
from httpx import Response

from codereviewbuddy.github_api import (
    _HTTP_FORBIDDEN,
    _HTTP_UNAUTHORIZED,
    GitHubAuthError,
    GitHubError,
    _parse_next_link,
    _raise_for_status,
    _resolve_token_sync,
    download_bytes,
    graphql,
    parse_repo,
    reset_token,
    rest,
)

# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------


class TestGitHubAuthError:
    def test_default_message_contains_setup_url(self):
        err = GitHubAuthError()
        assert "GH_TOKEN" in str(err)
        assert "github.com/settings/tokens" in str(err)

    def test_detail_prepended(self):
        err = GitHubAuthError("Access denied")
        assert str(err).startswith("Access denied")
        assert "GH_TOKEN" in str(err)

    def test_status_code_is_401(self):
        assert GitHubAuthError().status_code == _HTTP_UNAUTHORIZED


# ---------------------------------------------------------------------------
# parse_repo
# ---------------------------------------------------------------------------


class TestParseRepo:
    def test_valid_repo(self):
        assert parse_repo("owner/repo") == ("owner", "repo")

    def test_raises_on_missing_slash(self):
        with pytest.raises(GitHubError, match="Invalid repo format"):
            parse_repo("noslash")

    def test_raises_on_empty_repo_part(self):
        with pytest.raises(GitHubError, match="Invalid repo format"):
            parse_repo("owner/")


# ---------------------------------------------------------------------------
# _resolve_token_sync
# ---------------------------------------------------------------------------


class TestResolveTokenSync:
    def test_returns_gh_token_env(self, monkeypatch):
        monkeypatch.setenv("GH_TOKEN", "tok_from_env")
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        assert _resolve_token_sync() == "tok_from_env"

    def test_returns_github_token_env(self, monkeypatch):
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "tok_github")
        assert _resolve_token_sync() == "tok_github"

    def test_falls_back_to_gh_auth_token(self, monkeypatch):
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "ghp_fallback\n"
        with patch("subprocess.run", return_value=mock_result):
            assert _resolve_token_sync() == "ghp_fallback"

    def test_returns_none_when_gh_not_found(self, monkeypatch):
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert _resolve_token_sync() is None

    def test_returns_none_when_gh_fails(self, monkeypatch):
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        with patch("subprocess.run", return_value=mock_result):
            assert _resolve_token_sync() is None


# ---------------------------------------------------------------------------
# reset_token / get_token
# ---------------------------------------------------------------------------


class TestGetToken:
    async def test_raises_when_no_token(self, monkeypatch):
        from codereviewbuddy import github_api

        reset_token()
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        with patch("subprocess.run", side_effect=FileNotFoundError), pytest.raises(GitHubAuthError):
            await github_api.get_token()
        reset_token()

    async def test_returns_token_from_env(self, monkeypatch):
        from codereviewbuddy import github_api

        reset_token()
        monkeypatch.setenv("GH_TOKEN", "tok_test")
        result = await github_api.get_token()
        assert result == "tok_test"
        reset_token()


# ---------------------------------------------------------------------------
# _raise_for_status
# ---------------------------------------------------------------------------


class TestRaiseForStatus:
    def test_success_does_not_raise(self):
        _raise_for_status(Response(200))

    def test_401_raises_auth_error(self):
        with pytest.raises(GitHubAuthError):
            _raise_for_status(Response(401))

    def test_403_rate_limit_raises_github_error(self):
        with pytest.raises(GitHubError, match="rate limit"):
            _raise_for_status(
                Response(403, json={"message": "API rate limit exceeded for ..."}),
            )

    def test_403_forbidden_raises_auth_error(self):
        with pytest.raises(GitHubAuthError, match="forbidden"):
            _raise_for_status(Response(403, json={"message": "Forbidden"}))

    def test_500_raises_github_error(self):
        with pytest.raises(GitHubError, match="500"):
            _raise_for_status(Response(500, json={"message": "Internal Server Error"}))

    def test_non_json_body_uses_text(self):
        with pytest.raises(GitHubError):
            _raise_for_status(Response(422, text="Unprocessable"))

    def test_constants_are_correct(self):
        assert _HTTP_UNAUTHORIZED == 401
        assert _HTTP_FORBIDDEN == 403


# ---------------------------------------------------------------------------
# _parse_next_link
# ---------------------------------------------------------------------------


class TestParseNextLink:
    def test_parses_next_link(self):
        header = '<https://api.github.com/repos?page=2>; rel="next", <https://api.github.com/repos?page=5>; rel="last"'
        assert _parse_next_link(header) == "https://api.github.com/repos?page=2"

    def test_returns_none_when_no_next(self):
        assert _parse_next_link('<https://api.github.com/repos?page=1>; rel="prev"') is None

    def test_returns_none_for_empty_string(self):
        assert _parse_next_link("") is None


# ---------------------------------------------------------------------------
# graphql
# ---------------------------------------------------------------------------


class TestGraphQL:
    async def test_successful_query(self, monkeypatch):
        from codereviewbuddy import cache

        reset_token()
        monkeypatch.setenv("GH_TOKEN", "tok_test")
        cache.clear()

        with respx.mock:
            respx.post("https://api.github.com/graphql").mock(
                return_value=Response(200, json={"data": {"viewer": {"login": "user"}}}),
            )
            result = await graphql("{ viewer { login } }")

        assert result["data"]["viewer"]["login"] == "user"
        reset_token()
        cache.clear()

    async def test_graphql_errors_raise(self, monkeypatch):
        from codereviewbuddy import cache

        reset_token()
        monkeypatch.setenv("GH_TOKEN", "tok_test")
        cache.clear()

        with respx.mock:
            respx.post("https://api.github.com/graphql").mock(
                return_value=Response(200, json={"errors": [{"message": "Not found"}]}),
            )
            with pytest.raises(GitHubError, match="Not found"):
                await graphql("{ viewer { login } }")

        reset_token()
        cache.clear()

    async def test_mutation_clears_cache(self, monkeypatch):
        from codereviewbuddy import cache

        reset_token()
        monkeypatch.setenv("GH_TOKEN", "tok_test")
        cache.clear()

        with respx.mock:
            respx.post("https://api.github.com/graphql").mock(
                return_value=Response(200, json={"data": {"resolveReviewThread": {"thread": {"id": "t1"}}}}),
            )
            result = await graphql('mutation { resolveReviewThread(input: {threadId: "t1"}) { thread { id } } }')

        assert "data" in result
        reset_token()
        cache.clear()

    async def test_uses_cache_on_second_call(self, monkeypatch):
        from codereviewbuddy import cache

        reset_token()
        monkeypatch.setenv("GH_TOKEN", "tok_test")
        cache.clear()

        with respx.mock:
            route = respx.post("https://api.github.com/graphql").mock(
                return_value=Response(200, json={"data": {"viewer": {"login": "cached"}}}),
            )
            await graphql("{ viewer { login } }")
            await graphql("{ viewer { login } }")
            assert route.call_count == 1  # second call served from cache

        reset_token()
        cache.clear()


# ---------------------------------------------------------------------------
# rest
# ---------------------------------------------------------------------------


class TestRest:
    async def test_get_request(self, monkeypatch):
        from codereviewbuddy import cache

        reset_token()
        monkeypatch.setenv("GH_TOKEN", "tok_test")
        cache.clear()

        with respx.mock:
            respx.get("https://api.github.com/repos/o/r/pulls").mock(
                return_value=Response(200, json=[{"number": 1}]),
            )
            result = await rest("/repos/o/r/pulls")

        assert result == [{"number": 1}]
        reset_token()
        cache.clear()

    async def test_post_request(self, monkeypatch):
        from codereviewbuddy import cache

        reset_token()
        monkeypatch.setenv("GH_TOKEN", "tok_test")
        cache.clear()

        with respx.mock:
            respx.post("https://api.github.com/repos/o/r/issues/1/comments").mock(
                return_value=Response(201, json={"id": 42}),
            )
            result = await rest("/repos/o/r/issues/1/comments", method="POST", body="hello")

        assert result == {"id": 42}
        reset_token()
        cache.clear()

    async def test_paginate_follows_link_header(self, monkeypatch):
        from codereviewbuddy import cache

        reset_token()
        monkeypatch.setenv("GH_TOKEN", "tok_test")
        cache.clear()

        page2_url = "https://api.github.com/repos/o/r/pulls?page=2"
        responses = [
            Response(200, json=[{"number": 1}], headers={"link": f'<{page2_url}>; rel="next"'}),
            Response(200, json=[{"number": 2}]),
        ]

        with respx.mock:
            respx.get(url__regex=r"https://api\.github\.com/repos/o/r/pulls").mock(
                side_effect=responses,
            )
            result = await rest("/repos/o/r/pulls", paginate=True)

        assert result == [{"number": 1}, {"number": 2}]
        reset_token()
        cache.clear()

    async def test_empty_response_returns_none(self, monkeypatch):
        from codereviewbuddy import cache

        reset_token()
        monkeypatch.setenv("GH_TOKEN", "tok_test")
        cache.clear()

        with respx.mock:
            respx.delete("https://api.github.com/repos/o/r/issues/1").mock(
                return_value=Response(204, content=b""),
            )
            result = await rest("/repos/o/r/issues/1", method="DELETE")

        assert result is None
        reset_token()
        cache.clear()


# ---------------------------------------------------------------------------
# download_bytes
# ---------------------------------------------------------------------------


class TestDownloadBytes:
    async def test_downloads_bytes(self, monkeypatch):
        reset_token()
        monkeypatch.setenv("GH_TOKEN", "tok_test")

        with respx.mock:
            respx.get("https://example.com/archive.zip").mock(
                return_value=Response(200, content=b"PK\x03\x04"),
            )
            result = await download_bytes("https://example.com/archive.zip")

        assert result == b"PK\x03\x04"
        reset_token()

    async def test_raises_on_http_error(self, monkeypatch):
        reset_token()
        monkeypatch.setenv("GH_TOKEN", "tok_test")

        with respx.mock:
            respx.get("https://example.com/archive.zip").mock(
                return_value=Response(404),
            )
            with pytest.raises(GitHubError):
                await download_bytes("https://example.com/archive.zip")

        reset_token()
