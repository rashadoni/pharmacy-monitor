#!/usr/bin/env bash
# Layer 1 of docs/DELIVERY-ARCHITECTURE.md, as a script rather than a promise.
#
# The agent review of the pull request that introduced this architecture made
# the point that killed the previous design: a replacement control that is only
# described is not a control. So the enforcement lives here, is version
# controlled, is re-runnable, and is what every other repository copies.
#
# What it configures on `main`:
#   - changes arrive only through a pull request (no direct push, no force push,
#     no branch deletion)
#   - the three CI jobs must be green before merge
#   - no required approvals: the owner works alone, and a rule nobody can
#     satisfy is how production became undeployable in the first place
#
# Changed 2026-10-04. This script used to pin `agent-review` as the single
# required context. That check reports green on every pull request when its
# API key is missing, so for as long as it was the only required one the gate
# was decorative: tests, lint and the frontend build did not affect merging at
# all. The workflow is deleted and must not come back — see Layer 2 of
# docs/DELIVERY-ARCHITECTURE.md.
#
# Usage:  bash scripts/ci/configure-main-protection.sh [owner/repo]
set -euo pipefail

# The repository moved to the rashadoni account; the old default would have
# configured protection on a repository nobody uses any more.
REPO="${1:-rashadoni/pharmacy-monitor}"
BRANCH="${BRANCH:-main}"

command -v gh >/dev/null || { echo "gh is required" >&2; exit 1; }

echo "Configuring branch protection on ${REPO}@${BRANCH}"

# These three are the jobs of ci-pipeline.yml. That workflow has NO path
# filters: it runs on every pull request to main, documentation-only ones
# included, so none of them can hang waiting for a check that never starts.
# Keep that property — a path-filtered required check is unmergeable for ever.
# Context strings must match the job `name:` values exactly.
gh api -X PUT "repos/${REPO}/branches/${BRANCH}/protection" \
  -H "Accept: application/vnd.github+json" \
  --input - <<'JSON'
{
  "required_status_checks": {
    "strict": false,
    "contexts": ["Lint (ruff)", "Test (pytest)", "Frontend (Next.js)"]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_linear_history": false,
  "required_conversation_resolution": false,
  "block_creations": false
}
JSON

echo
echo "Applied. Current state:"
gh api "repos/${REPO}/branches/${BRANCH}/protection" \
  --jq '{
    pull_request_only: (.required_status_checks != null),
    required_checks: .required_status_checks.contexts,
    force_pushes: .allow_force_pushes.enabled,
    deletions: .allow_deletions.enabled
  }'

echo
echo "Note: enforce_admins is true here, matching the live state as of"
echo "2026-10-04 — the rules apply to the owner too, so a red pull request"
echo "cannot be merged by anyone. If a check itself breaks and production has"
echo "to be recovered, turn it off deliberately for that one merge and turn it"
echo "back on; using that path is worth saying out loud in the report."
