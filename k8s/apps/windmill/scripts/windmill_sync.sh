#!/usr/bin/env bash
# Bidirectional sync between the windmill-homelab git repository and the
# Windmill workspace, run every 5 minutes by the windmill-sync CronJob
# (k8s/apps/windmill/sync-cronjob.yaml).
#
# Leg 1 - capture drift (Windmill -> git): a fresh clone is filled from the
#   workspace with `wmill sync pull`; any difference is committed as a
#   "[WM] ..." commit and pushed. Running this BEFORE the deploy leg is what
#   keeps UI edits from being silently clobbered by the push below.
#   --yes is NOT optional: without a TTY the pull confirmation prompt reads
#   EOF, applies nothing, and exits 0 - which once let the push below mirror
#   an empty repo into the workspace (2026-09-27 incident, recovered).
# Leg 2 - deploy (git -> Windmill): `wmill sync push` applies main to the
#   workspace, so merged PRs deploy themselves and git deletions prune the
#   workspace. Guarded: the push is skipped unless the pull left real
#   content behind, so a silent pull failure can never empty the workspace.
#
# Secret variables are never synced (skipSecrets in wmill.yaml); they live
# only in Windmill and must be re-entered by hand after a workspace rebuild.
#
# Races with a simultaneous merge are expected to fail the job: the next run
# self-heals, except a rebase conflict over the same file (a UI edit racing a
# PR that touches it), which needs manual resolution once.
set -euo pipefail

export HOME=/work
export PATH="/deps/node_modules/.bin:$PATH"

# The CLI must match the deployed server version. The server reports its own
# version at /api/version, so the pin can never drift when Renovate bumps the
# windmill-server image; an unreachable server fails the job before any sync.
SERVER_VERSION=$(curl -fsS "$WMILL_BASE_URL/api/version" | sed 's/^.*v//')
npm install --prefix /deps --no-audit --no-fund "windmill-cli@${SERVER_VERSION}"

# Secret volume files are group-readable; ssh insists on an owner-only key.
install -m 600 /ssh/id_ed25519 "$HOME/id_ed25519"
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

echo "==> leg 1: capturing workspace drift into git"
wmill sync pull --yes --base-url "$WMILL_BASE_URL" --token "$WMILL_TOKEN" --workspace homelab

# Fail-safe: a healthy pull always leaves synced content behind (clone or
# pull). If none exists, the pull failed silently and pushing now would
# mirror an empty repo into the workspace.
if [ ! -f settings.yaml ] && [ ! -d f ] && [ ! -d u ]; then
  echo "ERROR: no workspace content present after pull; refusing to push." >&2
  exit 1
fi

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

echo "==> leg 2: deploying repository state to the workspace"
wmill sync push --yes --base-url "$WMILL_BASE_URL" --token "$WMILL_TOKEN" --workspace homelab
