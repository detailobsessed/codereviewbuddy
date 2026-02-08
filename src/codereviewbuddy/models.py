"""Pydantic models for codereviewbuddy."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - Pydantic needs this at runtime
from enum import StrEnum

from pydantic import BaseModel, Field


class CommentStatus(StrEnum):
    """Status of a review thread."""

    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"


class ReviewComment(BaseModel):
    """A single comment within a review thread."""

    author: str = Field(description="GitHub username of the comment author")
    body: str = Field(description="Comment body text")
    created_at: datetime | None = Field(default=None, description="When the comment was posted")


class ReviewThread(BaseModel):
    """A review thread on a pull request."""

    thread_id: str = Field(description="GraphQL node ID (PRRT_...) for resolving")
    pr_number: int = Field(description="PR number this thread belongs to")
    status: CommentStatus = Field(description="Whether the thread is resolved or not")
    file: str | None = Field(default=None, description="File path the comment is on")
    line: int | None = Field(default=None, description="Line number in the file")
    reviewer: str = Field(description="Which reviewer posted this (e.g. unblocked, devin, coderabbit)")
    comments: list[ReviewComment] = Field(default_factory=list, description="Comments in this thread")
    is_stale: bool = Field(default=False, description="Whether the commented lines changed since review")
    is_pr_review: bool = Field(default=False, description="True for PR-level reviews (PRR_ IDs) — not resolvable via resolveReviewThread")


class StackPR(BaseModel):
    """A PR in a stack with review summary."""

    pr_number: int = Field(description="PR number")
    branch: str = Field(description="Branch name")
    title: str = Field(description="PR title")
    url: str = Field(description="PR URL")
    unresolved_count: int = Field(default=0, description="Number of unresolved review threads")
    resolved_count: int = Field(default=0, description="Number of resolved review threads")


class StackStatus(BaseModel):
    """Summary of all PRs in a stack."""

    prs: list[StackPR] = Field(default_factory=list, description="PRs in the stack, bottom to top")
    total_unresolved: int = Field(default=0, description="Total unresolved threads across all PRs")
    stack_tool: str = Field(default="none", description="Stack tool detected (graphite, git-town, none)")


class ResolveStaleResult(BaseModel):
    """Result of bulk-resolving stale review threads."""

    resolved_count: int = Field(description="Number of threads resolved")
    resolved_thread_ids: list[str] = Field(default_factory=list, description="Thread IDs that were resolved")
    skipped_count: int = Field(default=0, description="Threads skipped because the reviewer auto-resolves (e.g. Devin, CodeRabbit)")


class RereviewResult(BaseModel):
    """Result of triggering re-reviews on a PR."""

    triggered: list[str] = Field(default_factory=list, description="Reviewers that were manually triggered")
    auto_triggers: list[str] = Field(default_factory=list, description="Reviewers that auto-trigger on push (no action needed)")
