---
id: cod-3tsf
status: open
deps: []
links: []
created: 2026-02-07T01:59:16Z
type: feature
priority: 1
assignee: Ismar Iljazovic
tags: [mcp, polling]
---
# wait_for_reviews polling tool

Add a wait_for_reviews MCP tool that polls until AI reviewers have completed their review cycle on a PR. Must handle:

1. Reviewers that post comments (Unblocked finds 2 issues, Devin finds 3 issues)
2. Reviewers that approve with NO comments (everything looks good)
3. Timeout after configurable duration

Detection signals:

- Check runs: CodeRabbit creates check runs (IN_PROGRESS -> COMPLETED)
- Review submissions: latestOpinionatedReviews shows submitted reviews (APPROVED, CHANGES_REQUESTED)
- Bot comments: presence of known bot comments on the PR timeline
- Heuristic fallback: if a push happened recently and no bot activity yet, still waiting

Returns a status dict per reviewer: {reviewer: status, has_comments: bool, review_state: str}

Design consideration: MCP has no server-push or event system. This tool should long-poll (sleep loop) with configurable timeout and poll interval. The agent calls it and the tool blocks until reviews are detected or timeout.

Edge case: reviewer simply has nothing to say (clean PR). Need to distinguish 'still reviewing' from 'reviewed and found nothing'. Check run completion + review submission are the reliable signals for this.
