<!-- mcp-name: io.github.detailobsessed/codereviewbuddy -->
# codereviewbuddy

[![ci](https://github.com/detailobsessed/codereviewbuddy/workflows/ci/badge.svg)](https://github.com/detailobsessed/codereviewbuddy/actions?query=workflow%3Aci)
[![release](https://img.shields.io/github/v/release/detailobsessed/codereviewbuddy)](https://github.com/detailobsessed/codereviewbuddy/releases)
[![documentation](https://img.shields.io/badge/docs-mkdocs-blue.svg)](https://detailobsessed.github.io/codereviewbuddy/)
[![Python 3.14+](https://img.shields.io/badge/python-3.14+-blue.svg)](https://www.python.org/downloads/)
[![FastMCP v3 prerelease](https://img.shields.io/badge/FastMCP-v3.0.0rc1-orange.svg)](https://github.com/jlowin/fastmcp)

An MCP server that helps your AI coding agent interact with AI code reviewers — smoothly.

Manages review comments from **Unblocked**, **Devin**, and **CodeRabbit** on GitHub PRs with staleness detection, batch resolution, re-review triggering, and issue tracking.

> [!WARNING]
> **Bleeding edge.** This server runs on **Python 3.14** and **FastMCP v3 prerelease** (`>=3.0.0rc1`). FastMCP v3 is pre-release software — APIs may change before stable. We track it closely and pin versions in `uv.lock` for reproducibility, but be aware that upstream breaking changes are possible.

## Features

### Review comment management

- **List review comments** — inline threads, PR-level reviews, and bot comments (codecov, netlify, vercel, etc.) with reviewer identification and staleness detection
- **Stacked PR support** — `list_stack_review_comments` fetches comments across an entire PR stack in one call
- **Resolve comments** — individually or bulk-resolve stale ones (files changed since the review)
- **Smart skip logic** — `resolve_stale_comments` skips reviewers that auto-resolve their own comments (Devin, CodeRabbit), only batch-resolving threads from reviewers that don't (Unblocked)
- **Reply to anything** — inline review threads (`PRRT_`), PR-level reviews (`PRR_`), and bot issue comments (`IC_`) all routed to the correct GitHub API
- **Request re-reviews** — per-reviewer logic handles differences automatically (manual trigger for Unblocked, auto for Devin/CodeRabbit)

### Issue tracking

- **Create issues from review comments** — turn useful AI suggestions into GitHub issues with labels, PR backlinks, file/line location, and quoted comment text

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

Add the following to your MCP client's config JSON (Windsurf, Claude Desktop, Cursor, VS Code, Claude Code, Gemini CLI, etc. — the JSON shape is the (roughly) same everywhere and I assume you know what your client needs):

```json
{
  "mcpServers": {
    "codereviewbuddy": {
      "command": "uvx",
      "args": ["--prerelease=allow", "codereviewbuddy@latest"],
      "env": {
        "CRB_WORKSPACE": "/path/to/your/project"
      }
    }
  }
}
```

> **Why `CRB_WORKSPACE`?** The server needs to know which project you're working in so `gh` CLI commands target the right repo. Without this, auto-detection may pick the wrong repository.
>
> **Why `--prerelease=allow`?** codereviewbuddy depends on FastMCP v3 prerelease (`>=3.0.0rc1`). Without this flag, `uvx` refuses to resolve pre-release dependencies.
>
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
        // Required: point at the repo you're reviewing PRs in
        "CRB_WORKSPACE": "/path/to/your/project",

        // Self-improvement: agents file issues when they hit server gaps
        "CRB_SELF_IMPROVEMENT__ENABLED": "true",
        "CRB_SELF_IMPROVEMENT__REPO": "detailobsessed/codereviewbuddy",

        // PR description review (enabled by default)
        "CRB_PR_DESCRIPTIONS__ENABLED": "true",

        // Diagnostics: transport and tool call logging
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

1. Prefer `uvx --prerelease=allow codereviewbuddy@latest` in MCP client config.
2. For local source checkouts, launch with `uv run --directory /path/to/codereviewbuddy codereviewbuddy`.
3. Reinstall to refresh cached deps: `uv tool install --reinstall codereviewbuddy`.

## MCP Tools

| Tool | Description |
| ---- | ----------- |
| `summarize_review_status` | Lightweight stack-wide overview with severity counts — auto-discovers stack when `pr_numbers` omitted |
| `list_review_comments` | Fetch all review threads with reviewer ID, status, staleness, and auto-discovered `stack` field |
| `list_stack_review_comments` | Fetch comments for multiple PRs in a stack in one call, grouped by PR number |
| `resolve_comment` | Resolve a single inline thread by GraphQL node ID (`PRRT_...`) |
| `resolve_stale_comments` | Bulk-resolve threads on files modified since the review, with smart skip for auto-resolving reviewers |
| `reply_to_comment` | Reply to inline threads (`PRRT_`), PR-level reviews (`PRR_`), or bot comments (`IC_`) |
| `request_rereview` | Trigger re-reviews per reviewer (handles differences automatically) |
| `create_issue_from_comment` | Create a GitHub issue from a review comment with labels, PR backlink, and quoted text |
| `review_pr_descriptions` | Analyze PR descriptions across a stack for quality issues (empty body, boilerplate, missing linked issues) |

## Configuration

codereviewbuddy works **zero-config** with sensible defaults. All configuration is via `CRB_*` environment variables in the `"env"` block of your MCP client config — no config files needed. Nested settings use `__` (double underscore) as a delimiter. See the [dev setup](#from-source-development) above for a fully-commented example.

### All settings

| Env var | Type | Default | Description |
| ------- | ---- | ------- | ----------- |
| `CRB_WORKSPACE` | string | *(auto-detect)* | Project directory for `gh` CLI — set this to avoid wrong-repo detection |
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

### Resolve enforcement

The `resolve_levels` config is **enforced server-side**. If an agent tries to resolve a thread whose severity exceeds the allowed levels, the server returns an error. This prevents agents from resolving critical review comments regardless of their instructions.

For example, with the default config, resolving a 🔴 bug from Devin is blocked — only 📝 info threads can be resolved.

## Reviewer behavior

| Reviewer | Auto-reviews on push | Auto-resolves comments | Re-review trigger |
| -------- | ------------------- | -------------------- | ----------------- |
| **Unblocked** | No | No | `request_rereview` posts a configurable comment (default: "@unblocked please re-review") |
| **Devin** | Yes | Yes | Auto on push (no action needed) |
| **CodeRabbit** | Yes | Yes | Auto on push (no action needed) |

## Typical workflow

```
1. Push a fix
2. list_review_comments(pr_number=42)           # See all threads with staleness
3. resolve_stale_comments(pr_number=42)          # Batch-resolve changed files
4. reply_to_comment(42, thread_id, "Fixed in ...")  # Reply to remaining threads
5. request_rereview(pr_number=42)                # Trigger fresh review cycle
```

For stacked PRs, use `list_stack_review_comments` with all PR numbers to get a full picture before deciding what to fix.

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

- **`server.py`** — FastMCP server with tool registration, middleware, and instructions
- **`config.py`** — Per-reviewer configuration (`CRB_*` env vars via pydantic-settings, severity classifier, resolve policy)
- **`tools/`** — Tool implementations (`comments.py`, `stack.py`, `descriptions.py`, `issues.py`, `rereview.py`)
- **`reviewers/`** — Pluggable reviewer adapters with behavior flags (auto-resolve, re-review triggers)
- **`gh.py`** — Thin wrapper around the `gh` CLI for GraphQL and REST calls
- **`models.py`** — Pydantic models for typed tool outputs

All blocking `gh` CLI calls are wrapped with `call_sync_fn_in_threadpool` to avoid blocking the async event loop.

## Template Updates

This project was generated with [copier-uv-bleeding](https://github.com/detailobsessed/copier-uv-bleeding). To pull the latest template changes:

```bash
copier update --trust .
```
