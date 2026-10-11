#!/usr/bin/env bash
# Layer 1 of docs/DELIVERY-ARCHITECTURE.md, as a script rather than a promise.
#
# The agent review of the pull request that introduced this architecture made
# the point that killed the previous design: a replacement control that is only
# described is not a control. So the enforcement lives here, is version
# controlled, is re-runnable, and is what every other repository copies.
#
# What it configures on `main`:
#   - changes arrive only through a pull request: `required_pull_request_reviews`
#     is set, with zero required approvals — the owner works alone, and a rule
#     nobody can satisfy is how production became undeployable in the first
#     place
#   - the three CI jobs must be green before merge, and only GitHub Actions
#     (app 15368) can report them
#   - the rules apply to administrators too (`enforce_admins`): nobody merges a
#     red pull request. Owner's decision, confirmed 2026-10-11
#   - no force push, no branch deletion
#
# Changed 2026-10-04. This script used to pin `agent-review` as the single
# required context. That check reports green on every pull request when its
# API key is missing, so for as long as it was the only required one the gate
# was decorative: tests, lint and the frontend build did not affect merging at
# all. The workflow is deleted and must not come back — see Layer 2 of
# docs/DELIVERY-ARCHITECTURE.md.
#
# Changed 2026-10-11. Until then the payload said
# `"required_pull_request_reviews": null`, while this header promised "only
# through a pull request". GitHub's API description reads null as "disable",
# and the PUT below replaces the whole protection object: a re-run would have
# removed the pull-request requirement that is set on GitHub, and a commit with
# green checks could then have been pushed straight to `main`. (Read from the
# API description. Never tried.) The closing report would not have shown it: it
# printed `pull_request_only` from the status-check field, so it said `true`
# whatever the pull-request field held. Now the payload carries the
# requirement, and the report is a comparison of what GitHub returns with the
# payload — the script exits non-zero when they differ.
#
# NOT VERIFIED BY A RUN, as of 2026-10-11: the write below has not been sent to
# GitHub in this form — branch protection is not changed for the sake of a
# test. What was checked instead: the payload against the live protection, by
# reading it, and the script against a stand-in `gh` that never reaches the
# network (tests/test_main_protection_script.py).
#
# Before a real run:
#   - it is the owner's call, not an agent's (docs/DELIVERY-ARCHITECTURE.md)
#   - run it from an up-to-date `main` only: branches and worktrees cut before
#     2026-10-11 still carry the copy that sends null
#   - run `--check` first and read what differs: the write replaces every
#     setting, including one somebody changed on purpose
#
# Usage:
#   bash scripts/ci/configure-main-protection.sh --check [owner/repo]
#       read-only: compares the live protection with the payload, sends nothing
#   bash scripts/ci/configure-main-protection.sh [owner/repo]
#       writes the payload, unless the live protection already equals it
set -euo pipefail

usage() {
  echo "Usage: bash scripts/ci/configure-main-protection.sh [--check] [owner/repo]" >&2
  exit 2
}

# Anything this script does not recognise stops it before the first request: a
# mistyped or misplaced `--check` must not turn into a write.
MODE=apply
if [[ "${1:-}" == "--check" ]]; then
  MODE=check
  shift
fi
[[ $# -le 1 ]] || usage
[[ "${1:-}" != -* ]] || usage

# The repository moved to the rashadoni account; the old default would have
# configured protection on a repository nobody uses any more.
REPO="${1:-rashadoni/pharmacy-monitor}"
BRANCH="${BRANCH:-main}"
ENDPOINT="repos/${REPO}/branches/${BRANCH}/protection"

command -v gh >/dev/null || { echo "gh is required" >&2; exit 1; }

# The whole protection object. PUT replaces it: a setting left out here is
# reset to GitHub's default, not kept — which is why every setting the endpoint
# takes is spelled out, and why `required_pull_request_reviews` must never be
# null again.
#
# The three checks are the jobs of ci-pipeline.yml. That workflow has NO path
# filters: it runs on every pull request to main, documentation-only ones
# included, so none of them can hang waiting for a check that never starts.
# Keep that property — a path-filtered required check is unmergeable for ever.
# Context strings must match the job `name:` values exactly. 15368 is GitHub
# Actions: a status of the same name posted by anything else does not count.
#
# `checks` replaces the `contexts` list this script sent before. `contexts` is
# deprecated and leaves the choice of app to GitHub ("recently set by…");
# `checks` names it, which is what GitHub reports for this branch today. Like
# the rest of this write, the `checks`-only form has not been sent from here,
# and GitHub's published API schema still marks `contexts` as required. If
# GitHub refuses the write for that reason, the refusal is of the whole write
# and nothing changes: say so, do not patch around it silently.
DESIRED="$(cat <<'JSON'
{
  "required_status_checks": {
    "strict": false,
    "checks": [
      {"context": "Lint (ruff)", "app_id": 15368},
      {"context": "Test (pytest)", "app_id": 15368},
      {"context": "Frontend (Next.js)", "app_id": 15368}
    ]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": false,
    "require_code_owner_reviews": false,
    "require_last_push_approval": false,
    "required_approving_review_count": 0
  },
  "restrictions": null,
  "required_linear_history": false,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "block_creations": false,
  "required_conversation_resolution": false,
  "lock_branch": false,
  "allow_fork_syncing": false
}
JSON
)"

# Turns GitHub's answer into the shape of the payload and compares the two,
# setting by setting. It runs inside `gh api --jq`, with the payload bound to
# $want (JSON is valid jq). The last line it prints is the verdict; everything
# before it is for the reader. A setting GitHub does not return counts as a
# difference, not as "off".
COMPARE="$(cat <<'JQ'
| ($want | .required_status_checks.checks |= sort_by(.context)) as $want
| {
    required_status_checks: (
      .required_status_checks
      | if . == null then null
        else {strict, checks: ((.checks // []) | map({context, app_id}) | sort_by(.context))}
        end
    ),
    enforce_admins: .enforce_admins.enabled,
    required_pull_request_reviews: (
      .required_pull_request_reviews | if . == null then null else del(.url) end
    ),
    restrictions: .restrictions,
    required_linear_history: .required_linear_history.enabled,
    allow_force_pushes: .allow_force_pushes.enabled,
    allow_deletions: .allow_deletions.enabled,
    block_creations: .block_creations.enabled,
    required_conversation_resolution: .required_conversation_resolution.enabled,
    lock_branch: .lock_branch.enabled,
    allow_fork_syncing: .allow_fork_syncing.enabled
  } as $live
| [$want | keys[] | select($want[.] != $live[.])] as $differs
| "pull request required  : \($live.required_pull_request_reviews != null)",
  "required approvals     : \($live.required_pull_request_reviews.required_approving_review_count)",
  "required checks        : \([$live.required_status_checks.checks // [] | .[] | .context] | join(", "))",
  "applies to admins      : \($live.enforce_admins)",
  "force pushes allowed   : \($live.allow_force_pushes)",
  "branch deletion allowed: \($live.allow_deletions)",
  ($differs[] | "DIFFERS \(.): this script sets \($want[.] | tojson), GitHub has \($live[.] | tojson)"),
  (if ($differs | length) == 0 then "RESULT: match" else "RESULT: MISMATCH" end)
JQ
)"

read_state() {
  gh api "${ENDPOINT}" -H "Accept: application/vnd.github+json" \
    --jq "(${DESIRED}) as \$want ${COMPARE}"
}

in_sync() {
  [[ "${1##*$'\n'}" == "RESULT: match" ]]
}

if [[ "${MODE}" == check ]]; then
  echo "Comparing ${REPO}@${BRANCH} with this script. Nothing is sent."
  state="$(read_state)" || {
    echo "Could not read the protection of ${REPO}@${BRANCH}: see the error above." >&2
    exit 1
  }
  printf '%s\n' "${state}"
  in_sync "${state}" || exit 1
  exit 0
fi

if state="$(read_state)"; then
  if in_sync "${state}"; then
    printf '%s\n' "${state}"
    echo "${REPO}@${BRANCH} is already protected the way this script describes. Nothing sent."
    exit 0
  fi
  echo "Protection of ${REPO}@${BRANCH} before the change:"
  printf '%s\n' "${state}"
else
  echo "No readable protection on ${REPO}@${BRANCH} (see the error above)."
fi

echo
echo "Configuring branch protection on ${REPO}@${BRANCH}"
response="$(gh api -X PUT "${ENDPOINT}" -H "Accept: application/vnd.github+json" \
  --input - <<<"${DESIRED}")" || {
  printf '%s\n' "${response}" >&2
  echo "GitHub refused the write. Nothing in this script was applied." >&2
  exit 1
}

echo "Sent. Read back from GitHub:"
state="$(read_state)" || {
  echo "The write was accepted, but the protection could not be read back." >&2
  echo "Treat ${REPO}@${BRANCH} as unverified until --check passes." >&2
  exit 1
}
printf '%s\n' "${state}"
if ! in_sync "${state}"; then
  echo >&2
  echo "GitHub accepted the write, but ${REPO}@${BRANCH} is NOT in the state this" >&2
  echo "script describes: see the DIFFERS lines above. Do not report it as protected." >&2
  exit 1
fi

echo
echo "Note: the rules apply to administrators too, so a red pull request cannot"
echo "be merged by anyone. If a check itself breaks — so that even the pull"
echo "request fixing it cannot pass — enforce_admins is switched off by the owner,"
echo "or by an agent on the owner's direct word, for that one merge, and switched"
echo "back on at once. Say so out loud in the report."
