<!-- mcp-name: io.github.detailobsessed/codereviewbuddy -->
# codereviewbuddy

[![ci](https://github.com/detailobsessed/codereviewbuddy/workflows/ci/badge.svg)](https://github.com/detailobsessed/codereviewbuddy/actions?query=workflow%3Aci)
[![Python 3.14+](https://img.shields.io/badge/python-3.14+-blue.svg)](https://www.python.org/downloads/)
[![FastMCP v3 beta](https://img.shields.io/badge/FastMCP-v3.0.0b2-orange.svg)](https://github.com/jlowin/fastmcp)

An MCP server that helps your AI coding agent interact with AI code reviewers — smoothly.

Manages review comments from **Unblocked**, **Devin**, and **CodeRabbit** on GitHub PRs with staleness detection, batch resolution, re-review triggering, and issue tracking.

> [!WARNING]
> **Bleeding edge.** This server runs on **Python 3.14** and **FastMCP v3 beta** (`>=3.0.0b2`). FastMCP v3 is pre-release software — APIs may change before stable. We track the beta closely and pin to specific beta versions in `uv.lock` for reproducibility, but be aware that upstream breaking changes are possible.

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

### Windsurf

Add to your MCP settings (`~/.codeium/windsurf/mcp_config.json`):

```json
{
  "mcpServers": {
    "codereviewbuddy": {
      "command": "uvx",
      "args": ["codereviewbuddy"]
    }
  }
}
```

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "codereviewbuddy": {
      "command": "uvx",
      "args": ["codereviewbuddy"]
    }
  }
}
```

### Cursor

Add to `.cursor/mcp.json` in your project:

```json
{
  "mcpServers": {
    "codereviewbuddy": {
      "command": "uvx",
      "args": ["codereviewbuddy"]
    }
  }
}
```

### From source (development)

```json
{
  "mcpServers": {
    "codereviewbuddy": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/codereviewbuddy", "codereviewbuddy"]
    }
  }
}
```

## MCP Tools

| Tool | Description |
| ---- | ----------- |
| `list_review_comments` | Fetch all review threads, PR-level reviews, and bot comments with reviewer ID, status, and staleness |
| `list_stack_review_comments` | Fetch comments for multiple PRs in a stack in one call, grouped by PR number |
| `resolve_comment` | Resolve a single inline thread by GraphQL node ID (`PRRT_...`) |
| `resolve_stale_comments` | Bulk-resolve threads on files modified since the review, with smart skip for auto-resolving reviewers |
| `reply_to_comment` | Reply to inline threads (`PRRT_`), PR-level reviews (`PRR_`), or bot comments (`IC_`) |
| `request_rereview` | Trigger re-reviews per reviewer (handles differences automatically) |
| `create_issue_from_comment` | Create a GitHub issue from a review comment with labels, PR backlink, and quoted text |
| `check_for_updates` | Check if a newer version is available on PyPI |

## Reviewer behavior

| Reviewer | Auto-reviews on push | Auto-resolves comments | Re-review trigger |
| -------- | ------------------- | -------------------- | ----------------- |
| **Unblocked** | No | No | `request_rereview` posts "@unblocked please re-review" |
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
- **`tools/`** — Tool implementations (`comments.py`, `issues.py`, `rereview.py`, `version.py`)
- **`reviewers/`** — Pluggable reviewer adapters with behavior flags (auto-resolve, re-review triggers)
- **`gh.py`** — Thin wrapper around the `gh` CLI for GraphQL and REST calls
- **`models.py`** — Pydantic models for typed tool outputs

All blocking `gh` CLI calls are wrapped with `call_sync_fn_in_threadpool` to avoid blocking the async event loop.

## Template Updates

This project was generated with [copier-uv-bleeding](https://github.com/detailobsessed/copier-uv-bleeding). To pull the latest template changes:

```bash
copier update --trust .
```
