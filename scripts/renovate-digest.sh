#!/usr/bin/env bash
#
# Gathers the deterministic half of the weekly Renovate digest.
#
# Run by the Hermes agent's `renovate-digest` cron job. Everything printed here
# is injected into the agent's prompt; the agent then does the judgement half
# (reading release notes, classifying risk) per docs/renovate-digest.md, which
# this script prints so the procedure stays versioned and reviewable rather than
# living in an unreviewed prompt on the agent host.
#
# The split matters: enumerating pull requests, diffing them against last week
# and computing ages are all things a shell does exactly right every time and a
# model does approximately. Only what needs judgement reaches the model.
#
# State lives outside the repo. Each run records the pull requests it reported
# so the next one only has to describe what actually changed; without it, week
# two re-reports the same twenty pull requests and the digest stops being read.
# Set RENOVATE_DIGEST_NO_STATE=1 to dry-run without consuming the delta.
#
# Reads the repo but never touches its working tree: the clone is shared with
# interactive agent sessions and may sit on a branch mid-change, so this fetches
# and reads blobs out of origin/main instead of switching or pulling.

set -euo pipefail

REPO_SLUG="${RENOVATE_DIGEST_REPO:-minchingtonak/k3s-homelab}"
REPO_DIR="${RENOVATE_DIGEST_REPO_DIR:-/workspace/k3s-homelab}"
STATE_FILE="${RENOVATE_DIGEST_STATE:-${HERMES_HOME:-${HOME}/.hermes}/state/renovate-digest.json}"
PROCEDURE="${RENOVATE_DIGEST_PROCEDURE:-docs/renovate-digest.md}"

# Renovate embeds full release notes in the pull request body, which is most of
# the research done for free — but twenty of them at full length is a wall of
# changelog. New pull requests get their body up to this many characters;
# carried-over ones get a single line.
BODY_LIMIT="${RENOVATE_DIGEST_BODY_LIMIT:-12000}"

# How many new pull requests get a full body in one run. There are routinely
# more open than are worth putting in a single prompt — 35 full bodies is around
# 400KB — so the rest are listed by title and left out of the state file, which
# brings them back with bodies on a later run rather than losing them.
# Security-labelled first, then oldest.
MAX_BODIES="${RENOVATE_DIGEST_MAX_BODIES:-12}"

die() {
  printf 'renovate-digest: %s\n' "$*" >&2
  exit 1
}

for tool in gh jq git; do
  command -v "${tool}" >/dev/null 2>&1 || die "${tool} not found on PATH"
done

[ -d "${REPO_DIR}/.git" ] || die "no git repo at ${REPO_DIR}"

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

# Fetch rather than pull. Failing here is worth stopping for: a stale
# origin/main means printing last week's procedure as though it were current.
git -C "${REPO_DIR}" fetch --quiet origin main \
  || die "git fetch origin main failed"

# Read the procedure out of origin/main, not the working tree, so what the
# agent follows is always what was reviewed and merged. RENOVATE_DIGEST_LOCAL=1
# reads the working copy instead — for testing an edit before it lands, and
# deliberately not the default, since falling back to it silently is how the
# agent ends up following an uncommitted draft.
if [ "${RENOVATE_DIGEST_LOCAL:-0}" = "1" ]; then
  cp "${REPO_DIR}/${PROCEDURE}" "${WORK}/procedure.md" \
    || die "could not read ${REPO_DIR}/${PROCEDURE}"
else
  git -C "${REPO_DIR}" show "origin/main:${PROCEDURE}" >"${WORK}/procedure.md" \
    || die "could not read ${PROCEDURE} from origin/main"
fi

gh pr list -R "${REPO_SLUG}" --author app/renovate --state open --limit 100 \
  --json number,title,url,createdAt,labels,body >"${WORK}/open.json" \
  || die "gh pr list failed"

if [ -f "${STATE_FILE}" ] && jq -e . "${STATE_FILE}" >/dev/null 2>&1; then
  cp "${STATE_FILE}" "${WORK}/state.json"
else
  printf '{"prs":{}}' >"${WORK}/state.json"
fi

# Keyed on title, not on updatedAt. A Renovate pull request keeps its number
# when a newer upstream version lands and rewrites the title, so a title change
# is exactly "this is a different upgrade now" — while updatedAt also moves for
# comments and rebases, which would resurface unchanged entries every week.
jq -n \
  --slurpfile open "${WORK}/open.json" \
  --slurpfile state "${WORK}/state.json" \
  --arg body_limit "${BODY_LIMIT}" \
  --arg max_bodies "${MAX_BODIES}" \
  --rawfile procedure "${WORK}/procedure.md" \
  --arg procedure_path "${PROCEDURE}" \
  --arg repo "${REPO_SLUG}" '
  ($open[0]) as $prs
  | ($state[0].prs // {}) as $seen
  | ($body_limit | tonumber) as $limit
  | ($max_bodies | tonumber) as $maxb
  | (now | todate) as $ts
  | def age: ((now - (.createdAt | fromdateiso8601)) / 86400 | floor);
    def clip: if (. | length) > $limit
      then (.[0:$limit] + "\n\n[…body truncated at \($limit) characters]")
      else . end;
    # Renovate appends a Configuration block of schedule/rebase boilerplate to
    # every body. It is identical across pull requests and carries no signal.
    def trim_footer: split("\n---\n\n### Configuration")[0];
    def is_security: (.labels | map(.name) | index("security")) != null;

  # Keyed on title, not on updatedAt. A Renovate pull request keeps its number
  # when a newer upstream version lands and rewrites the title, so a title
  # change is exactly "this is a different upgrade now" — while updatedAt also
  # moves for comments and rebases, resurfacing unchanged entries every week.
  ($prs | map(select($seen[.number | tostring] != .title))
        | sort_by([(if is_security then 0 else 1 end), .createdAt])) as $fresh
  | ($prs | map(select($seen[.number | tostring] == .title))) as $carried
  | ($fresh[0:$maxb]) as $included
  | ($fresh[$maxb:]) as $deferred
  | ($deferred | map(.number | tostring)) as $deferred_ids
  | ($seen | keys | map(select(. as $n | ($prs | map(.number | tostring) | index($n)) == null))) as $gone

  | {
      report: ([
        "===== RENOVATE DIGEST INPUT =====",
        "generated: \($ts)",
        "repo: \($repo)",
        "open renovate pull requests: \($prs | length)",
        (if ($seen | length) == 0
          then "state: first run — every pull request below is new"
          else "state: \($seen | length) tracked from the previous digest" end),
        "",
        "--- PROCEDURE (\($procedure_path) @ origin/main) ---",
        $procedure,
        "",
        "--- CLOSED OR MERGED SINCE THE LAST DIGEST (\($gone | length)) ---",
        (if ($gone | length) == 0 then "(none)"
          else [$gone[] | "#\(.)  \($seen[.])"] end),
        "",
        "--- NEW OR CHANGED SINCE THE LAST DIGEST (\($fresh | length)) ---",
        (if ($fresh | length) == 0 then "(none — report this plainly, do not pad)"
          else [$included[] | [
            "<<<PR \(.number)>>>",
            "title: \(.title)",
            "url: \(.url)",
            "age: \(age)d",
            "labels: \(if (.labels | length) == 0 then "none" else (.labels | map(.name) | join(", ")) end)",
            "body:",
            (.body | trim_footer | clip),
            "<<<END PR \(.number)>>>",
            ""]] end),
        "",
        # Deferred entries are NOT recorded in state, so they arrive with full
        # bodies on a later run. The backlog drains instead of being silently
        # swallowed — but the agent must say they went unreviewed, hence
        # printing them rather than dropping them.
        "--- NEW, BUT DEFERRED TO A LATER RUN FOR PROMPT BUDGET (\($deferred | length)) ---",
        (if ($deferred | length) == 0 then "(none)"
          else [
            "These were NOT reviewed this run. Report the count and say so.",
            ($deferred[] | "#\(.number)  \(age)d  \(.title)")] end),
        "",
        "--- CARRIED OVER, UNCHANGED SINCE THE LAST DIGEST (\($carried | length)) ---",
        (if ($carried | length) == 0 then "(none)"
          else [$carried[] | "#\(.number)  \(age)d  \(.title)"] end),
        "",
        "===== END INPUT ====="
      ] | flatten | join("\n")),

      state: {
        generated_at: $ts,
        prs: ($prs
          | map(select(. as $pr | ($deferred_ids | index($pr.number | tostring)) == null))
          | map({key: (.number | tostring), value: .title})
          | from_entries)
      }
    }
  ' >"${WORK}/result.json"

jq -r '.report' "${WORK}/result.json"

if [ "${RENOVATE_DIGEST_NO_STATE:-0}" = "1" ]; then
  printf '\n[state not written: RENOVATE_DIGEST_NO_STATE=1]\n'
  exit 0
fi

mkdir -p "$(dirname "${STATE_FILE}")"
jq '.state' "${WORK}/result.json" >"${WORK}/next-state.json"
mv "${WORK}/next-state.json" "${STATE_FILE}"
