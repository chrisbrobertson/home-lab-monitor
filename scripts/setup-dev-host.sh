#!/usr/bin/env bash
# setup-dev-host.sh — idempotent macOS dev slot host bootstrap
# See specs/dev-host-setup-v0.1.md for the full specification and idempotency contract.
#
# Usage: ./scripts/setup-dev-host.sh
# When run by an agent over SSH, set REPO_ROOT explicitly.
#
# Environment variables (all have defaults):
#   HLAB_DIR        Deploy directory          (default: /opt/hlab)
#   AGENT_PORT      Agent listen port         (default: 9100)
#   REGISTRY        Lab registry (insecure)   (default: 192.168.1.93:5000)
#   COLIMA_CPU      Colima VM CPU count       (default: 4)
#   COLIMA_MEMORY   Colima VM memory in GB    (default: 8)
#   COLIMA_DISK     Colima VM disk in GB      (default: 60)
#   REPO_ROOT       Path to repo root         (default: parent of scripts/)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT:-$(dirname "$SCRIPT_DIR")}"
HLAB_DIR="${HLAB_DIR:-/opt/hlab}"
AGENT_PORT="${AGENT_PORT:-9100}"
REGISTRY="${REGISTRY:-192.168.1.93:5000}"
COLIMA_CPU="${COLIMA_CPU:-4}"
COLIMA_MEMORY="${COLIMA_MEMORY:-8}"
COLIMA_DISK="${COLIMA_DISK:-60}"

PLIST_LABEL="com.homelab.monitor.agent"
PLIST_DEST="/Library/LaunchAgents/${PLIST_LABEL}.plist"
PLIST_SRC="${REPO_ROOT}/launchd/${PLIST_LABEL}.plist"
AGENT_SRC="${REPO_ROOT}/agent/agent.py"
AGENT_DEST="${HLAB_DIR}/agent/agent.py"
AGENT_CONFIG="${HLAB_DIR}/agent-config.yml"
COLIMA_YAML="${HOME}/.colima/default/colima.yaml"

FAILS=0

# ── Output helpers ──────────────────────────────────────────────────────────
_skip() { echo "[SKIP] $*"; }
_inst() { echo "[INST] $*"; }
_ok()   { echo "[ OK ] $*"; }
_warn() { echo "[WARN] $*"; }
_fail() { echo "[FAIL] $*"; FAILS=$((FAILS + 1)); }

_sha256() {
    /usr/bin/python3 -c \
        "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" \
        "$1"
}

echo "==> Home Lab Monitor — Dev Host Setup"
echo "    HLAB_DIR=${HLAB_DIR}  AGENT_PORT=${AGENT_PORT}  REGISTRY=${REGISTRY}"
echo "    COLIMA: cpu=${COLIMA_CPU}  memory=${COLIMA_MEMORY}GB  disk=${COLIMA_DISK}GB"
echo "    REPO_ROOT=${REPO_ROOT}"
echo ""

# ── Ensure Homebrew is findable on Apple Silicon and Intel ─────────────────
if [[ -x /opt/homebrew/bin/brew ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
elif [[ -x /usr/local/bin/brew ]]; then
    eval "$(/usr/local/bin/brew shellenv)"
fi

# ══════════════════════════════════════════════════════════════════════════════
# Step 1: Prerequisites
# ══════════════════════════════════════════════════════════════════════════════
echo "── Step 1: Prerequisites ──────────────────────────────────────────────────"

# 1a. Homebrew
if command -v brew &>/dev/null; then
    _skip "Homebrew: $(brew --version 2>/dev/null | head -1)"
else
    _inst "Installing Homebrew (NONINTERACTIVE=1)..."
    NONINTERACTIVE=1 /bin/bash -c \
        "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Re-source after install
    if [[ -x /opt/homebrew/bin/brew ]]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [[ -x /usr/local/bin/brew ]]; then
        eval "$(/usr/local/bin/brew shellenv)"
    fi
    if command -v brew &>/dev/null; then
        _ok "Homebrew installed"
    else
        _fail "Homebrew install failed — PATH may need refresh; re-run the script"
        exit 1
    fi
fi

# 1b. Python3 >= 3.9
if python3 -c "import sys; assert sys.version_info >= (3,9)" &>/dev/null; then
    _skip "python3: $(python3 --version 2>&1)"
else
    _inst "Installing python@3.11..."
    brew install python@3.11
    if python3 -c "import sys; assert sys.version_info >= (3,9)" &>/dev/null; then
        _ok "python3: $(python3 --version 2>&1)"
    else
        _fail "python3 install failed"
        exit 1
    fi
fi

# 1c. Colima binary
if command -v colima &>/dev/null; then
    _skip "colima: $(colima version 2>/dev/null | head -1 || echo 'installed')"
else
    _inst "Installing colima..."
    brew install colima
    if command -v colima &>/dev/null; then
        _ok "colima installed"
    else
        _fail "colima install failed"
        exit 1
    fi
fi

# 1d. Docker CLI
if command -v docker &>/dev/null; then
    _skip "docker: $(docker --version 2>/dev/null || echo 'installed')"
else
    _inst "Installing docker CLI..."
    brew install docker
    if command -v docker &>/dev/null; then
        _ok "docker CLI installed"
    else
        _fail "docker CLI install failed"
        exit 1
    fi
fi

# ══════════════════════════════════════════════════════════════════════════════
# Step 2: Agent deployment
# ══════════════════════════════════════════════════════════════════════════════
echo ""
echo "── Step 2: Agent ──────────────────────────────────────────────────────────"

# 2a. Directories
if [[ -d "${HLAB_DIR}/agent" && -d "${HLAB_DIR}/logs" ]]; then
    _skip "${HLAB_DIR}/{agent,logs} already exist"
else
    _inst "Creating ${HLAB_DIR}/{agent,logs}..."
    sudo mkdir -p "${HLAB_DIR}/agent" "${HLAB_DIR}/logs"
    sudo chown -R "$(whoami)" "${HLAB_DIR}"
    _ok "Directories created and owned by $(whoami)"
fi

# 2b. agent.py — atomic copy if sha256 differs
AGENT_UPDATED=0
if [[ -f "$AGENT_DEST" ]] && \
   [[ "$(_sha256 "$AGENT_SRC")" == "$(_sha256 "$AGENT_DEST")" ]]; then
    _skip "agent.py is up to date"
else
    _inst "Installing agent.py (sha256 changed or not present)..."
    AGENT_TMP="${AGENT_DEST}.tmp"
    cp "$AGENT_SRC" "$AGENT_TMP"
    # Stop agent before swapping the file
    if launchctl list "$PLIST_LABEL" &>/dev/null; then
        launchctl bootout "gui/$(id -u)/${PLIST_LABEL}" 2>/dev/null || true
        sleep 1
    fi
    mv "$AGENT_TMP" "$AGENT_DEST"
    AGENT_UPDATED=1
    _ok "agent.py installed"
fi

# 2c. Python deps (installed for /usr/bin/python3 — the plist interpreter)
if /usr/bin/python3 -c "import psutil, yaml" &>/dev/null; then
    _skip "Python deps already installed (psutil, pyyaml)"
else
    _inst "Installing Python deps (psutil, pyyaml)..."
    /usr/bin/python3 -m pip install --user psutil pyyaml
    if /usr/bin/python3 -c "import psutil, yaml" &>/dev/null; then
        _ok "Python deps installed"
    else
        _fail "Python deps install failed"
    fi
fi

# 2d. Agent config — write once, never overwrite
if [[ -f "$AGENT_CONFIG" ]]; then
    _skip "agent-config.yml already exists (not overwritten)"
else
    _inst "Writing default agent-config.yml..."
    cat > "$AGENT_CONFIG" <<EOF
port: ${AGENT_PORT}
services:
  - name: "Colima"
    type: colima
  - name: "Docker"
    type: process
    process: com.docker.backend
EOF
    _ok "agent-config.yml written to ${AGENT_CONFIG}"
fi

# 2e. LaunchAgent plist — install or update if content changed
PLIST_UPDATED=0
if [[ -f "$PLIST_DEST" ]] && diff -q "$PLIST_SRC" "$PLIST_DEST" &>/dev/null; then
    _skip "LaunchAgent plist up to date"
else
    _inst "Installing LaunchAgent plist → ${PLIST_DEST}..."
    # Unload before replacing
    if launchctl list "$PLIST_LABEL" &>/dev/null; then
        launchctl bootout "gui/$(id -u)/${PLIST_LABEL}" 2>/dev/null || true
        sleep 1
    fi
    if sudo cp "$PLIST_SRC" "$PLIST_DEST"; then
        PLIST_UPDATED=1
        _ok "LaunchAgent plist installed"
    else
        _fail "Failed to install LaunchAgent plist (sudo cp failed)"
    fi
fi

# 2f. Load / reload LaunchAgent
if launchctl list "$PLIST_LABEL" &>/dev/null; then
    if [[ "$AGENT_UPDATED" -eq 1 || "$PLIST_UPDATED" -eq 1 ]]; then
        _inst "Reloading LaunchAgent (agent or plist changed)..."
        if launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST" 2>/dev/null; then
            _ok "LaunchAgent reloaded (bootstrap)"
        elif launchctl load -w "$PLIST_DEST" 2>/dev/null; then
            _warn "LaunchAgent reloaded via legacy 'load -w'"
        else
            _warn "LaunchAgent reload failed — agent will restart on next GUI login"
        fi
    else
        _skip "LaunchAgent ${PLIST_LABEL} already running"
    fi
else
    _inst "Loading LaunchAgent ${PLIST_LABEL}..."
    if launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST" 2>/dev/null; then
        _ok "LaunchAgent loaded (bootstrap)"
    elif launchctl load -w "$PLIST_DEST" 2>/dev/null; then
        _warn "LaunchAgent loaded via legacy 'load -w' (no active GUI session detected)"
    else
        _warn "LaunchAgent could not be loaded — agent will start on next GUI login"
    fi
fi

# ══════════════════════════════════════════════════════════════════════════════
# Step 3: Colima
# ══════════════════════════════════════════════════════════════════════════════
echo ""
echo "── Step 3: Colima ─────────────────────────────────────────────────────────"

# 3a. Insecure registry patch (atomic: write .tmp → os.replace)
REGISTRY_ADDED=0

if [[ -f "$COLIMA_YAML" ]] && grep -qF "${REGISTRY}" "$COLIMA_YAML" 2>/dev/null; then
    _skip "Insecure registry ${REGISTRY} already in colima.yaml"
else
    _inst "Adding ${REGISTRY} to Colima insecure-registries in ${COLIMA_YAML}..."
    mkdir -p "$(dirname "$COLIMA_YAML")"
    export COLIMA_YAML REGISTRY
    /usr/bin/python3 - <<'PYEOF'
import os, sys
try:
    import yaml
except ImportError:
    print("[FAIL] pyyaml not found for /usr/bin/python3", file=sys.stderr)
    sys.exit(1)

path = os.environ["COLIMA_YAML"]
registry = os.environ["REGISTRY"]
cfg = {}
if os.path.exists(path):
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
docker_cfg = cfg.setdefault("docker", {})
regs = docker_cfg.setdefault("insecure-registries", [])
if registry not in regs:
    regs.append(registry)
tmp = path + ".tmp"
with open(tmp, "w") as f:
    yaml.dump(cfg, f, default_flow_style=False)
os.replace(tmp, path)
PYEOF
    REGISTRY_ADDED=1
    _ok "Insecure registry configured"
fi

# 3b. Start or restart Colima
if colima status 2>&1 | grep -qi "running"; then
    if [[ "$REGISTRY_ADDED" -eq 1 ]]; then
        _inst "Restarting Colima to apply registry config..."
        colima restart
        if colima status 2>&1 | grep -qi "running"; then
            _ok "Colima restarted"
        else
            _fail "Colima restart failed"
        fi
    else
        _skip "Colima already running, registry already configured"
    fi
else
    _inst "Starting Colima (cpu=${COLIMA_CPU}, memory=${COLIMA_MEMORY}GB, disk=${COLIMA_DISK}GB)..."
    colima start --cpu "$COLIMA_CPU" --memory "$COLIMA_MEMORY" --disk "$COLIMA_DISK"
    if colima status 2>&1 | grep -qi "running"; then
        _ok "Colima started"
    else
        _fail "Colima failed to start — check 'colima logs'"
    fi
fi

# ══════════════════════════════════════════════════════════════════════════════
# Step 4: Verification
# ══════════════════════════════════════════════════════════════════════════════
echo ""
echo "── Step 4: Verification ───────────────────────────────────────────────────"

sleep 3   # allow agent to start after launchctl load

# Agent /health
HEALTH=$(curl -s --max-time 5 "http://localhost:${AGENT_PORT}/health" 2>/dev/null || true)
if [[ "$HEALTH" == "ok" ]]; then
    _ok "Agent /health → ok"
else
    _fail "Agent /health did not return 'ok' (got: '${HEALTH}')"
fi

# Agent /metrics
if curl -s --max-time 5 "http://localhost:${AGENT_PORT}/metrics" 2>/dev/null \
   | /usr/bin/python3 -c \
       "import json,sys; d=json.load(sys.stdin); assert 'hostname' in d" &>/dev/null; then
    _ok "Agent /metrics → valid JSON with hostname"
else
    _fail "Agent /metrics check failed"
fi

# Colima running
if colima status 2>&1 | grep -qi "running"; then
    _ok "Colima is running"
else
    _fail "Colima is not running"
fi

# Docker sees the insecure registry
if docker info 2>/dev/null | grep -qF "$REGISTRY"; then
    _ok "Docker info shows insecure registry ${REGISTRY}"
else
    _fail "Docker info does not show ${REGISTRY} — try 'colima restart'"
fi

# SSH verification (manual — requires action on calling machine)
HOST_IP=$(ipconfig getifaddr en0 2>/dev/null \
    || ipconfig getifaddr en1 2>/dev/null \
    || echo "<HOST_IP>")
echo ""
echo "[INFO] Manual SSH verification (run from calling machine):"
echo "       DOCKER_HOST=ssh://$(whoami)@${HOST_IP} docker ps"
echo ""
echo "[INFO] If SSH key is not yet authorised on this host:"
echo "       ssh-copy-id $(whoami)@${HOST_IP}"

# ══════════════════════════════════════════════════════════════════════════════
# Summary
# ══════════════════════════════════════════════════════════════════════════════
echo ""
echo "────────────────────────────────────────────────────────────────────────────"
if [[ "$FAILS" -eq 0 ]]; then
    echo "[ OK ] All checks passed — dev host setup complete"
    exit 0
else
    echo "[FAIL] ${FAILS} check(s) failed — review [FAIL] lines above"
    exit 1
fi
