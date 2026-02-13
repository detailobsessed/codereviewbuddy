# Issue 65 Observability Tracking

This document tracks all temporary diagnostics added to investigate intermittent MCP unresponsiveness in issue #65 and provides a cleanup checklist for removing them after root cause is fixed upstream.

## Tracking identifier

All issue-65-specific log entries and code markers include this sentinel:

- `CRB-ISSUE-65-TRACKING`

Use it to find all temporary diagnostics quickly:

```bash
rg "CRB-ISSUE-65-TRACKING" src tests docs
```

## What was added

### 1) Tool-call lifecycle diagnostics (`tool_calls.jsonl`)

File: `src/codereviewbuddy/middleware.py`

Added richer per-call metadata:

- `call_type` (`read` / `write`)
- `task_id`
- `mono_start`, `mono_end`
- `elapsed_ms_precise` (float ms)
- optional argument metadata:
  - `args_size_bytes`
  - `args_fingerprint` (SHA256 of canonicalized JSON args)

Added optional in-flight heartbeat entries:

- `phase: "heartbeat"`
- `inflight_ms`
- emitted every configurable interval while `call_next` is pending.

All started/completed/heartbeat entries include:

- `tracking_tag: "CRB-ISSUE-65-TRACKING"`

Rotation behavior:

- `tool_calls.jsonl` truncates to the most recent 1000 lines every 100 writes.

### 2) Subprocess-level gh CLI timing (`gh_calls.jsonl`)

File: `src/codereviewbuddy/gh.py`

Every `gh` CLI subprocess invocation is logged with:

- `ts`, `ts_end` — start and end wall-clock timestamps
- `cmd` — summarized command (e.g. `api graphql`, `pr comment 42`)
- `duration_ms` — wall-clock duration of the subprocess
- `exit_code` — process exit code
- `stdout_bytes` — size of stdout (to detect large payloads)
- `stderr` — first 500 chars of stderr (rate limit messages, auth errors, etc.)
- `error` — set to `"FileNotFoundError"` if `gh` is not installed

All entries include:

- `tracking_tag: "CRB-ISSUE-65-TRACKING"`

Rotation behavior:

- `gh_calls.jsonl` truncates to the most recent 10,000 lines every 100 writes.

### Analysis query (jq)

```bash
# Find slow gh calls (>10s)
jq -r 'select(.duration_ms>=10000) | [.ts,.cmd,.duration_ms,.exit_code,.stderr] | @tsv' ~/.codereviewbuddy/gh_calls.jsonl
```

### 3) Transport-level envelope diagnostics (`io_tap.jsonl`)

File: `src/codereviewbuddy/io_tap.py`

Enhanced JSON-RPC metadata extraction:

- `rpc_envelope`: `request` / `notification` / `response` / `parse_error`
- `rpc_error_code` (for error responses)

Added:

- `tracking_tag: "CRB-ISSUE-65-TRACKING"` to all io tap rows.

Rotation behavior:

- On startup, `io_tap.jsonl` is truncated to the most recent 10,000 lines.
- During runtime, it truncates to the most recent 10,000 lines every 100 writes.

### 4) Runtime diagnostics controls

Files:

- `src/codereviewbuddy/config.py`
- `src/codereviewbuddy/server.py`

Added config options under `[diagnostics]`:

- `tool_call_heartbeat = false`
- `heartbeat_interval_ms = 5000`
- `include_args_fingerprint = true`

Server startup now applies these options to the middleware instance.

## Recommended repro config

```toml
[diagnostics]
io_tap = true
tool_call_heartbeat = true
heartbeat_interval_ms = 1000
include_args_fingerprint = true
```

## Analysis workflow (jq)

### A) Find long-running calls

```bash
jq -r 'select(.phase=="completed") | select(.duration_ms>=30000) | [.ts,.call_id,.tool,.call_type,.duration_ms,.elapsed_ms_precise] | @tsv' ~/.codereviewbuddy/tool_calls.jsonl
```

### B) Find calls with heartbeat but late completion

```bash
jq -r 'select(.tracking_tag=="CRB-ISSUE-65-TRACKING") | [.call_id,.phase,.tool,.inflight_ms,.duration_ms] | @tsv' ~/.codereviewbuddy/tool_calls.jsonl
```

### C) Transport health and response shape

```bash
jq -r 'select(.tracking_tag=="CRB-ISSUE-65-TRACKING") | [.ts,.direction,.phase,.rpc_id,.rpc_envelope,.rpc_method,.rpc_error_code] | @tsv' ~/.codereviewbuddy/io_tap.jsonl
```

## Cleanup checklist (after issue is fixed)

1. Remove temporary tracking sentinel constants and fields:
   - `tracking_tag`
   - `ISSUE_65_TRACKING_TAG`
2. Decide whether to keep or remove:
   - heartbeat logging path
   - args fingerprint/size metadata
   - rpc envelope/error extraction
3. Remove this document if diagnostics are fully removed.
4. Run tests:
   - `uv run pytest -q tests/test_write_middleware.py tests/test_io_tap.py tests/test_config.py --no-cov`

## Notes

The diagnostics are intentionally designed to distinguish among:

- no dispatch (`started` missing)
- in-flight execution (`heartbeat` present)
- completed execution but delayed/absent response emission
- transport still alive (pings/responses continue) while specific calls stall.
