# AI PR Review: Agent Experience & MCP Server Design Input

## Context

This document captures my experience as an AI coding agent (Cascade/Windsurf) working through PR review cycles with two AI reviewers (**Unblocked** and **Devin**) using the `propcom` CLI tool and a manual `/prreview` workflow. The goal is to inform the design of an MCP server that streamlines this process.

---

## Current Workflow (Manual)

```
1. gt bottom                          # navigate to bottom of PR stack
2. propcom                            # fetch review comments for current PR
3. read comments, evaluate validity
4. fix legitimate issues in code
5. gt modify                          # amend commit
6. gt submit --stack --update-only    # push all branches
7. comment on PR asking for re-review # (Unblocked only — Devin auto-triggers)
8. gt up                              # move to next PR in stack
9. repeat from step 2
```

## What Works Well

- **`propcom`** is excellent for fetching and displaying comments in a structured, terminal-friendly format. The separation of inline comments vs review-level comments is clear.
- **Devin** auto-triggers re-review on new pushes and auto-resolves addressed comments. Zero friction.
- **Stacked PR support** via Graphite (`gt up/down/bottom`) integrates well with the review loop.

## Pain Points & Friction

### 1. Stale Comment Management (Biggest Pain Point)

**Problem**: After fixing issues and pushing, Unblocked's old comments remain "unresolved." I cannot programmatically resolve them via the GitHub API without knowing the exact comment node IDs. `propcom` shows comments but doesn't expose their GraphQL node IDs.

**What I need from the MCP server**:
- `resolve_comment(pr_number, comment_id)` — resolve/dismiss a specific review comment
- `resolve_all_stale(pr_number)` — bulk-resolve comments on lines that have changed since the review
- `list_comments(pr_number, status="unresolved")` — list with actionable IDs, not just display text

### 2. Re-Review Triggering Is Inconsistent

**Problem**: Devin auto-re-reviews on push. Unblocked does not — I have to manually leave a PR comment like `@unblocked please re-review`. This is easy to forget and adds a manual step per PR.

**What I need**:
- `request_rereview(pr_number, reviewer="unblocked")` — trigger re-review for a specific AI reviewer
- Or better: `request_rereview(pr_number)` that knows which reviewers need manual triggering vs which auto-trigger

### 3. Comment Validity Assessment Is Time-Consuming

**Problem**: Many AI review comments are false positives or apply to stacked PR context (e.g., "this file doesn't exist" when it's added in the next PR up the stack). I spend significant time evaluating each comment's legitimacy.

**What would help**:
- `comment.files_in_pr` — which files the comment references, so I can check if they exist in this PR vs another
- `comment.is_stale` — whether the commented lines have been modified since the review
- `comment.reviewer` — which tool generated it (to calibrate trust — Devin tends to be more precise, Unblocked sometimes flags already-fixed issues)
- Stack-awareness: knowing which PRs are in a stack and whether a referenced file exists in a sibling PR

### 4. No Way to Batch-Process a Stack

**Problem**: I have to manually `gt up` → `propcom` → fix → `gt modify` → repeat for each PR. With 5 PRs in a stack, this is 5 cycles of the same loop.

**What I need**:
- `review_stack()` — fetch all unresolved comments across all PRs in the current stack in one call
- `stack_status()` — summary showing which PRs have unresolved comments, which are clean, which need re-review

### 5. Resolving Comments Requires GitHub GraphQL Knowledge

**Problem**: To minimize/resolve a comment, I need the comment's GraphQL node ID (`minimizeComment` mutation). `propcom` and `gh` CLI don't expose these IDs in an agent-friendly way. I tried and failed to resolve comments programmatically during this session.

**What I need**:
- The MCP server should abstract this entirely. I should never need to know about GraphQL node IDs.
- Simple: `resolve_comment(pr=66, comment_index=1)` or `resolve_comment(pr=66, file="pyproject.toml.jinja", line=7)`

---

## Proposed MCP Server Tools

### Core Tools

| Tool | Description |
|------|-------------|
| `list_prs` | List open PRs for the current repo (or current stack) |
| `get_review_comments` | Get all review comments for a PR, with status, reviewer, staleness |
| `resolve_comment` | Resolve/dismiss a specific comment by ID |
| `resolve_stale_comments` | Bulk-resolve comments on lines changed since review |
| `request_rereview` | Trigger re-review (handles per-reviewer differences) |
| `reply_to_comment` | Post a reply to a specific review comment |
| `get_stack_status` | Summary of all PRs in stack: comment counts, review status |

### Nice-to-Have Tools

| Tool | Description |
|------|-------------|
| `get_comment_diff_context` | Show the current state of the code a comment references (has it changed?) |
| `batch_resolve` | Resolve multiple comments at once with a reason |
| `get_reviewer_config` | Which reviewers are configured, which auto-trigger, which need manual re-review |
| `approve_pr` | Mark a PR as approved (if the agent is a reviewer) |

### Data Model

Each comment should include:

```json
{
  "id": "actionable_id",
  "pr_number": 66,
  "reviewer": "unblocked",
  "file": "project/pyproject.toml.jinja",
  "line": 7,
  "body": "The escaping logic only handles...",
  "status": "unresolved",
  "is_stale": true,
  "severity": "bug",
  "created_at": "2026-02-06T09:43:42Z",
  "lines_changed_since_review": true
}
```

---

## Reviewer-Specific Observations

### Unblocked

- **Quality**: Good at catching real issues (backslash escaping, undefined variables). Occasionally flags already-fixed issues on re-review.
- **Re-review**: Must be manually triggered via `@unblocked please re-review` comment.
- **Resolution**: Does NOT auto-resolve comments. Agent must manually resolve or they accumulate.
- **Staleness**: Comments remain even after the underlying code is fixed. No awareness of new pushes.

### Devin

- **Quality**: Excellent at catching stack-level issues (missing files in stacked PRs) and restrictive filter patterns. Sometimes flags valid stack dependencies as bugs.
- **Re-review**: Auto-triggers on new pushes. Zero friction.
- **Resolution**: Auto-resolves addressed comments. Very low maintenance.
- **Stack awareness**: Limited — flags "file doesn't exist" for files added in sibling PRs.

---

## Authentication: Use `gh` CLI

**Recommendation**: Shell out to `gh` CLI for all GitHub API calls rather than managing tokens directly.

**Why this is the best approach**:
- Most developers already have `gh` installed and authenticated (`gh auth login`)
- No token management, no `.env` files, no secret storage
- `gh api` supports both REST and GraphQL natively: `gh api graphql -f query='...'`
- Handles token refresh, SSO, and enterprise GitHub automatically
- The agent already has `run_command` access — `gh` is the path of least resistance

**What we discovered works**:
```bash
# REST: List PR review comments with node IDs
gh api repos/{owner}/{repo}/pulls/{pr}/comments --jq '.[] | {id: .node_id, user: .user.login, path: .path, body: .body[:80]}'

# GraphQL: Minimize (resolve) a comment
gh api graphql -f query='mutation { minimizeComment(input: {subjectId: "PRRC_kwDO...", classifier: OUTDATED}) { minimizedComment { isMinimized } } }'

# REST: Post a PR comment (for re-review triggers)
gh pr comment {pr} --repo {owner}/{repo} --body "@unblocked please re-review"
```

**MCP server implementation**: The server should wrap these `gh` calls internally. The agent should never construct raw GraphQL — the MCP tools should accept simple parameters (`pr_number`, `comment_index`) and handle the API details.

**Fallback**: If `gh` is not installed or not authenticated, the server should detect this on startup and return a clear error with setup instructions rather than failing silently on the first API call.

---

## Architecture Suggestions

### Modular Reviewer Adapters

```
MCP Server
├── core/
│   ├── github.py          # GitHub API client (REST + GraphQL)
│   ├── comments.py         # Unified comment model
│   └── stack.py            # PR stack detection (Graphite, native)
├── reviewers/
│   ├── base.py             # Abstract reviewer adapter
│   ├── unblocked.py        # Unblocked-specific behavior
│   ├── devin.py            # Devin-specific behavior
│   └── coderabbit.py       # Future: CodeRabbit adapter
└── tools/
    ├── list_comments.py
    ├── resolve.py
    ├── rereview.py
    └── stack_status.py
```

Each reviewer adapter should define:
- `needs_manual_rereview: bool`
- `auto_resolves_comments: bool`
- `rereview_trigger(pr_number)` — how to request re-review
- `parse_comment(raw) -> ReviewComment` — normalize comment format

### Key Design Principle

**The MCP server should make the `/prreview` workflow a single tool call per PR**, not 7 manual steps. Ideally:

```
agent calls: review_and_fix_pr(pr_number=66)
server returns: {
  "unresolved_comments": [...],
  "stale_comments_resolved": 3,
  "rereview_requested": ["unblocked"],
  "next_pr_in_stack": 67
}
```

---

## Why MCP Over Skills (Windsurf Workflows)

We considered whether Windsurf Skills (`.windsurf/workflows/*.md` files that encode step-by-step instructions) could replace an MCP server. A Skill covers ~70% of the friction — encoding the review loop, teaching the agent `gh` CLI patterns, and storing reviewer-specific knowledge. But MCP is the right move for three reasons:

1. **Approval friction** — Every `run_command` an agent makes requires user approval. Resolving one comment today required 3 sequential approved shell commands (list comments → extract node ID → execute GraphQL mutation). An MCP tool collapses this to a single tool call with a single approval.

2. **Server-side logic** — Staleness detection (comparing comment line ranges against the current diff), batch operations across a stack, and stack-awareness are computation the agent shouldn't be doing inline. These aren't "knowledge the agent is missing" (Skill territory) — they're "logic that belongs in a server."

3. **Portability** — An MCP server works in Windsurf, Cursor, Cline, and any MCP-compatible agent. A Skill is Windsurf-only and must be copy-pasted per workspace.

**Recommendation**: Ship a Skill alongside the MCP server as agent-facing documentation — teaching agents the mental model of PR review, when to use which tool, and reviewer quirks. The MCP server handles execution; the Skill handles context.

---

## Session Statistics (This Session)

- **5 PRs** in stack (#66-68, #71-72)
- **~15 review comments** processed across all PRs
- **3 legitimate bugs found** by reviewers (backslash escaping, undefined `project_slug`, restrictive `files:` filter)
- **~5 false positives** (stale comments, stack dependencies, Python 3.10 concern on 3.14+ project)
- **2 full prreview cycles** through the entire stack
- **Estimated time on review management vs actual fixes**: ~60% review management, ~40% actual coding
