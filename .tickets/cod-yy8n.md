---
id: cod-yy8n
status: open
deps: []
links: []
created: 2026-02-07T00:39:17Z
type: chore
priority: 2
assignee: Ismar Iljazovic
tags: [ci, testing]
---
# CI workflow for MCP server tests

Add pytest to CI pipeline. Ensure integration tests (marked @pytest.mark.integration) can be skipped in CI without gh auth. Unit tests should always run.

