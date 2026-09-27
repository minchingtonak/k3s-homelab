#!/usr/bin/env bash
# Bidirectional sync between the windmill-homelab git repository and the
# Windmill workspace, run every 5 minutes by the windmill-sync CronJob
# (k8s/apps/windmill/sync-cronjob.yaml).
#
# `wmill sync pull` mirrors remote -> local INCLUDING deleting local-only
# files, so a naive pull-then-push job eats repo-first additions before the
# push can deploy them (observed 2026-09-27: a "[WM]" drift commit reverted
# an entire feature commit). The job is therefore MODE-AWARE, picking one
# direction per run:
#
#   deploy  - human commits pending on main (HEAD != last "[WM]" commit):
#             `wmill sync push` makes the workspace match the repository.
#             Merged PRs deploy themselves; git deletions prune the
#             workspace. Afterwards an empty "[WM] deployed <sha>" marker
#             commit flips the next run back to capture mode.
#   capture - no human commits pending: `wmill sync pull` mirrors the
#             workspace into git, committing UI edits (and UI deletions)
#             as "[WM] workspace drift sync" commits.
#
# Policy: when a human commit and UI edits race, the repo wins - concurrent
# UI edits are overwritten by deploy mode. Don't edit the same item in both
# places inside one 5-minute window. An empty-content repo is never pushed
# (bootstrap falls back to capture mode so the pull can seed it).
#
# Secret variables are never synced (skipSecrets in wmill.yaml); they live
# only in Windmill and must be re-entered by hand after a workspace rebuild.
set -euo pipefail

export HOME=/work
export PATH="/deps/node_modules/.bin:$PATH"

# The node image ships git and openssh-client; the Windmill CLI is installed
# at runtime into an emptyDir (the container filesystem is read-only). The
# CLI version is derived from the server's own /api/version so the pin can
# never drift when Renovate bumps the windmill-server image; an unreachable
# server fails the job before any sync.
SERVER_VERSION=$(curl -fsS "$WMILL_BASE_URL/api/version" | sed 's/^.*v//')
npm install --prefix /deps --no-audit --no-fund "windmill-cli@${SERVER_VERSION}"

# Secret volume files are named after their keys (/ssh/SSH_PRIVATE_KEY) and
# are group-readable; ssh insists on an owner-only key.
install -m 600 /ssh/SSH_PRIVATE_KEY "$HOME/id_ed25519"
export GIT_SSH_COMMAND="ssh -i $HOME/id_ed25519 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$HOME/.ssh_known_hosts"

git config --global user.name "windmill-sync"
git config --global user.email "windmill-sync@minicluster.internal"

git clone --quiet "ssh://git@github.com/${SYNC_REPO}.git" "$HOME/repo"
cd "$HOME/repo"

if [ ! -f wmill.yaml ]; then
  echo "ERROR: ${SYNC_REPO} has no wmill.yaml at the repo root." >&2
  echo "       It defines the sync scope; see README.md in that repository." >&2
  exit 1
fi

LAST_WM=$(git log -1 --format=%H --author='windmill-sync' --)
HEAD_SHA=$(git rev-parse HEAD)

has_content() { [ -f settings.yaml ] || [ -d f ] || [ -d u ]; }

WM_ARGS=(--base-url "$WMILL_BASE_URL" --token "$WMILL_TOKEN" --workspace homelab)

if [ "$HEAD_SHA" != "$LAST_WM" ] && has_content; then
  echo "==> deploy mode: human commits pending, pushing repo state to the workspace"
  wmill sync push --yes "${WM_ARGS[@]}"

  # Marker commit records the deployment and flips the next run to capture
  # mode. Rebase first in case a merge landed mid-run.
  git fetch --quiet origin
  git rebase origin/main
  git commit --allow-empty --quiet -m "[WM] deployed $(git rev-parse --short HEAD)"
  git push --quiet origin main
  echo "==> deployment marker pushed"
else
  echo "==> capture mode: capturing workspace drift into git"
  wmill sync pull --yes "${WM_ARGS[@]}"

  # Fail-safe: a healthy pull always leaves synced content behind (clone or
  # pull). If none exists, the pull failed silently and something is wrong.
  has_content || { echo "ERROR: no workspace content present after pull." >&2; exit 1; }

  if [ -n "$(git status --porcelain)" ]; then
    git add -A
    git commit --quiet -m "[WM] workspace drift sync $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    # A merge may have landed between clone and commit; drift rebases on top.
    # Conflicts here fail the job on purpose (see header).
    git fetch --quiet origin
    git rebase origin/main
    git push --quiet origin main
    echo "==> drift committed and pushed"
  else
    echo "==> no workspace drift"
  fi
fi
