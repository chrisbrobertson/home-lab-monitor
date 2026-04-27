#!/usr/bin/env bash
# deploy-server.sh — deploy the monitor server to the Mac Mini
#
# Runs local tests, copies all server files to the Mac Mini, restarts the
# service, and verifies the server is healthy before exiting.
#
# Usage: ./scripts/deploy-server.sh
#
# Environment variables:
#   REMOTE_HOST   SSH target          (default: crobertson@192.168.1.129)
#   REMOTE_DIR    Deploy root         (default: /opt/hlab)
#   SERVER_PORT   Monitor server port (default: 8888)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

REMOTE_HOST="${REMOTE_HOST:-crobertson@192.168.1.129}"
REMOTE_DIR="${REMOTE_DIR:-/opt/hlab}"
SERVER_PORT="${SERVER_PORT:-8888}"
REMOTE_IP="${REMOTE_HOST##*@}"

FAILS=0

_ok()   { echo "[ OK ] $*"; }
_inst() { echo "[INST] $*"; }
_fail() { echo "[FAIL] $*"; FAILS=$((FAILS + 1)); }
_info() { echo "[INFO] $*"; }

echo "==> Home Lab Monitor — Server Deploy"
echo "    target: ${REMOTE_HOST}  dir: ${REMOTE_DIR}  port: ${SERVER_PORT}"
echo ""

# ── Step 1: Local tests ────────────────────────────────────────────────────
echo "── Step 1: Local tests ────────────────────────────────────────────────────"
_inst "Running test suite..."
if python3 -m pytest "${REPO_ROOT}/tests/" -q 2>&1; then
    _ok "All tests passed"
else
    _fail "Tests failed — aborting deploy"
    exit 1
fi

# ── Step 2: SSH connectivity ───────────────────────────────────────────────
echo ""
echo "── Step 2: SSH connectivity ───────────────────────────────────────────────"
if ssh -o ConnectTimeout=5 -o BatchMode=yes "${REMOTE_HOST}" "echo ok" &>/dev/null; then
    _ok "SSH to ${REMOTE_HOST}"
else
    _fail "Cannot reach ${REMOTE_HOST} — is the Mac Mini on and SSH enabled?"
    exit 1
fi

# ── Step 3: Copy files ─────────────────────────────────────────────────────
echo ""
echo "── Step 3: Copy files ─────────────────────────────────────────────────────"
_inst "Copying server modules..."
scp -q \
    "${REPO_ROOT}/server/server.py" \
    "${REPO_ROOT}/server/db.py" \
    "${REPO_ROOT}/server/config.py" \
    "${REPO_ROOT}/server/placement.py" \
    "${REPO_ROOT}/server/registry.py" \
    "${REMOTE_HOST}:${REMOTE_DIR}/server/" \
    && _ok "server/*.py deployed" \
    || { _fail "scp server modules failed"; exit 1; }

_inst "Copying dashboard..."
scp -q \
    "${REPO_ROOT}/server/static/index.html" \
    "${REMOTE_HOST}:${REMOTE_DIR}/server/static/index.html" \
    && _ok "static/index.html deployed" \
    || { _fail "scp index.html failed"; exit 1; }

_inst "Copying config and requirements..."
scp -q \
    "${REPO_ROOT}/config.yml" \
    "${REPO_ROOT}/requirements.txt" \
    "${REMOTE_HOST}:${REMOTE_DIR}/" \
    && _ok "config.yml + requirements.txt deployed" \
    || { _fail "scp config failed"; exit 1; }

# ── Step 4: Install dependencies ──────────────────────────────────────────
echo ""
echo "── Step 4: Dependencies ───────────────────────────────────────────────────"
_inst "Installing Python deps on remote..."
if ssh "${REMOTE_HOST}" \
    "/usr/bin/python3 -m pip install -q -r ${REMOTE_DIR}/requirements.txt 2>&1 | grep -v WARNING"; then
    _ok "Python deps up to date"
else
    _fail "pip install failed"
fi

# ── Step 5: Restart service ────────────────────────────────────────────────
echo ""
echo "── Step 5: Restart service ────────────────────────────────────────────────"
_inst "Restarting com.homelab.monitor.server..."
ssh "${REMOTE_HOST}" "
    launchctl bootout gui/\$(id -u)/com.homelab.monitor.server 2>/dev/null || true
    sleep 2
    launchctl bootstrap gui/\$(id -u) \$HOME/Library/LaunchAgents/com.homelab.monitor.server.plist
" 2>&1
sleep 5

PID=$(ssh "${REMOTE_HOST}" \
    "launchctl list com.homelab.monitor.server 2>/dev/null | grep '\"PID\"' | awk '{print \$3}' | tr -d ';'" 2>/dev/null || true)
if [[ -n "$PID" ]]; then
    _ok "Service running (PID ${PID})"
else
    _fail "Service did not start — check: ssh ${REMOTE_HOST} 'tail -20 ${REMOTE_DIR}/logs/server.err'"
    exit 1
fi

# ── Step 6: Verify ─────────────────────────────────────────────────────────
echo ""
echo "── Step 6: Verification ───────────────────────────────────────────────────"

# /api/summary
SUMMARY=$(curl -s --max-time 10 "http://${REMOTE_IP}:${SERVER_PORT}/api/summary" 2>/dev/null || true)
if echo "$SUMMARY" | python3 -c "import json,sys; d=json.load(sys.stdin); assert len(d) > 0" &>/dev/null; then
    HOST_COUNT=$(echo "$SUMMARY" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))")
    _ok "/api/summary → ${HOST_COUNT} hosts"
else
    _fail "/api/summary did not return valid JSON"
fi

# /api/capabilities
CAPS=$(curl -s --max-time 10 "http://${REMOTE_IP}:${SERVER_PORT}/api/capabilities" 2>/dev/null || true)
if echo "$CAPS" | python3 -c "import json,sys; d=json.load(sys.stdin); assert 'hosts' in d and 'slot_policy' in d" &>/dev/null; then
    FREE=$(echo "$CAPS" | python3 -c "import json,sys; d=json.load(sys.stdin); print(sum(h['free_slots'] for h in d['hosts']))")
    _ok "/api/capabilities → ${FREE} free slots across fleet"
else
    _fail "/api/capabilities did not return expected shape"
fi

# Dashboard HTML
DASH=$(curl -s --max-time 10 "http://${REMOTE_IP}:${SERVER_PORT}/" 2>/dev/null || true)
if echo "$DASH" | grep -q "switchTab"; then
    _ok "Dashboard HTML serving correctly"
else
    _fail "Dashboard is not returning expected HTML"
fi

# ── Summary ────────────────────────────────────────────────────────────────
echo ""
echo "────────────────────────────────────────────────────────────────────────────"
if [[ "$FAILS" -eq 0 ]]; then
    echo "[ OK ] Deploy complete — http://${REMOTE_IP}:${SERVER_PORT}"
    exit 0
else
    echo "[FAIL] ${FAILS} check(s) failed"
    exit 1
fi
