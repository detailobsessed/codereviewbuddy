"""CI diagnosis — find and surface GitHub Actions failures."""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from codereviewbuddy import gh
from codereviewbuddy.models import CIDiagnosisResult, CIJobFailure

if TYPE_CHECKING:
    from typing import Any

logger = logging.getLogger(__name__)

# Lines matching these patterns are noise — strip them from error output.
_NOISE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^##\[(end)?group\]"),
    re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z\s*$"),
    re.compile(r"^(Post job cleanup|Cleaning up orphan processes)"),
    re.compile(r"^\s*shell:\s+/"),
    re.compile(r"^\s*env:\s*$"),
    re.compile(r"^\s+\w+:.*$"),  # indented env var lines
]

# Lines matching these patterns are interesting — keep them.
_ERROR_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"##\[error\]", re.IGNORECASE),
    re.compile(r"\berrors?\b", re.IGNORECASE),
    re.compile(r"\bFailed\b", re.IGNORECASE),
    re.compile(r"\bfailure\b", re.IGNORECASE),
    re.compile(r"\bfatal\b", re.IGNORECASE),
    re.compile(r"\bERROR:", re.IGNORECASE),
    re.compile(r"\bTraceback\b"),
    re.compile(r"\bAssertionError\b"),
    re.compile(r"\bexception\b", re.IGNORECASE),
    re.compile(r"exit code [1-9]"),
    re.compile(r"Process completed with exit code [1-9]"),
]

_MAX_ERROR_LINES = 50
_MAX_LOG_LINES = 2000


def _is_noise(line: str) -> bool:
    """Return True if the line is CI log noise."""
    return any(p.search(line) for p in _NOISE_PATTERNS)


def _is_error_line(line: str) -> bool:
    """Return True if the line looks like an error."""
    return any(p.search(line) for p in _ERROR_PATTERNS)


def _strip_timestamp(line: str) -> str:
    """Remove the leading ISO timestamp from a log line if present."""
    return re.sub(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z\s*", "", line)


def _strip_job_prefix(line: str) -> str:
    """Remove the leading job-name prefix (e.g. 'prek    UNKNOWN STEP    ')."""
    return re.sub(r"^\S+\s+\S+\s+\S+\s+", "", line)


def _clean_log_line(line: str) -> str:
    """Strip noise prefixes from a raw gh log line."""
    cleaned = _strip_job_prefix(line)
    cleaned = _strip_timestamp(cleaned)
    return cleaned.rstrip()


def _extract_error_lines(raw_log: str) -> list[str]:
    """Extract actionable error lines from raw gh log output.

    Keeps lines that match error patterns, strips noise, and adds a few
    lines of context around each error for readability.
    """
    lines = raw_log.splitlines()
    if len(lines) > _MAX_LOG_LINES:
        lines = lines[-_MAX_LOG_LINES:]

    cleaned = [_clean_log_line(line) for line in lines]

    # Find indices of error lines
    error_indices: set[int] = set()
    for i, line in enumerate(cleaned):
        if _is_error_line(line) and not _is_noise(line):
            # Add the error line plus 2 lines of context before and after
            error_indices.update(range(max(0, i - 2), min(len(cleaned), i + 3)))

    result: list[str] = []
    prev_idx = -2
    for idx in sorted(error_indices):
        line = cleaned[idx]
        if not line or _is_noise(line):
            continue
        # Insert a separator when there's a gap
        if idx > prev_idx + 1 and result:
            result.append("---")
        result.append(line)
        prev_idx = idx

    return result[:_MAX_ERROR_LINES]


def _find_latest_failed_run(
    *,
    pr_number: int | None = None,
    repo: str | None = None,
    cwd: str | None = None,
) -> dict[str, Any] | None:
    """Find the latest failed workflow run for a branch or PR."""
    args = [
        "run",
        "list",
        "--json",
        "databaseId,status,conclusion,name,headBranch,url",
        "--limit",
        "20",
    ]
    if repo:
        args.extend(["--repo", repo])

    # If we have a PR number, get the branch name first
    if pr_number is not None:
        pr_args = ["pr", "view", str(pr_number), "--json", "headRefName", "-q", ".headRefName"]
        if repo:
            pr_args.extend(["--repo", repo])
        branch = gh.run_gh(*pr_args, cwd=cwd).strip()
        args.extend(["--branch", branch])

    raw = gh.run_gh(*args, cwd=cwd)
    runs: list[dict[str, Any]] = json.loads(raw)

    # Find the first completed failure
    for run in runs:
        if run.get("status") == "completed" and run.get("conclusion") == "failure":
            return run
    return None


def _get_run_details(
    run_id: int,
    *,
    repo: str | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Get detailed info about a workflow run including jobs."""
    args = ["run", "view", str(run_id), "--json", "jobs,name,headBranch,conclusion,url"]
    if repo:
        args.extend(["--repo", repo])
    raw = gh.run_gh(*args, cwd=cwd)
    return json.loads(raw)


def _get_failed_logs(
    run_id: int,
    *,
    repo: str | None = None,
    cwd: str | None = None,
) -> str:
    """Get the failed job logs for a workflow run."""
    args = ["run", "view", str(run_id), "--log-failed"]
    if repo:
        args.extend(["--repo", repo])
    return gh.run_gh(*args, cwd=cwd)


def diagnose_ci(
    *,
    pr_number: int | None = None,
    repo: str | None = None,
    run_id: int | None = None,
    cwd: str | None = None,
) -> CIDiagnosisResult:
    """Diagnose CI failures for a PR or specific workflow run.

    This collapses the typical 3-5 sequential ``gh`` commands into one call:
    1. Find the latest failed run (or use a specific run_id)
    2. Identify failed jobs and steps
    3. Extract actionable error lines from logs
    """
    # Step 1: Find the run
    if run_id is not None:
        run_info: dict[str, Any] | None = {"databaseId": run_id}
    else:
        run_info = _find_latest_failed_run(pr_number=pr_number, repo=repo, cwd=cwd)

    if run_info is None:
        return CIDiagnosisResult(error="No failed workflow runs found.")

    actual_run_id = run_info["databaseId"]

    # Step 2: Get run details with jobs
    try:
        details = _get_run_details(actual_run_id, repo=repo, cwd=cwd)
    except gh.GhError as exc:
        return CIDiagnosisResult(
            run_id=actual_run_id,
            error=f"Failed to fetch run details: {exc}",
        )

    workflow = details.get("name", "")
    branch = details.get("headBranch", run_info.get("headBranch", ""))
    conclusion = details.get("conclusion", run_info.get("conclusion", ""))
    url = details.get("url", run_info.get("url", ""))

    # Step 3: Identify failed jobs
    jobs = details.get("jobs", [])
    failed_jobs = [j for j in jobs if j.get("conclusion") == "failure"]

    if not failed_jobs:
        return CIDiagnosisResult(
            run_id=actual_run_id,
            workflow=workflow,
            branch=branch,
            conclusion=conclusion,
            url=url,
            error="Run marked as failure but no individual jobs failed.",
        )

    # Step 4: Get failed logs and extract errors
    try:
        raw_logs = _get_failed_logs(actual_run_id, repo=repo, cwd=cwd)
    except gh.GhError as exc:
        logger.warning("Failed to fetch logs for run %d: %s", actual_run_id, exc)
        raw_logs = ""

    error_lines = _extract_error_lines(raw_logs) if raw_logs else []

    failures = _build_failures(failed_jobs, error_lines)

    # Build next_steps based on failures
    next_steps: list[str] = []
    if failures:
        failed_steps = [f.failed_step for f in failures if f.failed_step]
        if failed_steps:
            next_steps.append(f"Fix the error in failed step(s): {', '.join(failed_steps)}. Then push and re-run CI.")
        else:
            next_steps.append("Fix the errors shown above, then push and re-run CI.")
        next_steps.append(f"View the full run at: {url}" if url else "Re-run the workflow from GitHub Actions.")

    return CIDiagnosisResult(
        run_id=actual_run_id,
        workflow=workflow,
        branch=branch,
        conclusion=conclusion,
        url=url,
        failures=failures,
        next_steps=next_steps,
    )


def _build_failures(
    failed_jobs: list[dict[str, Any]],
    error_lines: list[str],
) -> list[CIJobFailure]:
    """Convert raw job dicts into CIJobFailure models with matched error lines."""
    failures: list[CIJobFailure] = []
    for job in failed_jobs:
        job_name = job.get("name", "unknown")
        failed_step = next(
            (s.get("name", "") for s in job.get("steps", []) if s.get("conclusion") == "failure"),
            "",
        )
        # For multi-job failures, try to match errors to the specific job
        if len(failed_jobs) > 1:
            job_errors = [line for line in error_lines if job_name.lower() in line.lower()] or error_lines
        else:
            job_errors = error_lines

        failures.append(
            CIJobFailure(
                job_name=job_name,
                conclusion=job.get("conclusion", "failure"),
                failed_step=failed_step,
                error_lines=job_errors,
            )
        )
    return failures
