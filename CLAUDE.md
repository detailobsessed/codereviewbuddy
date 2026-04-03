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

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **codereviewbuddy** (1078 symbols, 3242 relationships, 78 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `gitnexus_impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `gitnexus_detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `gitnexus_query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `gitnexus_context({name: "symbolName"})`.

## When Debugging

1. `gitnexus_query({query: "<error or symptom>"})` — find execution flows related to the issue
2. `gitnexus_context({name: "<suspect function>"})` — see all callers, callees, and process participation
3. `READ gitnexus://repo/codereviewbuddy/process/{processName}` — trace the full execution flow step by step
4. For regressions: `gitnexus_detect_changes({scope: "compare", base_ref: "main"})` — see what your branch changed

## When Refactoring

- **Renaming**: MUST use `gitnexus_rename({symbol_name: "old", new_name: "new", dry_run: true})` first. Review the preview — graph edits are safe, text_search edits need manual review. Then run with `dry_run: false`.
- **Extracting/Splitting**: MUST run `gitnexus_context({name: "target"})` to see all incoming/outgoing refs, then `gitnexus_impact({target: "target", direction: "upstream"})` to find all external callers before moving code.
- After any refactor: run `gitnexus_detect_changes({scope: "all"})` to verify only expected files changed.

## Never Do

- NEVER edit a function, class, or method without first running `gitnexus_impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `gitnexus_rename` which understands the call graph.
- NEVER commit changes without running `gitnexus_detect_changes()` to check affected scope.

## Tools Quick Reference

| Tool | When to use | Command |
|------|-------------|---------|
| `query` | Find code by concept | `gitnexus_query({query: "auth validation"})` |
| `context` | 360-degree view of one symbol | `gitnexus_context({name: "validateUser"})` |
| `impact` | Blast radius before editing | `gitnexus_impact({target: "X", direction: "upstream"})` |
| `detect_changes` | Pre-commit scope check | `gitnexus_detect_changes({scope: "staged"})` |
| `rename` | Safe multi-file rename | `gitnexus_rename({symbol_name: "old", new_name: "new", dry_run: true})` |
| `cypher` | Custom graph queries | `gitnexus_cypher({query: "MATCH ..."})` |

## Impact Risk Levels

| Depth | Meaning | Action |
|-------|---------|--------|
| d=1 | WILL BREAK — direct callers/importers | MUST update these |
| d=2 | LIKELY AFFECTED — indirect deps | Should test |
| d=3 | MAY NEED TESTING — transitive | Test if critical path |

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/codereviewbuddy/context` | Codebase overview, check index freshness |
| `gitnexus://repo/codereviewbuddy/clusters` | All functional areas |
| `gitnexus://repo/codereviewbuddy/processes` | All execution flows |
| `gitnexus://repo/codereviewbuddy/process/{name}` | Step-by-step execution trace |

## Self-Check Before Finishing

Before completing any code modification task, verify:

1. `gitnexus_impact` was run for all modified symbols
2. No HIGH/CRITICAL risk warnings were ignored
3. `gitnexus_detect_changes()` confirms changes match expected scope
4. All d=1 (WILL BREAK) dependents were updated

## Keeping the Index Fresh

After committing code changes, the GitNexus index becomes stale. Re-run analyze to update it:

```bash
npx gitnexus analyze
```

If the index previously included embeddings, preserve them by adding `--embeddings`:

```bash
npx gitnexus analyze --embeddings
```

To check whether embeddings exist, inspect `.gitnexus/meta.json` — the `stats.embeddings` field shows the count (0 means no embeddings). **Running analyze without `--embeddings` will delete any previously generated embeddings.**

> Claude Code users: A PostToolUse hook handles this automatically after `git commit` and `git merge`.

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
