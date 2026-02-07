---
id: cod-yy8n
status: closed
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


## Notes

**2026-02-07T01:16:37Z**

CI workflow already exists at .github/workflows/ci.yml with tests job running poe test-cov. PYTHON_GIL=0 is set in the poe task definition so it propagates to CI automatically. No changes needed.
