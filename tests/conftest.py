"""Global test fixtures for codereviewbuddy."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from codereviewbuddy.config import Config, set_config

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


@pytest.fixture(autouse=True)
def _default_config():
    """Reset config to defaults before every test.

    Ensures every test starts with a clean default config, unaffected
    by CRB_* env vars that may be set in the developer's shell.
    """
    set_config(Config())
    yield
    set_config(Config())


@pytest.fixture
def patch_server_context(mocker: MockerFixture):
    """Patch server context and workspace for tool handler tests."""
    ctx = mocker.MagicMock()
    mocker.patch("codereviewbuddy.server.get_context", return_value=ctx)
    mocker.patch("codereviewbuddy.server._get_workspace_cwd", return_value="/tmp")  # noqa: S108
    return ctx
