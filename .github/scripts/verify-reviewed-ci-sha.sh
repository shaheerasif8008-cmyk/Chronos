#!/usr/bin/env bash
set -euo pipefail

: "${GH_TOKEN:?GH_TOKEN is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${SOURCE_SHA:?SOURCE_SHA is required}"

REQUIRED_BRANCH="${REQUIRED_BRANCH:-main}"
CI_WORKFLOW_FILE="${CI_WORKFLOW_FILE:-ci.yml}"
REQUIRE_BRANCH_HEAD="${REQUIRE_BRANCH_HEAD:-false}"
EXPECTED_CI_RUN_ID="${EXPECTED_CI_RUN_ID:-}"

if [[ ! "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "SOURCE_SHA must be a full 40-character lowercase Git SHA" >&2
  exit 1
fi

branch_sha=$(gh api \
  "repos/${GITHUB_REPOSITORY}/branches/${REQUIRED_BRANCH}" \
  --jq '.commit.sha')

if [[ "$REQUIRE_BRANCH_HEAD" == "true" ]]; then
  if [[ "$SOURCE_SHA" != "$branch_sha" ]]; then
    echo "Release SHA $SOURCE_SHA is not the current $REQUIRED_BRANCH head $branch_sha" >&2
    exit 1
  fi
else
  comparison=$(gh api \
    "repos/${GITHUB_REPOSITORY}/compare/${SOURCE_SHA}...${branch_sha}")
  comparison_status=$(jq -r '.status // empty' <<<"$comparison")
  if [[ "$comparison_status" != "ahead" && "$comparison_status" != "identical" ]]; then
    echo "Release SHA $SOURCE_SHA is not on ${REQUIRED_BRANCH}" >&2
    exit 1
  fi
fi

ci_runs=$(gh api --method GET \
  "repos/${GITHUB_REPOSITORY}/actions/workflows/${CI_WORKFLOW_FILE}/runs" \
  -f branch="$REQUIRED_BRANCH" \
  -f head_sha="$SOURCE_SHA" \
  -f status=success \
  -f per_page=20)

run_filter='select(
  .head_sha == $sha and
  .head_branch == $branch and
  .event == "push" and
  .conclusion == "success" and
  .head_repository.full_name == $repository
)'
if [[ -n "$EXPECTED_CI_RUN_ID" ]]; then
  if ! jq -e \
    --arg sha "$SOURCE_SHA" \
    --arg branch "$REQUIRED_BRANCH" \
    --arg repository "$GITHUB_REPOSITORY" \
    --argjson run_id "$EXPECTED_CI_RUN_ID" \
    ".workflow_runs[] | $run_filter | select(.id == \$run_id)" \
    >/dev/null <<<"$ci_runs"; then
    echo "The triggering run is not a successful CI push run for $SOURCE_SHA on $REQUIRED_BRANCH" >&2
    exit 1
  fi
elif ! jq -e \
  --arg sha "$SOURCE_SHA" \
  --arg branch "$REQUIRED_BRANCH" \
  --arg repository "$GITHUB_REPOSITORY" \
  ".workflow_runs[] | $run_filter" \
  >/dev/null <<<"$ci_runs"; then
  echo "No successful CI push run exists for $SOURCE_SHA on $REQUIRED_BRANCH" >&2
  exit 1
fi

associated_prs=$(gh api \
  -H "Accept: application/vnd.github+json" \
  "repos/${GITHUB_REPOSITORY}/commits/${SOURCE_SHA}/pulls")
mapfile -t merged_prs < <(jq -r \
  --arg branch "$REQUIRED_BRANCH" \
  '.[] | select(.merged_at != null and .base.ref == $branch) | .number' \
  <<<"$associated_prs")

if (( ${#merged_prs[@]} == 0 )); then
  echo "Release SHA $SOURCE_SHA has no associated merged pull request into $REQUIRED_BRANCH" >&2
  exit 1
fi

owner=${GITHUB_REPOSITORY%%/*}
repository=${GITHUB_REPOSITORY#*/}
approved_pr=""
for pr_number in "${merged_prs[@]}"; do
  review_decision=$(gh api graphql \
    -f owner="$owner" \
    -f repository="$repository" \
    -F number="$pr_number" \
    -f query='query($owner:String!,$repository:String!,$number:Int!){repository(owner:$owner,name:$repository){pullRequest(number:$number){reviewDecision}}}' \
    --jq '.data.repository.pullRequest.reviewDecision // ""')
  if [[ "$review_decision" == "APPROVED" ]]; then
    approved_pr="$pr_number"
    break
  fi
done

if [[ -z "$approved_pr" ]]; then
  echo "Release SHA $SOURCE_SHA is not associated with an approved merged pull request" >&2
  exit 1
fi

echo "Release gate passed for $SOURCE_SHA (CI success and approved PR #$approved_pr on $REQUIRED_BRANCH)"
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  echo "source_sha=$SOURCE_SHA" >> "$GITHUB_OUTPUT"
  echo "approved_pr=$approved_pr" >> "$GITHUB_OUTPUT"
fi
