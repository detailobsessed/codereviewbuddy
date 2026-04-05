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
    url: str = Field(default="", description="Direct URL to this comment on GitHub")


class ReviewThread(BaseModel):
    """A review thread on a pull request."""

    thread_id: str = Field(description="GraphQL node ID (PRRT_...) for resolving")
    pr_number: int = Field(description="PR number this thread belongs to")
    status: CommentStatus = Field(description="Whether the thread is resolved or not")
    file: str | None = Field(default=None, description="File path the comment is on")
    line: int | None = Field(default=None, description="Line number in the file")
    reviewer: str = Field(description="GitHub login of the user or bot that posted the first comment")
    comments: list[ReviewComment] = Field(default_factory=list, description="Comments in this thread")
    is_pr_review: bool = Field(default=False, description="True for PR-level reviews (PRR_ IDs) — not resolvable via resolveReviewThread")


class StackPR(BaseModel):
    """A PR in a stack."""

    pr_number: int = Field(description="PR number")
    branch: str = Field(description="Branch name")
    title: str = Field(description="PR title")
    url: str = Field(description="PR URL")


class ReviewerState(BaseModel):
    """Review state for a single reviewer on a PR."""

    reviewer: str = Field(description="GitHub login of the reviewer")
    state: str = Field(description="Review state: 'approved', 'changes_requested', 'commented', 'dismissed', 'waiting'")


class PRReviewStatusSummary(BaseModel):
    """Lightweight review status for a single PR — no full comment bodies."""

    pr_number: int = Field(description="PR number")
    title: str = Field(description="PR title")
    url: str = Field(description="PR URL")
    unresolved: int = Field(default=0, description="Number of unresolved threads")
    resolved: int = Field(default=0, description="Number of resolved threads")
    review_state: str = Field(
        default="none",
        description="Overall review state: 'approved', 'changes_requested', 'waiting', 'commented', 'none'",
    )
    reviewers: list[ReviewerState] = Field(default_factory=list, description="Per-reviewer state from GitHub's reviewer API")


class StackReviewStatusResult(BaseModel):
    """Lightweight stack-wide review status overview."""

    prs: list[PRReviewStatusSummary] = Field(default_factory=list, description="Per-PR review status, bottom to top")
    total_unresolved: int = Field(default=0, description="Total unresolved threads across the stack")
    next_steps: list[str] = Field(default_factory=list, description="Suggested next actions based on the stack state")
    error: str | None = Field(default=None, description="Error message if the request failed")


class PRDescriptionInfo(BaseModel):
    """A single PR's description with quality analysis."""

    pr_number: int = Field(description="PR number")
    title: str = Field(description="PR title")
    body: str = Field(default="", description="PR body/description")
    url: str = Field(default="", description="PR URL")
    has_body: bool = Field(default=False, description="Whether the PR has a non-empty description")
    is_boilerplate: bool = Field(default=False, description="Whether the description is template boilerplate only")
    linked_issues: list[str] = Field(default_factory=list, description="Issue references found (e.g. #123)")
    missing_elements: list[str] = Field(
        default_factory=list, description="Missing quality elements (e.g. 'no linked issues', 'empty body')"
    )
    error: str | None = Field(default=None, description="Error message if fetching this PR failed")


class PRDescriptionReviewResult(BaseModel):
    """Result of reviewing PR descriptions across a stack."""

    descriptions: list[PRDescriptionInfo] = Field(default_factory=list, description="PR descriptions with analysis")
    error: str | None = Field(default=None, description="Error message if the request failed")


class TriageItem(BaseModel):
    """A single review thread that needs agent action."""

    thread_id: str = Field(description="GraphQL node ID (PRRT_...) for replying/resolving")
    pr_number: int = Field(description="PR number this thread belongs to")
    file: str | None = Field(default=None, description="File path the comment is on")
    line: int | None = Field(default=None, description="Line number in the file")
    reviewer: str = Field(description="GitHub login of the user or bot that posted this thread")
    title: str = Field(default="", description="Short title extracted from the comment (first bold text)")
    comment_url: str = Field(default="", description="Direct URL to the comment on GitHub for user navigation")
    action: str = Field(
        default="fix",
        description="Suggested action: 'fix' (code change needed), 'acknowledge' (informational), 'ambiguous' (unclear)",
    )


class ActivityEvent(BaseModel):
    """A single event in a stack activity timeline."""

    time: datetime = Field(description="When the event occurred")
    pr_number: int = Field(description="PR number this event belongs to")
    event_type: str = Field(description="Event type: push, review, comment, labeled, unlabeled, merged, closed")
    actor: str = Field(default="", description="GitHub username of the actor")
    detail: str = Field(default="", description="Human-readable detail (e.g. 'found 4 issues', 'approved')")


class StackActivityResult(BaseModel):
    """Chronological activity feed across all PRs in a stack."""

    events: list[ActivityEvent] = Field(default_factory=list, description="Events ordered chronologically (newest last)")
    last_activity: datetime | None = Field(default=None, description="Timestamp of the most recent event")
    minutes_since_last_activity: int | None = Field(default=None, description="Minutes since the last event")
    settled: bool = Field(default=False, description="True if no activity for 10+ minutes after a push+review cycle")
    error: str | None = Field(default=None, description="Error message if the request failed")


class TriageResult(BaseModel):
    """Triage result for one or more PRs — only threads needing agent action."""

    items: list[TriageItem] = Field(default_factory=list, description="Unresolved threads needing action")
    total: int = Field(default=0, description="Total actionable threads")
    next_steps: list[str] = Field(default_factory=list, description="Suggested next actions based on triage results")
    message: str = Field(default="", description="Human-readable summary when results are empty or noteworthy")
    error: str | None = Field(default=None, description="Error message if the request failed")


class CIJobFailure(BaseModel):
    """A single failed job in a CI workflow run."""

    job_name: str = Field(description="Name of the failed job")
    conclusion: str = Field(description="Job conclusion (e.g. 'failure', 'cancelled')")
    failed_step: str = Field(default="", description="Name of the first failed step within the job")
    error_lines: list[str] = Field(default_factory=list, description="Extracted error/failure lines from the job logs")


class CIDiagnosisResult(BaseModel):
    """Structured CI failure diagnosis from a GitHub Actions workflow run."""

    run_id: int = Field(default=0, description="Workflow run ID")
    workflow: str = Field(default="", description="Workflow name (e.g. 'ci', 'release')")
    branch: str = Field(default="", description="Branch the run was triggered on")
    conclusion: str = Field(default="", description="Overall run conclusion (e.g. 'failure')")
    url: str = Field(default="", description="URL to the workflow run")
    failures: list[CIJobFailure] = Field(default_factory=list, description="Failed jobs with extracted error details")
    next_steps: list[str] = Field(default_factory=list, description="Suggested next actions for fixing the CI failure")
    error: str | None = Field(default=None, description="Error message if diagnosis itself failed")


class CICheckStatus(BaseModel):
    """Status of a single CI check (e.g. a job or external status check)."""

    name: str = Field(description="Check name (e.g. 'quality', 'tests (ubuntu, 3.14, highest)')")
    status: str = Field(description="Normalized status: 'pass', 'fail', or 'pending'")
    workflow: str = Field(default="", description="Workflow name (e.g. 'ci', 'release') — empty for external checks")


class CIStatusResult(BaseModel):
    """Lightweight CI status for a PR — pass/fail/pending with per-check breakdown."""

    overall: str = Field(
        default="pass",
        description="Worst status across all checks: 'pass', 'fail', 'pending', 'none' (no checks), or 'error'",
    )
    checks: list[CICheckStatus] = Field(default_factory=list, description="Per-check status breakdown")
    total: int = Field(default=0, description="Total number of checks")
    passed: int = Field(default=0, description="Number of passing checks")
    failed: int = Field(default=0, description="Number of failed checks")
    pending: int = Field(default=0, description="Number of pending checks")
    next_steps: list[str] = Field(default_factory=list, description="Suggested next actions based on CI status")
    error: str | None = Field(default=None, description="Error message if the status check itself failed")


class ConfigInfo(BaseModel):
    """Active codereviewbuddy configuration with metadata."""

    config: dict = Field(description="Full configuration as a dictionary")
    source: str = Field(default="env", description="Configuration source: 'env' (CRB_* environment variables)")
    explanation: str = Field(default="", description="Human-readable summary of the active configuration highlights")
