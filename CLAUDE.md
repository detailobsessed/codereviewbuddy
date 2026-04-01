# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

codereviewbuddy is an MCP (Model Context Protocol) server built on FastMCP v3 that helps AI coding agents manage GitHub PR review comments. It provides tools for comment triage, CI diagnosis, stacked PR handling, issue tracking, and PR description review.

**Tech stack**: Python 3.14+, uv package manager, FastMCP v3, httpx, Pydantic Settings.

## Common Commands

```bash
uv sync                # Install all dependencies
poe check              # Lint (ruff) + type check (ty) in parallel
poe fix                # Auto-fix lint + format
poe test               # Run tests excluding @pytest.mark.slow
poe test-all           # Run all tests including slow
poe test-cov           # Run tests with coverage (90% minimum required)
poe test-affected      # Run only tests affected by changes (testmon)
poe prek               # Run all pre-commit hooks (prek run --all-files)
poe docs               # Serve docs locally (zensical serve)
```

Run a single test file or test function:

```bash
uv run pytest tests/test_comments.py
uv run pytest tests/test_comments.py::test_function_name -v
```

## Architecture

**Entry points**:

- CLI: `codereviewbuddy.cli:app` (cyclopts) — default command starts the MCP server
- MCP server: `codereviewbuddy.server:mcp` (FastMCP instance)

**Core modules** (`src/codereviewbuddy/`):

- `server.py` — FastMCP server setup, tool registration, recovery-guided error handling (classifies errors by type and returns actionable hints)
- `config.py` — Pydantic Settings configuration via `CRB_*` env vars (zero-config by default)
- `models.py` — Pydantic response models with `message` and `next_steps` fields for agent guidance
- `gh.py` — Primary GitHub access: wraps `gh` CLI subprocess for GraphQL/REST calls
- `github_api.py` — Fallback httpx-based GitHub API client using PAT tokens
- `middleware.py` — WriteOperationMiddleware for tool call interception
- `cli.py` — CLI with subcommands: `serve`, `install`, `check-env`

**Tool modules** (`src/codereviewbuddy/tools/`):

- `comments.py` — Review comment management (fetch, triage, reply)
- `stack.py` — Stacked PR detection and handling
- `ci.py` — CI failure diagnosis
- `issues.py` — GitHub issue creation
- `descriptions.py` — PR description review

Tools are tagged as `query` (read-only), `command` (write), or `discovery` (metadata).

**Workspace resolution order**: MCP roots > `CRB_WORKSPACE` env var > process cwd (if git repo).

**GitHub auth priority**: `GH_TOKEN` > `GITHUB_TOKEN` > `gh auth token`.

## Testing

- pytest with `asyncio_mode = "auto"` — async tests run without extra decorators
- **respx** mocks httpx calls (for `github_api.py` tests)
- **pytest-mock** mocks subprocesses (for `gh.py` tests)
- Coverage minimum: 90% with branch coverage
- Tests are randomized via pytest-randomly

## Commit Conventions

Conventional Commits enforced by pre-commit hook. Types: `feat`, `fix`, `docs`, `refactor`, `test`, `ci`, `chore`, `perf`, `build`, `style`, `deps`. Changelog and versioning are automated via python-semantic-release.

## Ruff Configuration

Line length 140, target Python 3.14, preview features enabled. Tests have relaxed rules (asserts, hardcoded strings, subprocess, print statements allowed).
