<!-- mcp-name: io.github.detailobsessed/codereviewbuddy -->
# codereviewbuddy

[![ci](https://github.com/detailobsessed/codereviewbuddy/workflows/ci/badge.svg)](https://github.com/detailobsessed/codereviewbuddy/actions?query=workflow%3Aci)
[![Python 3.14+](https://img.shields.io/badge/python-3.14+-blue.svg)](https://www.python.org/downloads/)
[![FastMCP](https://img.shields.io/badge/FastMCP-2.14.5-green.svg)](https://github.com/jlowin/fastmcp)

An MCP server that helps your AI coding agent interact with AI code reviewers — smoothly.

Manages review comments from **Unblocked**, **Devin**, and **CodeRabbit** on GitHub PRs with staleness detection, batch resolution, and re-review triggering.

## Features

- **List review comments** with reviewer identification and staleness detection
- **Resolve comments** individually or bulk-resolve stale ones (files changed since review)
- **Reply to review threads** directly from your agent
- **Request re-reviews** with per-reviewer logic (manual trigger for Unblocked, auto for Devin/CodeRabbit)
- **Zero config auth** — uses `gh` CLI, no PAT tokens or `.env` files

## Prerequisites

- [GitHub CLI (`gh`)](https://cli.github.com/) installed and authenticated (`gh auth login`)
- Python 3.14+

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
      "args": ["run", "--directory", "/path/to/codereviewbuddy", "python", "main.py"]
    }
  }
}
```

## MCP Tools

|Tool|Description|
|---|---|
|`list_review_comments`|Fetch all review threads with reviewer ID, status, and staleness|
|`resolve_comment`|Resolve a single thread by GraphQL node ID (`PRRT_...`)|
|`resolve_stale_comments`|Bulk-resolve threads on files modified since the review|
|`reply_to_comment`|Reply to a review thread|
|`request_rereview`|Trigger re-reviews per reviewer (handles differences automatically)|

## Installation

```bash
pip install codereviewbuddy
```

With [`uv`](https://docs.astral.sh/uv/):

```bash
uv tool install codereviewbuddy
```

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

<!-- TODO: Add usage examples with actual PR workflows -->
<!-- TODO: Add architecture diagram -->
<!-- TODO: Add contributing guide link -->

## Template Updates

This project was generated with [copier-uv-bleeding](https://github.com/detailobsessed/copier-uv-bleeding). To pull the latest template changes:

```bash
copier update --trust .
```
