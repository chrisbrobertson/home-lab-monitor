---
openapi: "3.0"
info:
  title: "Dev Host Setup — Idempotent Bootstrap Script"
  version: "0.1"
  status: "draft"
  authors: []
  updated: "2026-04-23"
  scope: "Defines the target state for a macOS dev laptop acting as both a monitoring agent host and a Docker slot host, and the idempotency contract for scripts/setup-dev-host.sh"
  owner: "specs"
  components:
    - "scripts/setup-dev-host.sh"
    - "agent"
    - "launchd"
---

# Dev Host Setup — Idempotent Bootstrap Script

> Authoritative reference for what a fully configured dev slot host looks like and how `scripts/setup-dev-host.sh` gets a macOS laptop there. Read this before running the script on a new host or debugging a host that is not appearing correctly in the dashboard.

## 1. Scope

This spec governs:

- The target state for a macOS dev laptop serving dual roles: monitoring agent host and Docker slot host (Colima)
- The idempotency contract for `scripts/setup-dev-host.sh` — what "already satisfied" means for each step, what action is taken when it isn't, and what constitutes a successful outcome
- Verification criteria: what the script checks at completion and what manual steps remain
- Prerequisites for the script itself (SSH access, sudo, repo availability)

Out of scope:
- Server setup (Mac Mini) — see `launchd/com.homelab.monitor.server.plist` and the deploy script
- LLM server setup (Spark DGX, Mac Studio) — different role, different service checks
- Linux agent setup — see `systemd/hlab-agent.service`
- Adding the host to `config.yml` — that is a repo commit by the operator after the host is set up
- SSH key provisioning on calling machines — requires action on the caller side; the script prints instructions

## 2. Context

Dev laptops serve two roles simultaneously:

1. **Monitoring host** — `agent.py` runs on port 9100, reporting CPU, memory, disk, I/O, and service health (Colima status, Docker process) to the server every 60 seconds.
2. **Docker slot host** — Colima runs as the container runtime. Callers reserve a slot via `POST /api/slots` and deploy containers via `DOCKER_HOST=ssh://USER@HOST docker run ...`. Colima must trust the lab registry (`192.168.1.93:5000`) as insecure for image pulls.

Previously, onboarding a new dev laptop required a dozen manual steps across the README, `specs/slot-workflow-example-v0.1.md §3.2`, and implicit tribal knowledge. There was no single script, no clear definition of "done", and no way to re-run safely if something was partially completed.

The setup script is designed to be run by an automated agent (e.g. Claude Code) connecting via SSH with sudo access. It is fully non-interactive — no prompts, no opt-ins, no user interaction required. It is safe to cancel mid-run, re-run immediately, or run on a machine that already has some steps completed.

## 3. Decision / Specification

### 3.1 Target State

A fully configured dev slot host has the following state after running `scripts/setup-dev-host.sh`:

#### Software (installed via Homebrew)

| Package | Purpose | Already present check |
|---------|---------|----------------------|
| Homebrew | Package manager | `command -v brew` |
| `python3` ≥ 3.9 | Agent runtime | `python3 -c "import sys; assert sys.version_info >= (3,9)"` |
| `colima` | macOS container runtime | `command -v colima` |
| `docker` CLI | Docker client for slot callers | `command -v docker` |

Homebrew is installed non-interactively (`NONINTERACTIVE=1`) if missing. All packages installed via `brew install` without prompts.

#### Agent

| Item | Location | Notes |
|------|---------|-------|
| `agent.py` | `/opt/hlab/agent/agent.py` | Copied from repo; updated atomically if sha256 changes |
| Agent config | `/opt/hlab/agent-config.yml` | Written once; never overwritten (user may add service checks) |
| Log directory | `/opt/hlab/logs/` | Created with sudo; owned by SSH user |
| LaunchAgent plist | `/Library/LaunchAgents/com.homelab.monitor.agent.plist` | Copied from `launchd/com.homelab.monitor.agent.plist`; updated if content changes |
| Python deps | user-local (`pip3 install --user`) | `psutil`, `pyyaml` |
| Agent port | 9100 (configurable via `AGENT_PORT`) | Confirmed at: `curl http://localhost:9100/health` → `ok` |

**Default agent config** (written only if `/opt/hlab/agent-config.yml` does not exist):

```yaml
port: 9100
services:
  - name: "Colima"
    type: colima
  - name: "Docker"
    type: process
    process: com.docker.backend
```

#### Colima

| Item | Target state |
|------|-------------|
| VM running | `colima status` exits 0 and shows "Running" |
| VM size | 4 CPU / 8 GB RAM / 60 GB disk (only applied at first `colima start`; running VMs are not resized) |
| Insecure registry | `192.168.1.93:5000` present in `~/.colima/default/colima.yaml` under `docker.insecure-registries` |
| Docker visibility | `docker info` shows `192.168.1.93:5000` under "Insecure Registries" |

### 3.2 Idempotency Contract

The script follows **read current state → compare to target → act only if different → verify** for every step. Running it a second time when fully configured must produce only `[SKIP]` lines and exit 0.

It is safe to:
- Cancel with Ctrl-C at any point
- Re-run immediately after cancellation
- Run on a machine with some or all steps already done
- Run after a repo update that changes `agent.py` or the plist

All file replacements use atomic `write-to-tmp → mv` (via `os.replace()` in Python or `mv` in shell). A cancelled run leaves the system in a consistent state — either the old file or the new file, never a half-written intermediate.

| Step | "Already satisfied" check | Action if not satisfied |
|------|--------------------------|------------------------|
| Homebrew | `command -v brew` (after PATH setup) | `NONINTERACTIVE=1 /bin/bash -c "$(curl ...)"` |
| Python3 ≥ 3.9 | `python3` exists and version check passes | `brew install python@3.11` |
| Colima binary | `command -v colima` | `brew install colima` |
| Docker CLI | `command -v docker` | `brew install docker` |
| `/opt/hlab/` dirs | `-d /opt/hlab/agent && -d /opt/hlab/logs` | `sudo mkdir -p ...; sudo chown -R $(whoami) /opt/hlab` |
| `agent.py` | sha256 of installed file matches repo copy | Stop agent (if running) → atomic copy → restart |
| Python deps | `python3 -c "import psutil, yaml"` exits 0 | `pip3 install --user psutil pyyaml` |
| Agent config | `/opt/hlab/agent-config.yml` exists (any content) | Write default — never overwrite |
| LaunchAgent plist | `/Library/LaunchAgents/com.homelab.monitor.agent.plist` exists AND `diff` vs repo plist shows no change | Unload (if running) → `sudo cp` → reload |
| LaunchAgent running | `launchctl list com.homelab.monitor.agent` exits 0 | `launchctl bootstrap gui/$(id -u) <plist>` with fallback to `launchctl load -w` |
| Colima insecure registry | `~/.colima/default/colima.yaml` exists AND contains `192.168.1.93:5000` under `docker.insecure-registries` | Python3 yaml atomic patch; restart Colima if it was running |
| Colima running | `colima status` shows Running | `colima start --cpu N --memory N --disk N` (only applies size on first start) |

**LaunchAgent load strategy:** `launchctl bootstrap gui/$(id -u)` is the primary method (requires active GUI session). Falls back to `launchctl load -w` for headless SSH sessions. If both fail, the agent starts on next GUI login — script prints a warning rather than failing.

**Colima restart policy:** if the registry was just added to the yaml AND Colima was already running → `colima restart`. If Colima was not running → `colima start` (picks up new yaml). If registry was already present → no restart.

### 3.3 Script Interface

```
scripts/setup-dev-host.sh

Environment variables (all have defaults):
  HLAB_DIR        Deploy directory                  default: /opt/hlab
  AGENT_PORT      Agent listen port                 default: 9100
  REGISTRY        Lab registry (insecure)           default: 192.168.1.93:5000
  COLIMA_CPU      Colima VM CPU count               default: 4
  COLIMA_MEMORY   Colima VM memory in GB            default: 8
  COLIMA_DISK     Colima VM disk in GB              default: 60
  REPO_ROOT       Path to repo root                 default: parent of scripts/
```

**Usage:** `./scripts/setup-dev-host.sh` — run from the repo root. When run by an agent over SSH, the agent sets `REPO_ROOT` explicitly.

**Output format:** every operation is logged with a prefix:
- `[SKIP]` — step already satisfied, no action taken
- `[INST]` — installing or applying a change
- `[ OK ]` — step succeeded
- `[WARN]` — non-fatal issue (e.g. GUI session not active for launchctl)
- `[FAIL]` — verification failed after application

**Exit codes:**
- `0` — all steps satisfied and all verifications passed
- `1` — one or more verification steps failed

### 3.4 What the Script Does NOT Do

These are intentionally excluded — each requires human decision or action on a different machine:

- **Does not update `config.yml`** — adding the host to the server's config is a repo commit by the operator
- **Does not add SSH keys to `~/.ssh/authorized_keys`** — the caller's key must be added manually; the script prints the host's public key and instructions at the end
- **Does not configure Ollama** — LLM-server concern; not applicable to dev-laptop role
- **Does not install `nvidia-ml-py`** — dev laptops have no NVIDIA GPU
- **Does not support Linux** — macOS/launchd only; see `systemd/hlab-agent.service` for Linux

### 3.5 Verification

The script runs these checks at completion and prints pass/fail for each:

```bash
# Agent
curl -s http://localhost:9100/health          # must return exactly "ok"
curl -s http://localhost:9100/metrics         # must return valid JSON with "hostname" key

# Colima
colima status                                 # must show "Running"
docker info | grep "192.168.1.93:5000"        # must appear under Insecure Registries

# SSH (printed as manual instruction — requires action on calling machine)
# DOCKER_HOST=ssh://USER@HOST_IP docker ps
```

A `[FAIL]` on any automated check causes exit 1. The SSH check is informational only.

## 4. Schema / Interface Definition

### 4.1 Agent Config Written by Setup Script

```yaml
port: 9100           # configurable via AGENT_PORT
services:
  - name: "Colima"
    type: colima      # runs `colima status`; highlighted badge on dashboard
  - name: "Docker"
    type: process
    process: com.docker.backend   # psutil process name match
```

### 4.2 Colima YAML Patch (target shape)

```yaml
# ~/.colima/default/colima.yaml (relevant section after patch)
docker:
  insecure-registries:
    - 192.168.1.93:5000
```

Other keys in the file (cpu, memory, disk, mounts) are preserved as-is by the Python3 yaml patch.

### 4.3 LaunchAgent Plist (source: `launchd/com.homelab.monitor.agent.plist`)

```xml
<!-- installed to /Library/LaunchAgents/com.homelab.monitor.agent.plist -->
<key>ProgramArguments</key>
<array>
    <string>/usr/bin/python3</string>
    <string>/opt/hlab/agent/agent.py</string>
    <string>/opt/hlab/agent-config.yml</string>
</array>
<key>WorkingDirectory</key><string>/opt/hlab</string>
<key>StandardOutPath</key><string>/opt/hlab/logs/agent.log</string>
<key>StandardErrorPath</key><string>/opt/hlab/logs/agent.err</string>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>
```

The plist is copied verbatim from the repo — no template substitution needed (it uses fixed `/opt/hlab/` paths).

## 5. Constraints

1. **No interactive prompts.** The script runs non-interactively over SSH. No `read`, no `select`, no `tty` operations.
2. **No extra dependencies.** Uses only: `bash`, `python3` stdlib, `curl` (always present on macOS), and what the script installs (`brew`, `colima`, `docker`). No `jq`, `yq`, or other tools assumed present.
3. **sudo used only where necessary:** `/opt/hlab/` creation, `/Library/LaunchAgents/` write. Homebrew install. User-level operations (Colima, pip3 install) run without sudo.
4. **Colima VM size is not changed on a running VM.** `--cpu`, `--memory`, `--disk` flags only take effect on `colima start`. If Colima is already running, only the insecure-registry is patched and a `colima restart` issued.
5. **Agent config is never overwritten** once it exists. The user may have added service checks; the script must not clobber them.
6. **All file replacements are atomic** (`write-to-tmp` → `mv`/`os.replace`). A Ctrl-C between the two cannot leave a half-written file.
7. **Colima runs as the SSH user, not root.** `colima start`, `colima restart`, `colima status` are run without sudo.
8. **LaunchAgent is updated when the plist changes.** If a repo update changes `launchd/com.homelab.monitor.agent.plist`, re-running the script unloads the old agent, installs the new plist, and reloads.
9. **The script is its own documentation.** Every step prints what it is checking, what it found, and what it did. No silent operations.

## 6. Rationale

**Why `/opt/hlab/` rather than `~/hlab/`?**
The system-level LaunchAgent plist (`launchd/com.homelab.monitor.agent.plist`) already uses `/opt/hlab/`. Using the same path for both the Mac Mini server and dev laptops simplifies the plist — no per-user path substitution needed. It requires sudo for initial directory creation, which is available to the agent.

**Why `pip3 install --user` rather than a venv?**
The agent has exactly two dependencies (`psutil`, `pyyaml`) that are stable and don't conflict with anything. A venv adds complexity (path management, activation in the plist) for no benefit at this scale. If conflicts ever arise, a venv can be added without changing the spec.

**Why `NONINTERACTIVE=1` Homebrew install rather than requiring it pre-installed?**
The script is run by an agent that may encounter a machine missing Homebrew. Requiring pre-installation makes the script less useful and adds a manual step. `NONINTERACTIVE=1` makes the Homebrew installer fully non-interactive.

**Why copy the plist verbatim rather than generating it from env vars?**
The plist already uses the correct fixed path (`/opt/hlab/`). Template generation adds complexity (sed substitution, escaping) with no benefit since the paths are standard. If a host needs a non-standard path, it overrides `HLAB_DIR` and the script can be extended.

**Alternatives considered:**

| Option | Rejected because |
|--------|-----------------|
| Ansible/Chef/Puppet | Heavyweight dependency for a personal lab; the script's needs are simple enough for bash |
| Docker Desktop instead of Colima | Docker Desktop has license restrictions and is heavier; Colima is the existing choice for the fleet |
| venv for Python deps | Adds path management complexity; psutil + pyyaml have no known conflict risk |
| Per-user `~/hlab/` path | Inconsistent with existing Mac Mini deployment; requires template substitution in plist |
| Require Homebrew pre-installed | Adds manual step for agent-driven setup; NONINTERACTIVE install handles it cleanly |

## 7. Open Questions

- [ ] **Apple Silicon PATH after Homebrew install** — Homebrew on Apple Silicon installs to `/opt/homebrew/bin`. Over SSH, this may not be in PATH until `eval "$(/opt/homebrew/bin/brew shellenv)"` runs. The script sources this, but if `brew install` itself fails to find brew after install, a shell restart may be needed. Impact: script may exit 1 on first run; re-run succeeds. Owner: test on fresh Apple Silicon VM.
- [ ] **Headless GUI session for launchctl** — `launchctl bootstrap gui/$(id -u)` requires an active GUI session. Dev laptops are assumed to have a user logged in at the console. If a laptop is headless (e.g. lid-closed, no display), the agent starts on next login. Impact: agent doesn't run until next login. Owner: acceptable for dev laptops; document the behavior.
- [ ] **Colima VM size on existing VMs** — if a dev laptop already has Colima running with 2 CPU / 4 GB, the script does not resize it. The operator must manually `colima stop && colima delete && colima start` to change the size. Owner: document in README; acceptable for now.

## 8. Changelog

| Version | Date | Summary |
|---------|------|---------|
| 0.1 | 2026-04-23 | Initial draft — target state, idempotency contract, script interface, and constraints for macOS dev slot host setup |
