#!/usr/bin/env bash
# slot-workflow.sh — end-to-end slot reservation smoke test
#
# Demonstrates the full lifecycle:
#   1. Check fleet capabilities
#   2. Reserve a dev slot
#   3. Build and push a container image to the lab registry
#   4. Run the container on the assigned host via DOCKER_HOST=ssh://
#   5. Validate the HTTP /health endpoint
#   6. Stop the container and release the slot
#
# Usage:
#   ./examples/slot-workflow.sh [MONITOR_URL]
#
# Prerequisites:
#   - SSH key loaded for the dev host (ssh-add)
#   - Docker daemon running locally (for build/push)
#   - Dev host running Colima with the lab registry in insecure-registries
#     (see specs/slot-workflow-example-v0.1.md §3.2)
#   - Monitor server reachable at MONITOR_URL

set -euo pipefail

MONITOR="${1:-http://192.168.1.129:8888}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CALLER="slot-workflow-smoke"
LABEL="$(date +%Y%m%d-%H%M%S)"
TTL=300   # 5 minutes — enough for the test, auto-expires if script aborts

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; RESET='\033[0m'

log()  { echo -e "${CYAN}==>${RESET} $*"; }
ok()   { echo -e "${GREEN}OK${RESET}  $*"; }
fail() { echo -e "${RED}FAIL${RESET} $*" >&2; exit 1; }
warn() { echo -e "${YELLOW}WARN${RESET} $*"; }

json_field() { python3 -c "import json,sys; d=json.load(sys.stdin); print(d$1)"; }

# Track slot ID for cleanup on unexpected exit
SLOT_ID=""
CONTAINER_NAME=""
OLD_DOCKER_HOST="${DOCKER_HOST:-}"

cleanup() {
    if [[ -n "$CONTAINER_NAME" && -n "${SSH_USER:-}" && -n "${HOST_ADDR:-}" ]]; then
        warn "Cleaning up container $CONTAINER_NAME on $HOST_ADDR..."
        DOCKER_HOST="ssh://${SSH_USER}@${HOST_ADDR}" \
            docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
    fi
    if [[ -n "$SLOT_ID" ]]; then
        warn "Releasing slot $SLOT_ID..."
        /usr/bin/curl -sf -X DELETE "$MONITOR/api/slots/$SLOT_ID" >/dev/null 2>&1 || true
    fi
    if [[ -n "$OLD_DOCKER_HOST" ]]; then
        export DOCKER_HOST="$OLD_DOCKER_HOST"
    else
        unset DOCKER_HOST 2>/dev/null || true
    fi
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# 1. Check fleet capabilities
# ---------------------------------------------------------------------------
log "Checking fleet capabilities at $MONITOR..."
CAPS=$(/usr/bin/curl -sf --max-time 10 "$MONITOR/api/capabilities") \
    || fail "Cannot reach monitor at $MONITOR"

REGISTRY_URL=$(echo "$CAPS" | python3 -c "
import json,sys
d = json.load(sys.stdin)
r = d.get('registry')
if r and r.get('status') == 'online':
    print(r['url'])
else:
    print('')
")

AVAIL=$(echo "$CAPS" | python3 -c "
import json,sys
d = json.load(sys.stdin)
total = sum(h.get('free_slots',0) for h in d.get('hosts',[]) if h.get('docker_enabled'))
print(total)
")

echo "  Registry : ${REGISTRY_URL:-not configured or offline}"
echo "  Free slots: $AVAIL"
[[ "$AVAIL" -gt 0 ]] || fail "No free slots available. Check /api/capabilities."

# ---------------------------------------------------------------------------
# 2. Reserve a slot
# ---------------------------------------------------------------------------
log "Reserving slot (caller=$CALLER, ttl=${TTL}s)..."
SLOT=$(/usr/bin/curl -sf -X POST "$MONITOR/api/slots" \
    -H "Content-Type: application/json" \
    -d "{\"caller\": \"$CALLER\", \"label\": \"$LABEL\", \"ttl_seconds\": $TTL}") \
    || fail "POST /api/slots failed"

SLOT_ID=$(echo "$SLOT"        | json_field "['id']")
HOST=$(echo "$SLOT"           | json_field "['host']")
HOST_ADDR=$(echo "$SLOT"      | json_field "['host_address']")
SSH_USER=$(echo "$SLOT"       | json_field "['ssh_user'] or ''")
COMPOSE_PROJECT=$(echo "$SLOT"| json_field "['compose_project']")
PORT=$(echo "$SLOT"           | json_field "['port_range_start']")
SLOT_REGISTRY=$(echo "$SLOT"  | json_field "['registry_url'] or ''")

echo "  Slot ID  : $SLOT_ID"
echo "  Host     : $HOST ($HOST_ADDR)"
echo "  SSH user : $SSH_USER"
echo "  Project  : $COMPOSE_PROJECT"
echo "  Port     : $PORT"
echo "  Registry : ${SLOT_REGISTRY:-none}"
ok "Slot reserved"

[[ -n "$SSH_USER" ]] || fail "No ssh_user returned for $HOST. Add ssh_user to config.yml for this host."

# ---------------------------------------------------------------------------
# 3. Build and push the smoke-test image
# ---------------------------------------------------------------------------
IMAGE_TAG="smoke-test:${SLOT_ID}"

if [[ -n "$SLOT_REGISTRY" ]]; then
    FULL_IMAGE="${SLOT_REGISTRY}/${IMAGE_TAG}"
    log "Building smoke-test image → $FULL_IMAGE ..."
    docker build -q -t "$FULL_IMAGE" "$SCRIPT_DIR/smoke-test" \
        || fail "docker build failed"
    ok "Image built"

    log "Pushing $FULL_IMAGE ..."
    docker push "$FULL_IMAGE" \
        || fail "docker push failed — is $SLOT_REGISTRY in your local insecure-registries?"
    ok "Image pushed to registry"
else
    warn "No registry available — using local image (must already exist on $HOST)"
    FULL_IMAGE="smoke-test:${SLOT_ID}"
    docker build -q -t "$FULL_IMAGE" "$SCRIPT_DIR/smoke-test" \
        || fail "docker build failed"
fi

# ---------------------------------------------------------------------------
# 4. Run on the dev host via DOCKER_HOST=ssh://
# ---------------------------------------------------------------------------
CONTAINER_NAME="$COMPOSE_PROJECT"
log "Starting container on $HOST via DOCKER_HOST=ssh://${SSH_USER}@${HOST_ADDR} ..."

export DOCKER_HOST="ssh://${SSH_USER}@${HOST_ADDR}"
docker run -d \
    --name "$CONTAINER_NAME" \
    -p "${PORT}:8080" \
    -e "HLAB_SLOT_ID=${SLOT_ID}" \
    "$FULL_IMAGE" \
    || fail "docker run failed on $HOST"
ok "Container $CONTAINER_NAME running on $HOST:$PORT"

# ---------------------------------------------------------------------------
# 5. Validate the HTTP endpoint
# ---------------------------------------------------------------------------
log "Waiting for service to be ready..."
MAX_TRIES=15
for i in $(seq 1 $MAX_TRIES); do
    if RESP=$(/usr/bin/curl -sf --max-time 3 "http://${HOST_ADDR}:${PORT}/health" 2>/dev/null); then
        break
    fi
    [[ $i -lt $MAX_TRIES ]] || fail "Service did not become ready after ${MAX_TRIES}s"
    sleep 1
done

STATUS=$(echo "$RESP" | json_field "['status']")
[[ "$STATUS" == "ok" ]] || fail "Unexpected status: $STATUS"
echo "  Response : $RESP"
ok "HTTP /health returned status=ok"

# ---------------------------------------------------------------------------
# 6. Tear down container and release slot
# ---------------------------------------------------------------------------
log "Stopping container $CONTAINER_NAME ..."
docker rm -f "$CONTAINER_NAME" >/dev/null
CONTAINER_NAME=""   # prevent double-cleanup in trap
unset DOCKER_HOST 2>/dev/null || true

log "Releasing slot $SLOT_ID ..."
/usr/bin/curl -sf -X DELETE "$MONITOR/api/slots/$SLOT_ID" >/dev/null
SLOT_ID=""   # prevent double-release in trap

echo ""
ok "Smoke test passed — reserve → build → push → run → validate → release"
