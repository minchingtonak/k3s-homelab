#!/usr/bin/env bash
#
# Builds the librarian CronJob image and pushes it to the Forgejo container
# registry. Follows scripts/build-azerothcore-images.sh conventions: dated
# tag for humans, the manifests pin by digest, PUSH=0 keeps it local.
#
# Run from the workstation. Build context is librarian/ only — the
# Dockerfile (docker/librarian/Dockerfile) lives outside the context and is
# passed with -f, so nothing outside the tool tree reaches the daemon.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

REGISTRY="${LIBRARIAN_IMAGE_REGISTRY:-forgejo.item.fyi}"
OWNER="${LIBRARIAN_IMAGE_OWNER:-akmin}"
TAG="${LIBRARIAN_IMAGE_TAG:-$(date -u +%Y%m%d)}"
PUSH="${LIBRARIAN_IMAGE_PUSH:-1}"

DOCKERFILE="docker/librarian/Dockerfile"
CONTEXT="librarian"
IMAGE="${REGISTRY}/${OWNER}/librarian:${TAG}"

echo "Building ${IMAGE}"
docker build -f "${DOCKERFILE}" -t "${IMAGE}" "${CONTEXT}"

if [ "${PUSH}" = "1" ]; then
    docker push "${IMAGE}"
    echo
    echo "Digest for the manifest (pin with this):"
    docker inspect --format '{{index .RepoDigests 0}}' "${IMAGE}" \
        | sed 's/.*@/librarian@/' || true
    docker images --digests | grep "akmin/librarian" | head -2 || true
else
    echo "LIBRARIAN_IMAGE_PUSH=0 — kept local: ${IMAGE}"
fi
