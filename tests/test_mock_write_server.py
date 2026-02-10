"""Tests for the mock write server helper used by stdio rapid-write tests.

Importing and exercising the tool functions directly gives us coverage
of tests/helpers/mock_write_server.py (which normally runs as a subprocess
via PythonStdioTransport and is invisible to coverage).
"""

from __future__ import annotations

import json

from helpers import mock_write_server


class TestMockWriteServerTools:
    async def test_list_review_comments(self):
        result = await mock_write_server.list_review_comments(42)  # type: ignore[operator]
        data = json.loads(result)
        assert "threads" in data

    async def test_list_review_comments_with_params(self):
        result = await mock_write_server.list_review_comments(42, repo="owner/repo", status="unresolved")  # type: ignore[operator]
        data = json.loads(result)
        assert "threads" in data

    async def test_resolve_stale_comments(self):
        result = await mock_write_server.resolve_stale_comments(42)  # type: ignore[operator]
        data = json.loads(result)
        assert data["resolved_count"] == 1

    async def test_resolve_stale_comments_with_repo(self):
        result = await mock_write_server.resolve_stale_comments(42, repo="owner/repo")  # type: ignore[operator]
        data = json.loads(result)
        assert "resolved_thread_ids" in data

    def test_resolve_comment(self):
        result = mock_write_server.resolve_comment(42, "PRRT_test1")  # type: ignore[operator]
        assert "PRRT_test1" in result

    def test_reply_to_comment(self):
        result = mock_write_server.reply_to_comment(42, "PRRT_test1", "Fixed")  # type: ignore[operator]
        assert "PRRT_test1" in result

    def test_reply_to_comment_with_repo(self):
        result = mock_write_server.reply_to_comment(42, "PRRT_test1", "Fixed", repo="owner/repo")  # type: ignore[operator]
        assert "PRRT_test1" in result

    async def test_request_rereview(self):
        result = await mock_write_server.request_rereview(42)  # type: ignore[operator]
        data = json.loads(result)
        assert "triggered" in data

    async def test_request_rereview_with_params(self):
        result = await mock_write_server.request_rereview(42, reviewer="unblocked", repo="owner/repo")  # type: ignore[operator]
        data = json.loads(result)
        assert "auto_triggers" in data

    async def test_check_for_updates(self):
        result = await mock_write_server.check_for_updates()  # type: ignore[operator]
        data = json.loads(result)
        assert data["update_available"] is False
