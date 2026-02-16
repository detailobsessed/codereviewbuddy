"""Global test fixtures for codereviewbuddy."""

from __future__ import annotations

import pytest

from codereviewbuddy.config import Config, set_config


@pytest.fixture(autouse=True)
def _default_config():
    """Reset config to defaults before every test.

    Ensures every test starts with a clean default config, unaffected
    by CRB_* env vars that may be set in the developer's shell.
    """
    set_config(Config())
    yield
    set_config(Config())
