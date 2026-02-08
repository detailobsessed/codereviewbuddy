"""Tests for version checking tool."""

from __future__ import annotations

import importlib.metadata
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

from codereviewbuddy.tools.version import check_for_updates


class TestCheckForUpdates:
    async def test_update_available(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.version._get_current_version", return_value="1.0.0")

        mock_response = mocker.Mock()
        mock_response.status_code = 200
        mock_response.raise_for_status = mocker.Mock()
        mock_response.json.return_value = {"info": {"version": "2.0.0"}}

        mock_client = mocker.AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = mocker.AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = mocker.AsyncMock(return_value=False)
        mocker.patch("codereviewbuddy.tools.version.httpx.AsyncClient", return_value=mock_client)

        result = await check_for_updates()
        assert result.current_version == "1.0.0"
        assert result.latest_version == "2.0.0"
        assert result.update_available is True
        assert "uvx --upgrade" in result.upgrade_command

    async def test_already_latest(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.version._get_current_version", return_value="2.0.0")

        mock_response = mocker.Mock()
        mock_response.status_code = 200
        mock_response.raise_for_status = mocker.Mock()
        mock_response.json.return_value = {"info": {"version": "2.0.0"}}

        mock_client = mocker.AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = mocker.AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = mocker.AsyncMock(return_value=False)
        mocker.patch("codereviewbuddy.tools.version.httpx.AsyncClient", return_value=mock_client)

        result = await check_for_updates()
        assert result.current_version == "2.0.0"
        assert result.latest_version == "2.0.0"
        assert result.update_available is False

    async def test_pypi_unreachable(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.version._get_current_version", return_value="1.0.0")

        mock_client = mocker.AsyncMock()
        mock_client.get.side_effect = httpx.ConnectError("Connection refused")
        mock_client.__aenter__ = mocker.AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = mocker.AsyncMock(return_value=False)
        mocker.patch("codereviewbuddy.tools.version.httpx.AsyncClient", return_value=mock_client)

        result = await check_for_updates()
        assert result.current_version == "1.0.0"
        assert result.latest_version == "unknown"
        assert result.update_available is False

    async def test_pypi_timeout(self, mocker: MockerFixture):
        mocker.patch("codereviewbuddy.tools.version._get_current_version", return_value="1.0.0")

        mock_client = mocker.AsyncMock()
        mock_client.get.side_effect = httpx.TimeoutException("Timed out")
        mock_client.__aenter__ = mocker.AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = mocker.AsyncMock(return_value=False)
        mocker.patch("codereviewbuddy.tools.version.httpx.AsyncClient", return_value=mock_client)

        result = await check_for_updates()
        assert result.current_version == "1.0.0"
        assert result.latest_version == "unknown"
        assert result.update_available is False

    async def test_current_ahead_of_pypi(self, mocker: MockerFixture):
        """Dev version ahead of PyPI — no update available."""
        mocker.patch("codereviewbuddy.tools.version._get_current_version", return_value="3.0.0")

        mock_response = mocker.Mock()
        mock_response.status_code = 200
        mock_response.raise_for_status = mocker.Mock()
        mock_response.json.return_value = {"info": {"version": "2.0.0"}}

        mock_client = mocker.AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = mocker.AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = mocker.AsyncMock(return_value=False)
        mocker.patch("codereviewbuddy.tools.version.httpx.AsyncClient", return_value=mock_client)

        result = await check_for_updates()
        assert result.update_available is False

    async def test_invalid_version_string(self, mocker: MockerFixture):
        """Malformed version from PyPI doesn't crash."""
        mocker.patch("codereviewbuddy.tools.version._get_current_version", return_value="1.0.0")

        mock_response = mocker.Mock()
        mock_response.status_code = 200
        mock_response.raise_for_status = mocker.Mock()
        mock_response.json.return_value = {"info": {"version": "not-a-version"}}

        mock_client = mocker.AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = mocker.AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = mocker.AsyncMock(return_value=False)
        mocker.patch("codereviewbuddy.tools.version.httpx.AsyncClient", return_value=mock_client)

        result = await check_for_updates()
        assert result.update_available is False
        assert result.latest_version == "not-a-version"

    async def test_package_not_installed(self, mocker: MockerFixture):
        """Missing package metadata doesn't crash."""
        mocker.patch(
            "codereviewbuddy.tools.version.importlib.metadata.version",
            side_effect=importlib.metadata.PackageNotFoundError("codereviewbuddy"),
        )

        mock_response = mocker.Mock()
        mock_response.status_code = 200
        mock_response.raise_for_status = mocker.Mock()
        mock_response.json.return_value = {"info": {"version": "1.0.0"}}

        mock_client = mocker.AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = mocker.AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = mocker.AsyncMock(return_value=False)
        mocker.patch("codereviewbuddy.tools.version.httpx.AsyncClient", return_value=mock_client)

        result = await check_for_updates()
        assert result.current_version == "unknown"
        assert result.update_available is False
