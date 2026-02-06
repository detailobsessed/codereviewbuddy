#!/usr/bin/env bash
# propcom: Show unresolved PR review comments for AI code review workflows
# Usage: propcom [PR_NUMBER]

set -e

PR_NUMBER="${1:-}"

get_repo_info() {
    local remote_url
    remote_url=$(git remote get-url origin 2>/dev/null) || {
        echo "Error: Not in a git repository or no origin remote" >&2
        exit 1
    }

    if [[ "$remote_url" =~ github\.com[:/]([^/]+)/([^/.]+)(\.git)?$ ]]; then
        echo "${BASH_REMATCH[1]} ${BASH_REMATCH[2]}"
    else
        echo "Error: Could not parse GitHub repo from: $remote_url" >&2
        exit 1
    fi
}

get_pr_number() {
    local owner="$1" repo="$2"
    local branch
    branch=$(git branch --show-current 2>/dev/null)

    [[ -z "$branch" ]] && { echo "Error: Could not determine current branch" >&2; exit 1; }

    local pr_num
    pr_num=$(gh pr list -R "$owner/$repo" --head "$branch" --json number -q '.[0].number' 2>/dev/null)

    [[ -z "$pr_num" ]] && { echo "Error: No PR found for branch '$branch'" >&2; exit 1; }

    echo "$pr_num"
}

read -r OWNER REPO <<< "$(get_repo_info)"
[[ -z "$PR_NUMBER" ]] && PR_NUMBER=$(get_pr_number "$OWNER" "$REPO")

# shellcheck disable=SC2016
result=$(gh api graphql -f query='
query($owner: String!, $repo: String!, $pr: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      title
      url
      reviewThreads(first: 100) {
        nodes {
          isResolved
          comments(first: 1) {
            nodes { author { login } body path line }
          }
        }
      }
      reviews(first: 100) {
        nodes {
          author { login }
          state
          body
          createdAt
        }
      }
    }
  }
}' -f owner="$OWNER" -f repo="$REPO" -F pr="$PR_NUMBER")

total=$(echo "$result" | jq '[.data.repository.pullRequest.reviewThreads.nodes[]] | length')
unresolved=$(echo "$result" | jq '[.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false)] | length')
review_comments=$(echo "$result" | jq '[.data.repository.pullRequest.reviews.nodes[] | select(.state == "COMMENTED" and (.body | length) > 0)] | length')
pr_title=$(echo "$result" | jq -r '.data.repository.pullRequest.title')
pr_url=$(echo "$result" | jq -r '.data.repository.pullRequest.url')

echo "📋 PR #$PR_NUMBER: $pr_title"
echo "🔗 $pr_url"
echo ""
echo "📊 Inline comments: $unresolved unresolved / $total total"
[[ "$review_comments" -gt 0 ]] && echo "📊 Review-level comments: $review_comments"
echo ""

[[ "$unresolved" -eq 0 && "$review_comments" -eq 0 ]] && { echo "✅ All comments resolved!"; exit 0; }

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

if [[ "$unresolved" -gt 0 ]]; then
    echo "$result" | jq -r '
.data.repository.pullRequest.reviewThreads.nodes[]
| select(.isResolved == false)
| "📍 \(.comments.nodes[0].path):\(.comments.nodes[0].line // "?")\n👤 \(.comments.nodes[0].author.login)\n\n\(.comments.nodes[0].body | split("\n")[0:20] | join("\n"))\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"'
fi

if [[ "$review_comments" -gt 0 ]]; then
    echo "$result" | jq -r '
.data.repository.pullRequest.reviews.nodes[]
| select(.state == "COMMENTED" and (.body | length) > 0)
| .body |= (
    # Remove HTML tags
    gsub("<[^>]+>"; "") |
    # Unescape common HTML entities
    gsub("&lt;"; "<") | gsub("&gt;"; ">") | gsub("&amp;"; "&") |
    # Remove excessive blank lines
    gsub("\n\n\n+"; "\n\n") |
    # Truncate at common boilerplate sections
    gsub("\n\nAbout (Unblocked|CodeRabbit).*$"; ""; "m")
  )
| "📝 Review by \(.author.login)\n🕒 \(.createdAt)\n\n\(.body)\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"'
fi
