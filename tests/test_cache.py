"""Tests for the TTL cache module."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

from codereviewbuddy import cache


class TestMakeKey:
    def test_same_args_same_key(self):
        k1 = cache.make_key("graphql", "query { foo }", {"pr": 42})
        k2 = cache.make_key("graphql", "query { foo }", {"pr": 42})
        assert k1 == k2

    def test_different_args_different_key(self):
        k1 = cache.make_key("graphql", "query { foo }", {"pr": 42})
        k2 = cache.make_key("graphql", "query { foo }", {"pr": 99})
        assert k1 != k2

    def test_different_prefix_different_key(self):
        k1 = cache.make_key("graphql", "query { foo }")
        k2 = cache.make_key("rest", "query { foo }")
        assert k1 != k2


class TestGetPut:
    def setup_method(self):
        cache.clear()

    def test_miss_returns_sentinel(self):
        assert cache.get("nonexistent") is cache._SENTINEL

    def test_put_then_get(self):
        cache.put("k1", {"data": "hello"})
        assert cache.get("k1") == {"data": "hello"}

    def test_expired_entry_returns_sentinel(self, mocker: MockerFixture):
        cache.put("k1", "value")
        mocker.patch.object(cache, "_DEFAULT_TTL", 0)
        # Force expiration by advancing time
        cache._cache["k1"] = (time.monotonic() - 1, "value")
        assert cache.get("k1") is cache._SENTINEL

    def test_expired_entry_is_removed(self):
        cache.put("k1", "value")
        cache._cache["k1"] = (time.monotonic() - 100, "value")
        cache.get("k1")
        assert "k1" not in cache._cache


class TestClear:
    def setup_method(self):
        cache.clear()

    def test_clear_removes_all(self):
        cache.put("k1", "v1")
        cache.put("k2", "v2")
        assert cache.size() == 2
        cache.clear()
        assert cache.size() == 0

    def test_clear_empty_is_noop(self):
        cache.clear()
        assert cache.size() == 0


class TestSize:
    def setup_method(self):
        cache.clear()

    def test_size_tracks_entries(self):
        assert cache.size() == 0
        cache.put("k1", "v1")
        assert cache.size() == 1
        cache.put("k2", "v2")
        assert cache.size() == 2
