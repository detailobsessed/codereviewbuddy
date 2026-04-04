"""Tests for the mock write server helper used by stdio rapid-write tests.

Importing and exercising the tool functions directly gives us coverage
of tests/helpers/mock_write_server.py (which normally runs as a subprocess
via PythonStdioTransport and is invisible to coverage).
"""

from __future__ import annotations

import json

from helpers import mock_write_server


class TestMockWriteServerTools:
    async def test_get_thread(self):
        result = await mock_write_server.get_thread("PRRT_test1")
        data = json.loads(result)
        assert "thread_id" in data

    def test_reply_to_comment(self):
        result = mock_write_server.reply_to_comment(42, "PRRT_test1", "Fixed")
        assert "PRRT_test1" in result

    def test_reply_to_comment_with_repo(self):
        result = mock_write_server.reply_to_comment(42, "PRRT_test1", "Fixed", repo="owner/repo")
        assert "PRRT_test1" in result

    async def test_check_for_updates(self):
        result = await mock_write_server.check_for_updates()
        data = json.loads(result)
        assert data["update_available"] is False
