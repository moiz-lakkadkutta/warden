#!/usr/bin/env bash
# Build (and optionally push) warden/fabric-base for linux/amd64 (Sandboxes are x86_64).
#   ops/build-base.sh [FABRIC_REPO] [--push]
# FABRIC_REPO defaults to https://github.com/moiz-lakkadkutta/Fabric-Inventory (origin/main).
set -euo pipefail
repo="${1:-https://github.com/moiz-lakkadkutta/Fabric-Inventory}"
push="${2:-}"
registry="${WARDEN_REGISTRY:-ghcr.io/moiz-lakkadkutta}"
here="$(cd "$(dirname "$0")/.." && pwd)"
ctx="$(mktemp -d)"
trap 'rm -rf "$ctx"' EXIT

git clone --quiet "$repo" "$ctx/src"
sha="$(git -C "$ctx/src" rev-parse --short HEAD)"
mkdir -p "$ctx/ops" && cp "$here/ops/entrypoint.sh" "$here/ops/shmshim.c" "$ctx/ops/"
echo "fabric @ $sha" >&2

docker buildx build --platform linux/amd64 --load \
  -t "fabric-dev:$sha" -f "$ctx/src/backend/Dockerfile.dev" "$ctx/src/backend"

image="$registry/warden/fabric-base"
docker buildx build --platform linux/amd64 --load \
  --build-arg DEV_IMAGE="fabric-dev:$sha" --build-arg FABRIC_SHA="$sha" \
  -t "$image:$sha" -t "$image:latest" -f "$here/ops/Dockerfile.base" "$ctx"

if [ "$push" = "--push" ]; then
  docker push "$image:$sha"
  docker push "$image:latest"
fi
echo "$image:$sha"
