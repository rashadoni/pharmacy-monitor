#!/usr/bin/env bash
# Layer 1 of docs/DELIVERY-ARCHITECTURE.md, as a script rather than a promise.
#
# The agent review of the pull request that introduced this architecture made
# the point that killed the previous design: a replacement control that is only
# described is not a control. So the enforcement lives here, is version
# controlled, is re-runnable, and is what every other repository copies.
#
# What it describes for `main`:
#   - changes arrive only through a pull request: `required_pull_request_reviews`
#     is set, with zero required approvals — the owner works alone, and a rule
#     nobody can satisfy is how production became undeployable in the first
#     place
#   - the three CI jobs must be green before merge, reported by GitHub Actions
#     (app 15368)
#   - the rules apply to administrators too (`enforce_admins`): nobody merges a
#     red pull request. Owner's decision, confirmed 2026-10-11
#   - no force push, no branch deletion
# That these settings behave as named — a direct push is refused, a status of
# the same name from another source does not count — is GitHub's description
# of them. Nobody here tried.
#
# Usage:
#   bash scripts/ci/configure-main-protection.sh --check [owner/repo]
#       read-only: compares the live protection with the payload below and
#       sends nothing.
#   bash scripts/ci/configure-main-protection.sh --apply [owner/repo]
#       writes the payload — only after a comparison that ended in "MISMATCH",
#       or on a branch GitHub reports as unprotected. Then reads the result
#       back.
# Exit codes, both modes: 0 = GitHub has what is written here (and nothing was
# sent, or the write was read back and equals the payload); 1 = it has
# something else; 2 = arguments not understood, nothing requested; 3 = could
# not tell — a read or the write failed.
# Without a flag it is `--check`. Always type `--check` anyway: the copies of
# this file from before this change (see below) do not know flags and WRITE
# when run with none.
#
# Changed 2026-10-04. This script used to pin `agent-review` as the single
# required context. That check reports green on every pull request when its
# API key is missing, so for as long as it was the only required one the gate
# was decorative: tests, lint and the frontend build did not affect merging at
# all. The workflow is deleted and must not come back — see Layer 2 of
# docs/DELIVERY-ARCHITECTURE.md.
#
# Changed 2026-10-11. Before this change the payload said
# `"required_pull_request_reviews": null`, while this header promised "only
# through a pull request". GitHub's API description reads null as "disable",
# and the PUT replaces the whole protection object: a run would have removed
# the pull-request requirement that is set on GitHub, and a commit with green
# checks could then have been pushed straight to `main`. (Read from the API
# description. Never tried. Every version that was on `main` before this
# change sent null, so the requirement that is on GitHub was not put there by
# this script.) The closing report would not have shown it: it printed
# `pull_request_only` from the status-check field, so it said `true` whatever
# the pull-request field held. Now the payload carries the requirement, the
# report is a comparison of what GitHub returns with the payload, and writing
# takes an explicit flag.
#
# NOT VERIFIED BY A RUN, as of 2026-10-11: `--apply` has never sent its write
# to GitHub — branch protection is not changed for the sake of a test. What was
# checked instead: the payload against the live protection, by reading it; the
# comparison filter in gh's own jq, on that same read; and the script against a
# stand-in `gh` that never reaches the network
# (tests/test_main_protection_script.py). After the first real write, rewrite
# this paragraph and the two that say the same: Layer 1 of
# docs/DELIVERY-ARCHITECTURE.md and the delivery section of CLAUDE.md.
#
# Before `--apply`:
#   - it is the owner's call, not an agent's (docs/DELIVERY-ARCHITECTURE.md)
#   - use the file from `origin/main` after a `git fetch`, not a local `main`
#     and not another branch. Every branch and worktree cut before this change
#     was merged carries the old file, whatever its date. The old file is the
#     one without `--apply` in it:
#       grep -c -- '--apply' scripts/ci/configure-main-protection.sh   # 0 = old
#   - run `--check` first and read what differs: the write replaces every
#     setting below, including one somebody changed on purpose — a fourth
#     required check added by hand is removed by it
set -euo pipefail
# Character ranges in the patterns below mean ASCII, whatever the caller's locale.
export LC_ALL=C

usage() {
  echo "Usage: bash scripts/ci/configure-main-protection.sh --check|--apply [owner/repo]" >&2
  exit 2
}

# Writing is never the default, and an argument that is not exactly a flag or
# an owner/repo pair stops the script before the first request.
MODE=check
case "${1:-}" in
  --check) shift ;;
  --apply) MODE=apply; shift ;;
esac
[[ $# -le 1 ]] || usage

# The repository moved to the rashadoni account; the old default would have
# configured protection on a repository nobody uses any more.
REPO="rashadoni/pharmacy-monitor"
[[ $# -eq 0 ]] || REPO="$1"
REPO_SHAPE='^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+$'
[[ "${REPO}" =~ ${REPO_SHAPE} ]] || usage
[[ "${REPO##*/}" != "." && "${REPO##*/}" != ".." ]] || usage

# Not taken from the environment: a BRANCH exported for something else must not
# point a protection write at another branch.
BRANCH="main"
ENDPOINT="repos/${REPO}/branches/${BRANCH}/protection"

command -v gh >/dev/null || { echo "gh is required" >&2; exit 3; }

# The whole protection object. PUT replaces it. The API description gives every
# optional setting a default, so one left out is expected to fall back to that
# default rather than keep its value (inferred from the description, not stated
# there, not tried) — which is why every top-level setting the endpoint takes
# is spelled out, and why `required_pull_request_reviews` must never be null
# again. Inside it, `dismissal_restrictions` and
# `bypass_pull_request_allowances` are left out on purpose: nobody is to bypass
# the pull-request requirement. That leaving them out clears an allowance
# already set is assumed too; the comparison shows one that survived.
#
# The three checks are jobs of ci-pipeline.yml. That workflow has NO path
# filters: it runs on every pull request to main, documentation-only ones
# included, so none of them can hang waiting for a check that never starts.
# Keep that property — a path-filtered required check is unmergeable for ever.
# Context strings must match the job `name:` values exactly. 15368 is GitHub
# Actions.
#
# `checks` replaces the `contexts` list this script sent before. `contexts` is
# deprecated and leaves the choice of app to GitHub ("recently set by…");
# `checks` names it, which is what GitHub reports for this branch today. Like
# the rest of the write, the `checks`-only form has not been sent from here,
# and GitHub's published API schema still marks `contexts` as required. A
# refusal for that reason is expected to be a refusal of the whole write: the
# script then reads the protection back and says what is set. Do not patch
# around it silently.
IFS= read -r -d '' DESIRED <<'JSON' || true
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

# Turns GitHub's answer into the shape of the payload and compares the two,
# setting by setting. It runs inside `gh api --jq`, with the payload bound to
# $want (JSON is valid jq). The last line it prints is the verdict; everything
# before it is for the reader, and is taken from GitHub's answer, not from the
# payload. Only the two exact verdict lines count: output that ends in anything
# else is "could not tell", never "different".
#
# What "match" means: every setting this script writes has, on GitHub, the
# value written here. A setting GitHub does not return counts as a difference,
# not as "off". What it does not look at: anything this endpoint does not
# write — commit signatures, repository rulesets.
IFS= read -r -d '' COMPARE <<'JQ' || true
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

NL=$'\n'

# Reads the protection and sets STATE (what to show) and VERDICT:
#   match | mismatch   the comparison ran and said so
#   unprotected        GitHub answered that the branch has no protection
#   unknown            anything else: an error, a refusal, an answer this
#                      script cannot read. Never a reason to write.
# "Unprotected" is GitHub's 404 body, which gh prints on stdout: a JSON object
# carrying that message. The comparison's own output never starts with a brace.
compare() {
  VERDICT=unknown
  if STATE="$(gh api "${ENDPOINT}" -H "Accept: application/vnd.github+json" \
    --jq "(${DESIRED}) as \$want ${COMPARE}")"; then
    case "${STATE##*"${NL}"}" in
      "RESULT: match") VERDICT=match ;;
      "RESULT: MISMATCH") VERDICT=mismatch ;;
    esac
  elif [[ "${STATE}" == "{"* && "${STATE}" == *'"Branch not protected"'* ]]; then
    VERDICT=unprotected
  fi
}

show() {
  case "${VERDICT}" in
    match | mismatch) printf '%s\n' "${STATE}" ;;
    unprotected) echo "${REPO}@${BRANCH} has no branch protection at all." ;;
    *)
      [[ -z "${STATE}" ]] || printf '%s\n' "${STATE}" >&2
      echo "Could not read the protection of ${REPO}@${BRANCH}: see above." >&2
      ;;
  esac
}

echo "Comparing ${REPO}@${BRANCH} with this script."
compare
show
case "${VERDICT}" in
  match)
    echo "Nothing to change, nothing sent."
    exit 0
    ;;
  mismatch | unprotected) ;;
  *)
    # Writing over a state nobody has seen is exactly what this script is for
    # preventing.
    echo "Nothing was sent." >&2
    exit 3
    ;;
esac

if [[ "${MODE}" != apply ]]; then
  echo "Nothing was sent. Writing is --apply, and it is the owner's call."
  exit 1
fi

echo
echo "Writing branch protection to ${REPO}@${BRANCH}"
if ! response="$(gh api -X PUT "${ENDPOINT}" -H "Accept: application/vnd.github+json" \
  --input - <<<"${DESIRED}")"; then
  printf '%s\n' "${response}" >&2
  echo "The write request ended in an error: see above. What GitHub has now:" >&2
  compare
  show >&2
  exit 3
fi

echo "Sent. Read back from GitHub:"
compare
show
case "${VERDICT}" in
  match) ;;
  mismatch | unprotected)
    echo >&2
    echo "GitHub accepted the write, but ${REPO}@${BRANCH} is NOT in the state this" >&2
    echo "script describes: see above. Do not report it as protected." >&2
    exit 1
    ;;
  *)
    echo "The write was accepted, but the protection could not be read back." >&2
    echo "Treat ${REPO}@${BRANCH} as unverified until --check passes." >&2
    exit 3
    ;;
esac

echo
echo "Note: the rules apply to administrators too, so a red pull request cannot"
echo "be merged by anyone. If a check itself breaks — so that even the pull"
echo "request fixing it cannot pass — enforce_admins is switched off by the owner,"
echo "or by an agent on the owner's direct word, for that one merge, and switched"
echo "back on at once. Say so out loud in the report."
