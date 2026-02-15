"""Global test fixtures for codereviewbuddy."""

from __future__ import annotations

import pytest

from codereviewbuddy.config import Config, set_config


@pytest.fixture(autouse=True)
def _default_config():
    """Reset config to defaults before every test.

    The committed .codereviewbuddy.toml may disable reviewers (e.g. Devin),
    which breaks tests that expect all reviewers enabled. This fixture
    ensures every test starts with a clean default config.
    """
    set_config(Config())
    yield
    set_config(Config())
