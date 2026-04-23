---
openapi: "3.0"
info:
  title: "Slot Workflow — Reservation, Deploy, Validate, Release"
  version: "0.1"
  status: "draft"
  authors: []
  updated: "2026-04-22"
  scope: "Defines the canonical end-to-end workflow for reserving a dev slot, deploying a container, validating it, and releasing — serving as both the reference example and the acceptance test for the slot reservation system"
  owner: "specs"
  components:
    - "examples/slot-workflow.sh"
    - "examples/smoke-test"
    - "server"
---

# Slot Workflow — Reservation, Deploy, Validate, Release

> The canonical end-to-end example for the slot reservation system: reserve a dev slot from the monitor, push a container image to the lab registry, run it on the assigned host, validate its HTTP endpoint, and release the slot. Read this before wiring the slot API into any caller workflow.

## 1. Scope

This spec governs:

- The end-to-end slot lifecycle: capabilities check → reserve → build/push → deploy → validate → release
- The `examples/slot-workflow.sh` test harness — what it does, how to run it, what constitutes pass/fail
- The `examples/smoke-test/` minimal container image — its purpose, interface, and constraints
- Prerequisite host configuration for Colima-based dev hosts (insecure registry, SSH key)
- How `ssh_user`, `registry_url`, `compose_project`, `port_range_start`, and `port_range_end` fields from the slot response are used by callers

Out of scope:
- The slot API contract itself — see `specs/slot-registry-and-cc-v0.1.md`
- Dashboard UI for slot management — see `specs/home-lab-monitor-spec-v0.1.md`
- Production-grade container orchestration (Compose files, multi-service deployments)

## 2. Context

The slot reservation system (spec: `slot-registry-and-cc-v0.1.md`) defines the API contract but does not specify how a caller should use the returned slot fields to actually run containers on a dev host. Without a concrete working example, the path from "slot reserved" to "container running and reachable" involves several undocumented steps: SSH-tunneled Docker, insecure registry configuration, port mapping, and cleanup ordering.

This spec fills that gap. It defines a smoke test that exercises the full cycle, catches integration failures early, and serves as the reference implementation for any caller building on the slot API.

## 3. Decision / Specification

### 3.1 Workflow Steps

The canonical workflow for a slot-based deployment is:

```
1. GET  /api/capabilities          → confirm free slots and registry URL
2. POST /api/slots                 → reserve slot, get host/port/ssh_user/registry_url
3. docker build + docker push      → push image to lab registry from local machine
4. DOCKER_HOST=ssh://user@host     → connect to dev host's Docker/Colima daemon
5. docker run -p PORT:CONTAINER_PORT → start container, map assigned port
6. curl http://HOST_ADDR:PORT/health → validate service is responding
7. docker rm -f CONTAINER          → stop and remove container
8. DELETE /api/slots/{id}          → release the slot
```

Steps 3–7 are the caller's responsibility. The monitor server is involved only in steps 1, 2, and 8.

**On abort:** the slot TTL (`ttl_seconds=300` in the smoke test) means an aborted run self-cleans in under 5 minutes. The test harness also registers a `trap` on `EXIT` to attempt cleanup regardless of how it exits.

### 3.2 Prerequisites

#### Local machine (where the script runs)

| Requirement | Check |
| --- | --- |
| Docker daemon running | `docker info` |
| SSH key loaded for dev hosts | `ssh-add -l` |
| SSH access to dev host | `ssh USER@HOST_ADDR echo ok` |
| Monitor server reachable | `curl http://MONITOR/api/capabilities` |

#### Dev host (where the container runs)

Run `scripts/setup-dev-host.sh` to configure a dev host end-to-end (see `specs/dev-host-setup-v0.1.md` for the full specification). This installs Colima, the monitoring agent, and the insecure-registry configuration in a single idempotent pass. Safe to cancel and re-run at any point.

```bash
# From the repo root on the dev host (or via SSH with REPO_ROOT set):
./scripts/setup-dev-host.sh
```

After running, two items still require manual action on the **calling machine**:

**1. SSH key** — the calling machine's key must be in `~/.ssh/authorized_keys` on the dev host. The setup script prints the host public key and instructions.

Verify: `DOCKER_HOST=ssh://USER@HOST_ADDR docker ps`

**2. Insecure registry on calling machine** — if the calling machine is also doing `docker push` to `192.168.1.93:5000`, add the registry to its own Docker daemon's insecure-registries. The dev host's Colima config is handled by the setup script.

### 3.3 Slot Response Fields Used by This Workflow

After `POST /api/slots`, the response contains:

| Field | Used for |
| --- | --- |
| `id` | Container name (`hlab-{id}`), cleanup, slot release |
| `host` | Logging and display |
| `host_address` | SSH connection target; HTTP validation endpoint |
| `ssh_user` | `DOCKER_HOST=ssh://USER@host_address` |
| `registry_url` | Image tag prefix; push destination |
| `compose_project` | Docker container/project name (`hlab-{id}`) |
| `port_range_start` | Host-side port for `-p PORT:8080` |
| `port_range_end` | Upper bound of reserved port range (unused in single-container case) |
| `expires_in_seconds` | Informs TTL choice; caller should heartbeat if workflow exceeds TTL |

### 3.4 Smoke Test Container (`examples/smoke-test/`)

**Purpose:** the minimal viable HTTP service for validating a deployed slot. Not a real workload — just enough to confirm the network path from monitor → slot → container → HTTP response is clear.

**Interface:**

- Listens on `$PORT` (default 8080)
- `GET /` and `GET /health` → `200 OK` with JSON body:
  ```json
  {
    "status": "ok",
    "service": "hlab-smoke-test",
    "slot_id": "<HLAB_SLOT_ID env var>",
    "host": "<container hostname>"
  }
  ```
- All other paths → `404`

**Environment variables:**

| Var | Description |
| --- | --- |
| `PORT` | Listen port (default: `8080`) |
| `HLAB_SLOT_ID` | Passed in from slot reservation; echoed back in response |

**Image:** `FROM python:3.11-alpine` — single stdlib Python file, no packages to install.

### 3.5 Test Harness (`examples/slot-workflow.sh`)

**Usage:**
```bash
./examples/slot-workflow.sh [MONITOR_URL]
# default MONITOR_URL: http://192.168.1.129:8888
```

**Pass criteria:** script exits 0 with `OK  Smoke test passed` as the final line.

**Fail criteria:** any step exits non-zero. Specific failure messages:
- `Cannot reach monitor` — server down or wrong URL
- `No free slots available` — all slots occupied or all hosts over load threshold
- `POST /api/slots failed` — server error on reservation
- `No ssh_user returned` — `ssh_user` missing from `config.yml` for the chosen host
- `docker build failed` — build error in smoke-test image
- `docker push failed` — registry unreachable or not in insecure-registries
- `docker run failed` — Colima not running on dev host, or SSH key not configured
- `Service did not become ready` — container started but port not reachable (firewall, wrong port mapping)
- `Unexpected status` — container running but returning unexpected response

**Cleanup on failure:** the `EXIT` trap fires on any abort, running `docker rm -f` on the container and `DELETE /api/slots/{id}`. The slot TTL (5 minutes) ensures cleanup even if the trap itself fails.

**Registry fallback:** if `/api/capabilities` shows the registry offline or unconfigured, the script builds the image locally and runs it without a push — the container runs from the local build pulled via SSH. This degrades gracefully but skips the push validation.

### 3.6 Port Assignment

Each slot gets a contiguous block of ports: `[port_range_start, port_range_end]` (default stride: 10 ports). The smoke test uses only `port_range_start`. A real workload using multiple services (e.g. app + db + proxy) would map each into a different port within the range:

```bash
APP_PORT=$((PORT_RANGE_START + 0))
DB_PORT=$((PORT_RANGE_START + 1))
PROXY_PORT=$((PORT_RANGE_START + 2))
```

No sub-allocation is managed by the monitor — the caller owns the mapping within the reserved range.

## 4. Schema / Interface Definition

### 4.1 Smoke-Test HTTP Response

```json
{
  "status":   "ok",
  "service":  "hlab-smoke-test",
  "slot_id":  "string",
  "host":     "string (container hostname)"
}
```

### 4.2 Slot Response Fields (abridged — full schema in `slot-registry-and-cc-v0.1.md` §4.1)

```json
{
  "id":               "string (8-char hex)",
  "host":             "string",
  "host_address":     "string",
  "ssh_user":         "string | null",
  "registry_url":     "string | null",
  "compose_project":  "string (hlab-{id})",
  "port_range_start": "integer",
  "port_range_end":   "integer",
  "expires_in_seconds": "integer"
}
```

## 5. Constraints

1. **The smoke test must run without `jq`.** All JSON parsing uses `python3` stdlib — the only guaranteed JSON tool on all lab hosts.
2. **The smoke test must be idempotent.** Multiple consecutive runs must not leave orphaned containers or unreleased slots.
3. **TTL must be set to 300 seconds in the smoke test.** Short enough to auto-expire quickly if cleanup fails; long enough for a typical run to complete.
4. **The container must use only the port at `port_range_start`.** Single-port convention for a single-service smoke test; callers may use additional ports within `[port_range_start, port_range_end]` for real workloads.
5. **`ssh_user` must be present in `config.yml` for every `docker: true` host.** The test harness fails fast if `ssh_user` is missing rather than guessing.
6. **The smoke-test image must build in under 60 seconds.** It is `FROM python:3.11-alpine` with a single file copy — no package installs.
7. **The harness must restore `DOCKER_HOST`** to its value before the script ran (or unset it if it was not set). Setting `DOCKER_HOST` globally without restoring it breaks subsequent Docker commands in the same shell session.

## 6. Rationale

**Why a shell script rather than Python?**
The workflow is a sequence of CLI invocations (`curl`, `docker`). A shell script expresses this naturally, keeps the example dependency-free, and is easier to adapt into a Makefile target or CI step than a Python script with imports.

**Why `python:3.11-alpine` for the smoke-test image?**
Zero additional packages. The `http.server` module handles GET requests. The image builds in seconds. `alpine` keeps it under 50 MB — small enough that push/pull over a LAN registry is fast.

**Why not use `nginx:alpine` directly?**
nginx doesn't echo `slot_id` back in the response. The ability to see the slot ID in the health response confirms the right container is running and the environment variable reached it. It also makes the smoke test distinguishable from any nginx container that might already be running on the host.

**Why 300-second TTL?**
The smoke test should complete in under 30 seconds on a healthy fleet. 300 seconds (5 minutes) gives plenty of margin for slow registry pulls while still self-cleaning quickly after an abort. Any real workload should use a longer TTL and send heartbeats.

**Alternatives considered:**

| Option | Rejected because |
| --- | --- |
| Use `nginx:alpine` directly | Does not validate slot_id or env var propagation |
| Use `httpbin` | 200+ MB image; overkill for a smoke test |
| Docker Compose file | Adds Compose as a dependency; single-container test doesn't need it |
| Python test script with `requests` | Adds a pip dependency; shell is more portable for a demo harness |

## 7. Open Questions

- [x] **Colima insecure-registry one-time setup** — resolved: `scripts/setup-dev-host.sh` handles this automatically. See `specs/dev-host-setup-v0.1.md`.
- [ ] **Multi-port mapping example** — §3.6 describes the pattern but no concrete example exists yet. Should `examples/` include a multi-service Compose variant? Impact: callers building real workloads have no template. Owner: future example.
- [ ] **Windows/Docker Desktop support** — `DOCKER_HOST=ssh://` behaves differently on Docker Desktop for Mac vs Colima. This workflow is tested only with Colima. Owner: if non-Colima hosts are added to the fleet.

## 8. Changelog

| Version | Date | Summary |
| --- | --- | --- |
| 0.1 | 2026-04-22 | Initial draft — full workflow spec, smoke-test container interface, test harness pass/fail criteria, and prerequisites |
