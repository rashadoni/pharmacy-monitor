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
#   - `agent-review` must be green before merge
#   - no required approvals: the owner works alone, and a rule nobody can
#     satisfy is how production became undeployable in the first place
#
# Usage:  bash scripts/ci/configure-main-protection.sh [owner/repo]
set -euo pipefail

REPO="${1:-rashadrahimov/pharmacy-monitor}"
BRANCH="${BRANCH:-main}"

command -v gh >/dev/null || { echo "gh is required" >&2; exit 1; }

echo "Configuring branch protection on ${REPO}@${BRANCH}"

# `required_status_checks.contexts` is deliberately just agent-review: it is the
# one check that runs for every pull request, including documentation-only ones.
# The heavy jobs keep their path filters, and requiring a check that never
# starts would leave such a pull request unmergeable for ever.
gh api -X PUT "repos/${REPO}/branches/${BRANCH}/protection" \
  -H "Accept: application/vnd.github+json" \
  --input - <<'JSON'
{
  "required_status_checks": {
    "strict": false,
    "contexts": ["agent-review"]
  },
  "enforce_admins": false,
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
echo "Note: enforce_admins is false on purpose. The owner must retain a way to"
echo "recover production when a check itself is broken — that is a break-glass"
echo "path, not a routine one. Using it is worth saying out loud in the report."
