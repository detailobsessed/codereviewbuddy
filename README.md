<!-- mcp-name: io.github.detailobsessed/codereviewbuddy -->
# codereviewbuddy

[![ci](https://github.com/detailobsessed/codereviewbuddy/workflows/ci/badge.svg)](https://github.com/detailobsessed/codereviewbuddy/actions?query=workflow%3Aci)
[![release](https://img.shields.io/github/v/release/detailobsessed/codereviewbuddy)](https://github.com/detailobsessed/codereviewbuddy/releases)
[![documentation](https://img.shields.io/badge/docs-mkdocs-blue.svg)](https://detailobsessed.github.io/codereviewbuddy/)
[![Python 3.14+](https://img.shields.io/badge/python-3.14+-blue.svg)](https://www.python.org/downloads/)
[![FastMCP v3](https://img.shields.io/badge/FastMCP-v3-blue.svg)](https://github.com/jlowin/fastmcp)

An MCP server that helps your AI coding agent interact with AI code reviewers — smoothly.

Manages review comments from **Unblocked**, **Devin**, **CodeRabbit**, and **Greptile** on GitHub PRs with staleness detection, batch resolution, re-review triggering, and issue tracking.

## Features

### Review comment management

- **List review comments** — inline threads, PR-level reviews, and bot comments (codecov, netlify, vercel, etc.) with reviewer identification and staleness detection
- **Stacked PR support** — `list_stack_review_comments` fetches comments across an entire PR stack in one call
- **Resolve comments** — individually or bulk-resolve stale ones (files changed since the review)
- **Smart skip logic** — `resolve_stale_comments` skips reviewers that auto-resolve their own comments (Devin, CodeRabbit), only batch-resolving threads from reviewers that don't (Unblocked)
- **Reply to anything** — inline review threads (`PRRT_`), PR-level reviews (`PRR_`), and bot issue comments (`IC_`) all routed to the correct GitHub API
- **Request re-reviews** — per-reviewer logic handles differences automatically (manual trigger for Unblocked, auto for Devin/CodeRabbit)

### Triage & CI diagnosis

- **Triage review comments** — `triage_review_comments` filters to only actionable threads, pre-classifies severity, suggests fix/reply/create_issue actions, and includes direct GitHub URLs for each comment
- **Diagnose CI failures** — `diagnose_ci` collapses 3-5 sequential `gh` commands into one call: finds the failed run, identifies failed jobs/steps, and extracts actionable error lines
- **Stack activity feed** — `stack_activity` shows a chronological timeline of pushes, reviews, labels, merges across all PRs in a stack with a `settled` flag for deciding when to proceed
- **Scan merged PRs** — `list_recent_unresolved` catches late review comments on already-merged PRs

### Issue tracking

- **Create issues from review comments** — turn useful AI suggestions into GitHub issues with labels, PR backlinks, file/line location, and quoted comment text

### Agent experience

- **Recovery-guided errors** — every tool handler classifies errors (auth, rate limit, not found, workspace, GraphQL, config) and returns actionable recovery hints so agents self-correct instead of retrying blindly
- **Next-action hints** — tool responses include `next_steps` suggestions guiding agents to the right follow-up tool call
- **Empty result messages** — when results are empty, responses explain why and suggest what to try next
- **GUI URLs** — triage items include `comment_url` so agents can link users directly to the comment on GitHub
- **Tool classification tags** — tools are tagged `query`, `command`, or `discovery` for MCP clients that support filtering

### Server features (FastMCP v3)

- **Typed output schemas** — all tools return Pydantic models with JSON Schema, giving MCP clients structured data instead of raw strings
- **Progress reporting** — long-running operations report progress via FastMCP context (visible in MCP clients that support it)
- **Production middleware** — ErrorHandling (transforms exceptions to clean MCP errors with tracebacks), Timing (logs execution duration for every tool call), and Logging (request/response payloads for debugging)
- **Update checker** — `check_for_updates` compares the running version against PyPI and suggests upgrade commands
- **Zero config auth** — uses `gh` CLI, no PAT tokens or `.env` files

### CLI testing (free with FastMCP v3)

FastMCP v3 gives you terminal testing of the server with no extra code:

```bash
# List all tools with their signatures
fastmcp list codereviewbuddy.server:mcp

# Call a tool directly from the terminal
fastmcp call codereviewbuddy.server:mcp list_review_comments pr_number=42

# Inspect server metadata
fastmcp inspect codereviewbuddy.server:mcp

# Run with MCP Inspector for interactive debugging
fastmcp dev codereviewbuddy.server:mcp
```

## Prerequisites

- [GitHub CLI (`gh`)](https://cli.github.com/) installed and authenticated (`gh auth login`)
- Python 3.14+

## Installation

This project uses [`uv`](https://docs.astral.sh/uv/). No install needed — run directly:

```bash
uvx codereviewbuddy
```

Or install permanently:

```bash
uv tool install codereviewbuddy
```

## MCP Client Configuration

### Quick setup (recommended)

One command configures your MCP client — no manual JSON editing:

```bash
uvx codereviewbuddy install claude-desktop
uvx codereviewbuddy install claude-code
uvx codereviewbuddy install cursor
uvx codereviewbuddy install windsurf
uvx codereviewbuddy install windsurf-next
```

With optional environment variables:

```bash
uvx codereviewbuddy install windsurf \
  --env CRB_SELF_IMPROVEMENT__ENABLED=true \
  --env CRB_SELF_IMPROVEMENT__REPO=your-org/codereviewbuddy
```

For any other client, generate the JSON config:

```bash
uvx codereviewbuddy install mcp-json          # print to stdout
uvx codereviewbuddy install mcp-json --copy   # copy to clipboard
```

Restart your MCP client after installing. See `uvx codereviewbuddy install --help` for all options.

### Manual configuration

If you prefer manual setup, add the following to your MCP client's config JSON:

```jsonc
{
  "mcpServers": {
    "codereviewbuddy": {
      "command": "uvx",
      "args": ["codereviewbuddy@latest"],
      "env": {
        // All CRB_* env vars are optional — zero-config works out of the box.
        // See Configuration section below for the full list.

        // Per-reviewer overrides (JSON string — omit to use adapter defaults)
        // "CRB_REVIEWERS": "{\"devin\": {\"enabled\": false}}",

        // Self-improvement: agents file issues when they hit server gaps
        // "CRB_SELF_IMPROVEMENT__ENABLED": "true",
        // "CRB_SELF_IMPROVEMENT__REPO": "your-org/codereviewbuddy",

        // Diagnostics (off by default)
        // "CRB_DIAGNOSTICS__IO_TAP": "true",
        // "CRB_DIAGNOSTICS__TOOL_CALL_HEARTBEAT": "true"
      }
    }
  }
}
```

The server auto-detects your project from MCP roots (sent per-window by your client). This works correctly with multiple windows open on different projects — no env vars needed.

> **Why `@latest`?** Without it, `uvx` caches the first resolved version and never upgrades automatically.

### From source (development)

For local development, use `uv run --directory` to run the server from your checkout instead of the PyPI-published version. Changes to the source take effect immediately — just restart the MCP server in your client.

```jsonc
{
  "mcpServers": {
    "codereviewbuddy": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/codereviewbuddy", "codereviewbuddy"],
      "env": {
        // Same CRB_* env vars as above, plus dev-specific settings:
        "CRB_SELF_IMPROVEMENT__ENABLED": "true",
        "CRB_SELF_IMPROVEMENT__REPO": "detailobsessed/codereviewbuddy",
        "CRB_DIAGNOSTICS__IO_TAP": "true",
        "CRB_DIAGNOSTICS__TOOL_CALL_HEARTBEAT": "true",
        "CRB_DIAGNOSTICS__HEARTBEAT_INTERVAL_MS": "5000",
        "CRB_DIAGNOSTICS__INCLUDE_ARGS_FINGERPRINT": "true"
      }
    }
  }
}
```

### Troubleshooting

If your MCP client reports `No module named 'fastmcp.server.tasks.routing'`, the runtime has an incompatible FastMCP. Fixes:

1. Prefer `uvx codereviewbuddy@latest` in MCP client config.
2. For local source checkouts, launch with `uv run --directory /path/to/codereviewbuddy codereviewbuddy`.
3. Reinstall to refresh cached deps: `uv tool install --reinstall codereviewbuddy`.

## MCP Tools

| Tool | Tags | Description |
| ---- | ---- | ----------- |
| `summarize_review_status` | query, discovery | Lightweight stack-wide overview with severity counts — start here |
| `triage_review_comments` | query | Only actionable threads, pre-classified with severity and suggested actions |
| `list_review_comments` | query | All review threads with reviewer ID, status, staleness, and auto-discovered stack |
| `list_stack_review_comments` | query | Comments for multiple PRs in one call, grouped by PR number |
| `resolve_comment` | command | Resolve a single inline thread by GraphQL node ID (`PRRT_...`) |
| `resolve_stale_comments` | command | Bulk-resolve threads on files modified since the review |
| `reply_to_comment` | command | Reply to inline threads (`PRRT_`), PR-level reviews (`PRR_`), or bot comments (`IC_`) |
| `create_issue_from_comment` | command | Create a GitHub issue from a review comment with labels and PR backlink |
| `diagnose_ci` | query | Diagnose CI failures — finds the failed run, jobs, steps, and error lines in one call |
| `stack_activity` | query | Chronological activity feed across a PR stack with a `settled` flag |
| `list_recent_unresolved` | query | Scan recently merged PRs for unresolved review threads |
| `review_pr_descriptions` | query | Analyze PR descriptions for quality issues (empty body, boilerplate, missing linked issues) |
| `show_config` | discovery | Show active configuration with human-readable explanation |

## Configuration

codereviewbuddy works **zero-config** with sensible defaults. All configuration is via `CRB_*` environment variables in the `"env"` block of your MCP client config — no config files needed. Nested settings use `__` (double underscore) as a delimiter. See the [dev setup](#from-source-development) above for a fully-commented example.

### All settings

| Env var | Type | Default | Description |
| ------- | ---- | ------- | ----------- |
| `CRB_REVIEWERS` | JSON | `{}` | Per-reviewer overrides as a JSON string (see [below](#per-reviewer-overrides)) |
| `CRB_PR_DESCRIPTIONS__ENABLED` | bool | `true` | Whether `review_pr_descriptions` tool is available |
| `CRB_SELF_IMPROVEMENT__ENABLED` | bool | `false` | Agents file issues when they encounter server gaps |
| `CRB_SELF_IMPROVEMENT__REPO` | string | `""` | Repository to file issues against (e.g. `owner/repo`) |
| `CRB_DIAGNOSTICS__IO_TAP` | bool | `false` | Log stdin/stdout for transport debugging |
| `CRB_DIAGNOSTICS__TOOL_CALL_HEARTBEAT` | bool | `false` | Emit heartbeat entries for long-running tool calls |
| `CRB_DIAGNOSTICS__HEARTBEAT_INTERVAL_MS` | int | `5000` | Heartbeat cadence in milliseconds |
| `CRB_DIAGNOSTICS__INCLUDE_ARGS_FINGERPRINT` | bool | `true` | Log args hash/size in tool call logs |

### Severity levels

Each reviewer adapter classifies comments using its own format. Currently only Devin has a known severity format (emoji markers). Unblocked and CodeRabbit comments default to `info` until their formats are investigated.

**Devin's emoji markers:**

| Emoji | Level | Meaning |
| ----- | ----- | ------- |
| 🔴 | `bug` | Critical issue, must fix before merge |
| 🚩 | `flagged` | Likely needs a code change |
| 🟡 | `warning` | Worth addressing but not blocking |
| 📝 | `info` | Informational, no action required |
| *(none)* | `info` | Default when no marker is present |

Reviewers without a known format classify all comments as `info`. This means `resolve_levels = ["info"]` would allow resolving all their threads, while `resolve_levels = []` blocks everything.

### Per-reviewer overrides

Each adapter defines sensible defaults. To override, set `CRB_REVIEWERS` as a JSON string:

```jsonc
"CRB_REVIEWERS": "{\"devin\": {\"enabled\": false}, \"greptile\": {\"resolve_levels\": [\"info\", \"warning\"]}}"
```

Available fields per reviewer:

| Field | Type | Default | Description |
| ----- | ---- | ------- | ----------- |
| `enabled` | bool | `true` | Whether this reviewer's threads appear in results |
| `auto_resolve_stale` | bool | varies | Whether `resolve_stale_comments` touches this reviewer's threads |
| `resolve_levels` | list | varies | Severity levels allowed to be resolved (`info`, `warning`, `flagged`, `bug`) |
| `require_reply_before_resolve` | bool | `true` | Block resolve unless someone replied explaining the fix |

**Adapter defaults** (used when no override is set):

| Reviewer | `auto_resolve_stale` | `resolve_levels` |
| -------- | ------------------- | ---------------- |
| Unblocked | `true` | all |
| Devin | `false` | `["info"]` |
| CodeRabbit | `false` | `[]` (none) |
| Greptile | `true` | all |

### Resolve enforcement

The `resolve_levels` config is **enforced server-side**. If an agent tries to resolve a thread whose severity exceeds the allowed levels, the server returns an error. This prevents agents from resolving critical review comments regardless of their instructions.

For example, with the default config, resolving a 🔴 bug from Devin is blocked — only 📝 info threads can be resolved.

## Reviewer behavior

| Reviewer | Auto-reviews on push | Auto-resolves comments | Re-review trigger |
| -------- | ------------------- | -------------------- | ----------------- |
| **Unblocked** | No | No | `gh pr comment <N> --body "@unblocked please re-review"` |
| **Devin** | Yes | Yes | Auto on push (no action needed) |
| **CodeRabbit** | Yes | Yes | Auto on push (no action needed) |
| **Greptile** | No (not on force push) | No | `gh pr comment <N> --body "@greptileai review"` |

## Typical workflow

```
1. summarize_review_status()                     # Stack-wide overview — start here
2. triage_review_comments(pr_numbers=[42, 43])   # Only actionable threads with suggested actions
3. resolve_stale_comments(pr_number=42)          # Batch-resolve changed files
4. # Fix bugs flagged by triage, then:
5. reply_to_comment(42, thread_id, "Fixed in ...")  # Reply explaining the fix
6. create_issue_from_comment(thread_id, "Improve X")  # Track followups as issues
7. diagnose_ci(pr_number=42)                     # If CI fails, diagnose in one call
```

Each tool response includes `next_steps` hints guiding the agent to the right follow-up call. For stacked PRs, all query tools auto-discover the stack when `pr_numbers` is omitted.

## Development

```bash
git clone https://github.com/detailobsessed/codereviewbuddy.git
cd codereviewbuddy
uv sync
```

### Testing

```bash
poe test          # Run tests (excludes slow)
poe test-cov      # Run with coverage report
poe test-all      # Run all tests including slow
```

### Quality checks

```bash
poe lint          # ruff check
poe typecheck     # ty check
poe check         # lint + typecheck
poe prek          # run all pre-commit hooks
```

### Architecture

The server is built on [FastMCP v3](https://github.com/jlowin/fastmcp) with a clean separation:

- **`server.py`** — FastMCP server with tool registration, middleware, instructions, and recovery-guided error handling
- **`config.py`** — Per-reviewer configuration (`CRB_*` env vars via pydantic-settings, severity classifier, resolve policy)
- **`tools/`** — Tool implementations (`comments.py`, `stack.py`, `ci.py`, `descriptions.py`, `issues.py`)
- **`reviewers/`** — Pluggable reviewer adapters with behavior flags (auto-resolve, re-review triggers)
- **`gh.py`** — Thin wrapper around the `gh` CLI for GraphQL and REST calls
- **`models.py`** — Pydantic models for typed tool outputs with `next_steps` and `message` fields for agent guidance

All blocking `gh` CLI calls are wrapped with `call_sync_fn_in_threadpool` to avoid blocking the async event loop.

## Template Updates

This project was generated with [copier-uv-bleeding](https://github.com/detailobsessed/copier-uv-bleeding). To pull the latest template changes:

```bash
copier update --trust .
```
