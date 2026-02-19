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


class ReviewerStatus(BaseModel):
    """Status of a specific AI reviewer on a PR."""

    reviewer: str = Field(description="Reviewer name (e.g. 'devin', 'unblocked')")
    status: str = Field(description="'completed' (reviewed latest push) or 'pending' (push after last review)")
    detail: str = Field(description="Human-readable explanation of the status")
    last_review_at: datetime | None = Field(default=None, description="Timestamp of the reviewer's most recent comment/review")
    last_push_at: datetime | None = Field(default=None, description="Timestamp of the latest commit on the PR")


class StackPR(BaseModel):
    """A PR in a stack."""

    pr_number: int = Field(description="PR number")
    branch: str = Field(description="Branch name")
    title: str = Field(description="PR title")
    url: str = Field(description="PR URL")


class ReviewSummary(BaseModel):
    """Review threads plus reviewer status for a PR."""

    threads: list[ReviewThread] = Field(default_factory=list, description="All review threads on the PR")
    reviewer_statuses: list[ReviewerStatus] = Field(default_factory=list, description="Per-reviewer status based on timestamp heuristic")
    stack: list[StackPR] = Field(default_factory=list, description="Other PRs in the same stack, discovered via branch chain")
    error: str | None = Field(default=None, description="Error message if the request failed")


class PRReviewStatusSummary(BaseModel):
    """Lightweight review status for a single PR — no full comment bodies."""

    pr_number: int = Field(description="PR number")
    title: str = Field(description="PR title")
    url: str = Field(description="PR URL")
    unresolved: int = Field(default=0, description="Number of unresolved threads")
    resolved: int = Field(default=0, description="Number of resolved threads")
    bugs: int = Field(default=0, description="Number of 🔴 bug-level threads")
    flagged: int = Field(default=0, description="Number of 🚩 flagged threads")
    warnings: int = Field(default=0, description="Number of 🟡 warning threads")
    info_count: int = Field(default=0, description="Number of 📝 info threads")
    stale: int = Field(default=0, description="Number of unresolved stale threads (file changed after comment)")


class StackReviewStatusResult(BaseModel):
    """Lightweight stack-wide review status overview."""

    prs: list[PRReviewStatusSummary] = Field(default_factory=list, description="Per-PR review status, bottom to top")
    total_unresolved: int = Field(default=0, description="Total unresolved threads across the stack")
    error: str | None = Field(default=None, description="Error message if the request failed")


class ResolveStaleResult(BaseModel):
    """Result of bulk-resolving stale review threads."""

    resolved_count: int = Field(description="Number of threads resolved")
    resolved_thread_ids: list[str] = Field(default_factory=list, description="Thread IDs that were resolved")
    skipped_count: int = Field(default=0, description="Threads skipped because the reviewer auto-resolves (e.g. Devin, CodeRabbit)")
    blocked_count: int = Field(default=0, description="Threads blocked by resolve_levels config (severity too high)")
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


class CreateIssueResult(BaseModel):
    """Result of creating a GitHub issue from a review comment."""

    issue_number: int = Field(default=0, description="Created issue number")
    issue_url: str = Field(default="", description="URL of the created issue")
    title: str = Field(default="", description="Issue title")
    error: str | None = Field(default=None, description="Error message if the request failed")


class TriageItem(BaseModel):
    """A single review thread that needs agent action."""

    thread_id: str = Field(description="GraphQL node ID (PRRT_...) for replying/resolving")
    pr_number: int = Field(description="PR number this thread belongs to")
    file: str | None = Field(default=None, description="File path the comment is on")
    line: int | None = Field(default=None, description="Line number in the file")
    reviewer: str = Field(description="Which reviewer posted this (e.g. devin, unblocked)")
    severity: str = Field(description="Classified severity: bug, flagged, warning, info")
    title: str = Field(default="", description="Short title extracted from the comment (first bold text)")
    is_stale: bool = Field(default=False, description="Whether the commented file changed since the review")
    action: str = Field(
        description="Suggested action: 'fix' (bug/flagged), 'reply' (info/warning), or 'create_issue' (followup without issue ref)"
    )
    snippet: str = Field(default="", description="First 200 chars of the comment body for context")


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

    items: list[TriageItem] = Field(default_factory=list, description="Threads needing action, ordered by severity (bugs first)")
    needs_fix: int = Field(default=0, description="Count of threads that need a code fix (bug/flagged)")
    needs_reply: int = Field(default=0, description="Count of threads that need a reply (info/warning)")
    needs_issue: int = Field(default=0, description="Count of 'noted for followup' replies missing a GH issue reference")
    total: int = Field(default=0, description="Total actionable threads")
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
    error: str | None = Field(default=None, description="Error message if diagnosis itself failed")


class ConfigInfo(BaseModel):
    """Active codereviewbuddy configuration with metadata."""

    config: dict = Field(description="Full configuration as a dictionary")
    source: str = Field(default="env", description="Configuration source: 'env' (CRB_* environment variables)")
