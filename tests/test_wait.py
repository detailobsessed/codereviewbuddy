"""Tests for the wait_for_reviews tool."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

from codereviewbuddy.models import ReviewerStatus, ReviewSummary
from codereviewbuddy.tools.wait import wait_for_reviews


class TestWaitForReviews:
    async def test_returns_immediately_when_no_reviews_in_progress(self, mocker: MockerFixture):
        summary = ReviewSummary(threads=[], reviews_in_progress=False)

        from unittest.mock import AsyncMock

        mocker.patch(
            "codereviewbuddy.tools.wait.list_review_comments",
            new_callable=AsyncMock,
            return_value=summary,
        )

        result = await wait_for_reviews(42, repo="owner/repo", timeout=60, poll_interval=10)
        assert result.reviews_in_progress is False

    async def test_polls_until_reviews_complete(self, mocker: MockerFixture):
        pending_summary = ReviewSummary(
            threads=[],
            reviewer_statuses=[
                ReviewerStatus(
                    reviewer="devin",
                    status="pending",
                    detail="devin has not reviewed since latest push",
                ),
            ],
            reviews_in_progress=True,
        )
        completed_summary = ReviewSummary(
            threads=[],
            reviewer_statuses=[
                ReviewerStatus(
                    reviewer="devin",
                    status="completed",
                    detail="devin reviewed after latest push",
                ),
            ],
            reviews_in_progress=False,
        )

        from unittest.mock import AsyncMock

        mock_list = mocker.patch(
            "codereviewbuddy.tools.wait.list_review_comments",
            new_callable=AsyncMock,
            side_effect=[pending_summary, completed_summary],
        )
        mocker.patch("codereviewbuddy.tools.wait.asyncio.sleep", new_callable=AsyncMock)

        result = await wait_for_reviews(42, repo="owner/repo", timeout=120, poll_interval=30)
        assert result.reviews_in_progress is False
        assert mock_list.call_count == 2

    async def test_returns_on_timeout(self, mocker: MockerFixture):
        pending_summary = ReviewSummary(
            threads=[],
            reviewer_statuses=[
                ReviewerStatus(
                    reviewer="unblocked",
                    status="pending",
                    detail="unblocked has not reviewed since latest push",
                ),
            ],
            reviews_in_progress=True,
        )

        from unittest.mock import AsyncMock

        mock_list = mocker.patch(
            "codereviewbuddy.tools.wait.list_review_comments",
            new_callable=AsyncMock,
            return_value=pending_summary,
        )
        mocker.patch("codereviewbuddy.tools.wait.asyncio.sleep", new_callable=AsyncMock)

        result = await wait_for_reviews(42, repo="owner/repo", timeout=60, poll_interval=30)
        # Polls: t=0 (poll), sleep 30, t=30 (poll), sleep 30, t=60 (poll) → 3 polls
        # After 3rd poll, elapsed=60, next sleep would exceed timeout → return
        assert result.reviews_in_progress is True
        assert mock_list.call_count == 3

    async def test_no_reviewer_statuses_returns_immediately(self, mocker: MockerFixture):
        """No known reviewers on the PR → reviews_in_progress=False → return immediately."""
        summary = ReviewSummary(threads=[], reviewer_statuses=[], reviews_in_progress=False)

        from unittest.mock import AsyncMock

        mocker.patch(
            "codereviewbuddy.tools.wait.list_review_comments",
            new_callable=AsyncMock,
            return_value=summary,
        )

        result = await wait_for_reviews(42, repo="owner/repo")
        assert result.reviews_in_progress is False

    async def test_rejects_zero_poll_interval(self):
        import pytest

        with pytest.raises(ValueError, match="poll_interval must be positive"):
            await wait_for_reviews(42, repo="owner/repo", poll_interval=0)

    async def test_rejects_negative_poll_interval(self):
        import pytest

        with pytest.raises(ValueError, match="poll_interval must be positive"):
            await wait_for_reviews(42, repo="owner/repo", poll_interval=-5)

    async def test_rejects_negative_timeout(self):
        import pytest

        with pytest.raises(ValueError, match="timeout must be non-negative"):
            await wait_for_reviews(42, repo="owner/repo", timeout=-1)
