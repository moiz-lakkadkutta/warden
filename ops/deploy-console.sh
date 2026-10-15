#!/usr/bin/env bash
# E3: build the read-only run console (console/Dockerfile) → push to Nebius Container Registry →
# run it as a Nebius Serverless AI endpoint on a small CPU VM. Idempotent: re-running reuses the
# registry and the endpoint (so the public URL stays the same) and only rolls the image.
#
#   ops/deploy-console.sh            build, push, create-or-restart, wait, smoke-test
#   ops/deploy-console.sh --status   print the endpoint's state and URL
#   ops/deploy-console.sh --down     delete the endpoint (the registry and images are kept)
#
# Needs: docker (buildx) — or, with no Docker daemon, crane + uv — the nebius CLI logged in (`nebius iam whoami`), jq, curl.
# Overrides (shell variables for this script only; Warden itself never reads them):
#   PARENT_ID   project id      (default: the nebius profile's parent-id)
#   NAME        endpoint name   (default: warden-console)
#   REGISTRY    registry name   (default: warden)
#   PLATFORM / PRESET / DISK    (default: cpu-d3 / 2vcpu-8gb / 32Gi; the cheapest CPU shape)
set -euo pipefail

NAME="${NAME:-warden-console}"
REGISTRY="${REGISTRY:-warden}"
PLATFORM="${PLATFORM:-cpu-d3}"
PRESET="${PRESET:-2vcpu-8gb}"
DISK="${DISK:-32Gi}"
IMAGE_NAME="warden-console"
PORT=8080
CRANE="${CRANE:-crane}"
cd "$(dirname "$0")/.."

log() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
die() { log "error: $*"; exit 1; }
for tool in docker nebius jq curl; do
  command -v "$tool" >/dev/null || die "$tool not found"
done
nebius iam whoami >/dev/null 2>&1 || die "nebius CLI not logged in → run: nebius profile create"
PARENT_ID="${PARENT_ID:-$(nebius config get parent-id 2>/dev/null || true)}"
[ -n "$PARENT_ID" ] || die "no project id → PARENT_ID=project-… $0  (or: nebius config set parent-id …)"

endpoint_json() {  # the endpoint as JSON, or nothing when it does not exist
  nebius ai endpoint get-by-name --parent-id "$PARENT_ID" --name "$NAME" --format json 2>/dev/null || true
}

public_url() {
  jq -r '[.status.public_endpoints[]? | select(startswith("https://"))][0] // empty'
}

case "${1:-}" in
  --status)
    ep="$(endpoint_json)"
    [ -n "$ep" ] || { log "no endpoint $NAME"; exit 1; }
    jq -r '"\(.metadata.id)  \(.status.state)  \(.spec.image)"' <<<"$ep"
    public_url <<<"$ep"
    exit 0 ;;
  --down)
    ep="$(endpoint_json)"
    [ -n "$ep" ] || { log "no endpoint $NAME"; exit 0; }
    id="$(jq -r .metadata.id <<<"$ep")"
    log "deleting endpoint $id"
    nebius ai endpoint delete --id "$id"
    exit 0 ;;
  "") ;;
  *) die "unknown option $1 (use --status or --down)" ;;
esac

t0=$(date +%s)

# 1. Registry (same project → the endpoint pulls without credentials).
reg="$(nebius registry list --parent-id "$PARENT_ID" --format json \
  | jq -c --arg n "$REGISTRY" '[.items[]? | select(.metadata.name == $n)][0] // empty')"
if [ -z "$reg" ]; then
  log "creating registry $REGISTRY"
  nebius registry create --parent-id "$PARENT_ID" --name "$REGISTRY" >/dev/null
  reg="$(nebius registry list --parent-id "$PARENT_ID" --format json \
    | jq -c --arg n "$REGISTRY" '[.items[]? | select(.metadata.name == $n)][0]')"
fi
fqdn="$(jq -r .status.registry_fqdn <<<"$reg")"
reg_path="$(jq -r .metadata.id <<<"$reg" | cut -d- -f2)"
repo="$fqdn/$reg_path/$IMAGE_NAME"

sha="$(git rev-parse --short HEAD)"
git diff --quiet HEAD -- console site || sha="$sha-dirty"
log "image $repo:$sha"

# 2. Build + push. A throwaway DOCKER_CONFIG avoids the macOS Keychain helper (`desktop`), which
#    can hang; the nebius credential helper handles cr.*.nebius.cloud.
work="$(mktemp -d)"
DOCKER_CONFIG="$work/docker"
export DOCKER_CONFIG
trap 'rm -rf "$work"' EXIT
mkdir -p "$DOCKER_CONFIG"
for d in cli-plugins contexts buildx; do
  [ -e "$HOME/.docker/$d" ] && ln -s "$HOME/.docker/$d" "$DOCKER_CONFIG/$d"
done
ctx="$(jq -r '.currentContext // empty' "$HOME/.docker/config.json" 2>/dev/null || true)"
jq -n --arg h "$fqdn" --arg c "$ctx" \
  '{credHelpers: {($h): "nebius"}} + (if $c == "" then {} else {currentContext: $c} end)' \
  >"$DOCKER_CONFIG/config.json"

# Keep these in step with console/Dockerfile.
BASE="python:3.12-slim"
DEPS=("fastapi==0.141.1" "uvicorn==0.53.0")
CMD="python,-m,uvicorn,console.app:app,--host,0.0.0.0,--port,$PORT,--proxy-headers,--forwarded-allow-ips,*,--no-server-header"

if docker info >/dev/null 2>&1; then
  log "building with docker"
  docker buildx build --platform linux/amd64 -f console/Dockerfile --build-arg "BUILD=$sha" \
    -t "$repo:$sha" -t "$repo:live" --load .
  docker push -q "$repo:$sha"
  docker push -q "$repo:live"
else
  # No Docker daemon (e.g. Docker Desktop owned by another macOS user): assemble the same image
  # without one. One layer = console/ + site/ + linux/amd64 wheels, on top of the base image.
  command -v "$CRANE" >/dev/null || die "no docker daemon and no crane → brew install crane"
  command -v uv >/dev/null || die "no docker daemon and no uv"
  log "no docker daemon; assembling with crane"
  root="$work/root"
  mkdir -p "$root/app/console"
  cp console/__init__.py console/__main__.py console/app.py "$root/app/console/"
  echo "$sha" >"$root/app/console/BUILD"
  cp -R site "$root/app/site"
  uv pip install -q --target "$root/usr/local/lib/python3.12/site-packages" \
    --python-platform x86_64-manylinux_2_28 --python-version 3.12 --only-binary :all: "${DEPS[@]}"
  rm -rf "$root/usr/local/lib/python3.12/site-packages/bin"
  chmod -R u=rwX,go=rX "$root"
  COPYFILE_DISABLE=1 tar --uid 0 --gid 0 --uname root --gname root --no-xattrs \
    -cf "$work/layer.tar" -C "$root" app usr
  "$CRANE" mutate "$BASE" --platform linux/amd64 --append "$work/layer.tar" \
    --workdir /app --user 10001 --exposed-ports "$PORT" --cmd "$CMD" \
    -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONUNBUFFERED=1 \
    -l "org.opencontainers.image.source=https://github.com/moiz-lakkadkutta/warden" \
    -t "$repo:$sha" >/dev/null
  "$CRANE" tag "$repo:$sha" live
fi
t_pushed=$(date +%s)

# 3. Endpoint: create once; afterwards restart it so it re-pulls :live and keeps its URL.
ep="$(endpoint_json)"
if [ -z "$ep" ]; then
  log "creating endpoint $NAME ($PLATFORM $PRESET)"
  nebius ai endpoint create --parent-id "$PARENT_ID" --name "$NAME" \
    --image "$repo:live" --container-port "$PORT" \
    --platform "$PLATFORM" --preset "$PRESET" --disk-size "$DISK" --auth none >/dev/null
else
  id="$(jq -r .metadata.id <<<"$ep")"
  log "restarting endpoint $id to pull $repo:live"
  nebius ai endpoint restart --id "$id" >/dev/null
fi

# 4. Wait until it serves this build.
url=""
for _ in $(seq 1 90); do
  ep="$(endpoint_json)"
  url="$(public_url <<<"$ep")"
  if [ -n "$url" ] && curl -fsS --max-time 5 "$url/healthz" 2>/dev/null | grep -q "\"build\":\"$sha\""; then
    break
  fi
  sleep 10
done
t_live=$(date +%s)
[ -n "$url" ] || die "endpoint has no https URL yet: $0 --status"
curl -fsS --max-time 5 "$url/healthz" | grep -q "\"build\":\"$sha\"" \
  || die "$url is not serving $sha yet (state: $(jq -r .status.state <<<"$ep")); check: nebius ai logs $(jq -r .metadata.id <<<"$ep")"
curl -fsS --max-time 10 "$url/" | grep -q "warden console" || die "$url/ does not render the console"

log "live: build+push $((t_pushed - t0))s, endpoint ready $((t_live - t_pushed))s, total $((t_live - t0))s"
echo "$url"
