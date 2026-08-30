#!/usr/bin/env bash
#
# Builds the AzerothCore worldserver and db-import images with server-side
# modules compiled in, and pushes them to the Forgejo container registry.
#
# Run it from the workstation, not a cluster node: a full AzerothCore compile
# wants ~16 cores and several GB of RAM, and the minicluster nodes have 15Gi
# each with the realm already living on one of them.
#
# The two images are built from a single Dockerfile and therefore a single
# source tree. That is deliberate — see the header of docker/azerothcore/Dockerfile
# for why db-import cannot stay on the upstream image once a module ships SQL.
#
# Both are built before either is pushed. A half-published pair is the one
# outcome worth avoiding here: Flux would happily reconcile a worldserver whose
# db-import never applied the matching schema.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

REGISTRY="${WOW_IMAGE_REGISTRY:-forgejo.item.fyi}"
OWNER="${WOW_IMAGE_OWNER:-akmin}"

# Dated rather than semantic: AzerothCore cuts no releases, so there is no
# upstream version to inherit. The manifests pin by digest anyway, which is what
# actually makes a deploy reproducible — this tag is for humans reading the
# registry listing.
TAG="${WOW_IMAGE_TAG:-$(date -u +%Y%m%d)}"

# Set to 0 to build and keep the images local, e.g. to test a Dockerfile change
# without publishing it.
PUSH="${WOW_IMAGE_PUSH:-1}"

# Source refs. Defaults track master; pass commit SHAs for a reproducible build.
AC_REF="${WOW_AC_REF:-master}"
MOD_TRANSMOG_REF="${WOW_MOD_TRANSMOG_REF:-master}"
MOD_AUTOBALANCE_REF="${WOW_MOD_AUTOBALANCE_REF:-master}"
MOD_AOE_LOOT_REF="${WOW_MOD_AOE_LOOT_REF:-master}"
MOD_AH_BOT_REF="${WOW_MOD_AH_BOT_REF:-master}"

DOCKERFILE="docker/azerothcore/Dockerfile"
# The Dockerfile clones its own sources, so the context only has to carry the
# Dockerfile itself. Pointing it at the repo root would ship the whole tree —
# including decrypted secrets, if any happen to be sitting in the worktree.
CONTEXT="docker/azerothcore"

# stage:image-name
TARGETS=(
  "worldserver:ac-wotlk-worldserver"
  "db-import:ac-wotlk-db-import"
)

echo "registry : ${REGISTRY}/${OWNER}"
echo "tag      : ${TAG}"
echo "sources  : azerothcore-wotlk@${AC_REF}"
echo "           mod-transmog@${MOD_TRANSMOG_REF}"
echo "           mod-autobalance@${MOD_AUTOBALANCE_REF}"
echo "           mod-aoe-loot@${MOD_AOE_LOOT_REF}"
echo "           mod-ah-bot@${MOD_AH_BOT_REF}"
echo

for entry in "${TARGETS[@]}"; do
  target="${entry%%:*}"
  name="${entry#*:}"
  ref="${REGISTRY}/${OWNER}/${name}:${TAG}"

  echo "==> building ${ref} (target ${target})"
  docker build \
    --file "$DOCKERFILE" \
    --target "$target" \
    --tag "$ref" \
    --build-arg "AC_REF=${AC_REF}" \
    --build-arg "MOD_TRANSMOG_REF=${MOD_TRANSMOG_REF}" \
    --build-arg "MOD_AUTOBALANCE_REF=${MOD_AUTOBALANCE_REF}" \
    --build-arg "MOD_AOE_LOOT_REF=${MOD_AOE_LOOT_REF}" \
    --build-arg "MOD_AH_BOT_REF=${MOD_AH_BOT_REF}" \
    "$CONTEXT"
  echo
done

if [[ "$PUSH" != "1" ]]; then
  echo "WOW_IMAGE_PUSH=${PUSH}, skipping push. Built:"
  for entry in "${TARGETS[@]}"; do
    echo "  ${REGISTRY}/${OWNER}/${entry#*:}:${TAG}"
  done
  exit 0
fi

for entry in "${TARGETS[@]}"; do
  name="${entry#*:}"
  ref="${REGISTRY}/${OWNER}/${name}:${TAG}"

  echo "==> pushing ${ref}"
  # Not caught and retried on purpose: the usual failure is an expired login,
  # and `docker login ${REGISTRY}` is something a human has to do.
  docker push "$ref"
  echo
done

echo "Pin these in k8s/apps/wow/worldserver.yaml:"
for entry in "${TARGETS[@]}"; do
  name="${entry#*:}"
  repo="${REGISTRY}/${OWNER}/${name}"
  digest="$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' \
    "${repo}:${TAG}" | grep "^${repo}@" | head -n1)"
  echo "  ${repo}:${TAG}@${digest#*@}"
done
