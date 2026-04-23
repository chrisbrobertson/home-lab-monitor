---
openapi: "3.0"
info:
  title: "Slot Reservation, Image Registry, and Capabilities API"
  version: "0.2"
  status: "draft"
  authors: []
  updated: "2026-04-22"
  scope: "Extends home-lab-monitor with a slot reservation API, Docker image registry integration, and a capabilities endpoint — enabling development projects (e.g. Meridian pre-commit hooks) to discover available compute, reserve isolated Docker Compose slots, and push/pull images from a local registry"
  owner: "specs"
  components:
    - "server"
    - "server/static"
    - "config"
---

# Slot Reservation, Image Registry, and Capabilities API

> Turns home-lab-monitor from a passive monitoring dashboard into a reservation service: callers discover what the fleet can offer, claim an isolated compute slot on a host, and use that slot's port assignments and registry URL to orchestrate Docker Compose workloads — without any knowledge of the underlying topology.

## 1. Scope

This spec governs:

- The `GET /api/capabilities` endpoint: fleet-wide slot availability, registry location, and per-host load derived from live monitoring data
- The slot CRUD endpoints: `POST /api/slots`, `GET /api/slots`, `GET /api/slots/{slot_id}`, `DELETE /api/slots/{slot_id}`, `POST /api/slots/{slot_id}/heartbeat`
- The `GET /api/agent-guide` endpoint: a single human- and AI-agent-readable Markdown document describing what the service offers and how to use it
- The slot model: ID, host assignment, port offset, compose project name, TTL, expiry
- Slot placement policy: how a host is chosen, load gating using live metrics, capacity limits
- The `slots` and `slot_port_assignments` SQLite tables
- Docker image registry integration: how the registry host is configured and its status surfaced in `/api/capabilities`
- TTL and reaping: expired slot cleanup integrated into the existing polling loop
- `config.yml` additions: per-host `docker` and `max_slots` fields, top-level `registry:` and `slot_policy:` sections

Out of scope:
- Starting or stopping Docker Compose workloads — that is the caller's responsibility; this service issues a reservation, not a container runtime command
- Image build, push, or pull operations — the registry is external to this service; only its URL and health are surfaced
- Authentication — consistent with the existing no-auth design (§6 of `specs/home-lab-monitor-spec-v0.1.md`)
- Multi-region or internet-facing deployment — this is a local-network service

This spec lives at `specs/` (repo root) because it modifies both `server/` and `config/`.

## 2. Context

The Meridian pre-commit hook (`specs/local-dev-and-precommit-v0.1.md` in the Meridian repo) needs to:
1. Discover which lab hosts can accept a Docker Compose slot
2. Claim an isolated slot (namespaced project, non-colliding ports)
3. Know where to push and pull Docker images
4. Release the slot when done, or let it expire automatically

Home-lab-monitor already knows the real-time state of every host on the network — CPU load, memory pressure, whether the host is online. That monitoring data is the right foundation for placement decisions. Rather than building a separate fleet-management service, this adds a thin reservation layer on top of the existing server.

The design adapts the Mac-D and slot model from Meridian's `local-dev-and-precommit-v0.1.md`:

- **Mac D → home-lab-monitor server.** Lease records previously written as JSON files to a Mac D host are stored in the existing SQLite database. The server is the single source of truth.
- **Fleet config (`config/local-fleet.conf`) → `config.yml`.** Per-host Docker capability and slot capacity are expressed as fields on existing host entries, keeping one config file.
- **`scripts/detect-changed` / SSH slot start → caller responsibility.** The server does not SSH into hosts or manage containers. It issues a reservation; the caller uses the returned host address and port offset to do its own `DOCKER_HOST=ssh://...` work.

This approach preserves the home-lab-monitor design constraint of being a simple Python server with no external dependencies beyond psutil, FastAPI, and httpx.

## 3. Decision / Specification

### 3.1 Configuration Additions

Two new top-level sections are added to `config.yml`; all fields have defaults and are optional.

#### `registry:` section

```yaml
registry:
  host: "Spark DGX"         # name of a configured host that runs the Docker registry
  port: 5000                  # registry port on that host (default: 5000)
```

The named host must appear in the `hosts:` list. The server derives the registry URL as `<host.address>:<registry.port>`. If `registry:` is absent, the capabilities endpoint returns `"registry": null` and callers must handle it.

The registry itself (Docker Distribution `registry:2`) is deployed and managed independently:

```bash
docker run -d -p 5000:5000 --restart always --name registry registry:2
```

No TLS. Hosts that pull from or push to the registry add it to their insecure-registries list (Colima: `~/.colima/default/colima.yaml`; Docker Desktop: daemon.json).

#### `slot_policy:` section

```yaml
slot_policy:
  default_ttl_seconds: 14400    # 4 hours — how long a slot lives without a heartbeat
  max_ttl_seconds: 86400        # 24 hours — maximum TTL a caller may request
  reap_interval_seconds: 3600   # how often the reaper runs (piggybacks on poll loop)
  cpu_threshold: 85             # do not place on a host with CPU% above this
  memory_threshold: 85          # do not place on a host with memory% above this
  ports_per_slot: 20            # number of consecutive ports reserved per slot
  port_base: 20000              # starting port for slot port ranges
```

#### Per-host fields

Two optional fields are added to each entry in `hosts:`:

```yaml
hosts:
  - name: "Spark DGX"
    address: "192.168.1.93"
    port: 9100
    docker: true         # host has a Docker daemon; eligible for slot placement (default: false)
    max_slots: 2         # maximum concurrent slots on this host (default: 0)
    port_base: 20000     # per-host override for slot port_base (optional)
    ssh_user: "admin"    # SSH login name — returned in slot response for caller use
```

Hosts with `docker: false` (or no `docker:` field) and `max_slots: 0` (or no `max_slots:` field) are monitoring-only. They appear in `/api/summary` and `/api/metrics` but never in slot placement.

### 3.2 Slot Model

A slot is a logical reservation of isolated Docker Compose capacity on a single host. The server allocates it; the caller uses the returned fields to orchestrate containers. No containers are started or stopped by this server.

**Fields:**

| Field | Type | Description |
| --- | --- | --- |
| `slot_id` | string (8-char hex) | Unique identifier. Derived as `sha256(caller + label + timestamp)[:8]`. Stable for logging; not reused. |
| `host` | string | Name of the assigned host (matches `config.yml` host name) |
| `host_address` | string | IP or hostname of the assigned host |
| `ssh_user` | string \| null | SSH login for the caller to connect with `DOCKER_HOST=ssh://` |
| `registry_url` | string \| null | Docker registry URL (`<addr>:<port>`) — null if registry not configured |
| `compose_project` | string | Docker Compose project name: `hlab-<slot_id>`. Caller passes `-p hlab-<slot_id>` to all compose commands. |
| `port_offset` | integer | Slot index × `ports_per_slot`. Add to `port_base` to get the slot's starting port. |
| `port_range_start` | integer | `port_base + port_offset` — first usable port |
| `port_range_end` | integer | `port_range_start + ports_per_slot - 1` — last usable port (inclusive) |
| `caller` | string | Free-text label from the reservation request (e.g. `meridian-pre-commit`) |
| `label` | string \| null | Optional caller-supplied label (e.g. worktree path or branch name) |
| `created_at` | integer | Unix timestamp of slot creation |
| `expires_at` | integer | Unix timestamp after which the slot may be reaped |

**Port assignment example** (default policy: `port_base=20000`, `ports_per_slot=20`, slot index 1):
- `port_offset` = 1 × 20 = 20
- `port_range_start` = 20020
- `port_range_end` = 20039

The caller maps their services to ports within this range however they choose. No further sub-allocation is managed by this server.

### 3.3 Slot Placement

When `POST /api/slots` is received the server selects a host using this algorithm:

1. **Build candidate list:** all configured hosts where `docker: true` and `max_slots > 0`.
2. **Check online:** filter to hosts with a monitoring row in the last `2 × POLL_INTERVAL` seconds (i.e., not stale).
3. **Check capacity:** filter to hosts where `active_slot_count < max_slots`.
4. **Check load:** filter to hosts where the latest `cpu.percent < cpu_threshold` AND `memory.percent < memory_threshold`.
5. **Apply host hint:** if the request includes `"host_hint": "<name>"` and that host is in the candidate list, prefer it. If the hinted host is not in the candidate list (offline, over capacity, over load threshold), ignore the hint and continue with the full candidate list.
6. **Select:** from remaining candidates, choose the host with the highest `(max_slots - active_slots)` headroom. Ties broken by lowest `memory.percent`.
7. **Assign port offset:** find the lowest slot index `i` in `[0, max_slots)` not currently in use on that host. `port_offset = i × ports_per_slot`.
8. **Return 409** if no candidate host is found after all filters.

### 3.4 Slot TTL and Reaping

Every slot has an `expires_at` timestamp. The server does not automatically extend it — the caller must send a heartbeat to keep a slot alive.

**Heartbeat (`POST /api/slots/{slot_id}/heartbeat`):** accepts an optional `ttl_seconds` in the request body (must be ≤ `max_ttl_seconds`; defaults to `default_ttl_seconds`). Updates `expires_at = now + ttl_seconds`. Returns the updated slot.

**Reaping:** The existing polling loop (`polling_loop` in `server.py`) runs every `POLL_INTERVAL` seconds. A reaper check piggybacks on it every `reap_interval_seconds` (tracked by the server's in-memory state). On each reap pass:
1. `DELETE FROM slots WHERE expires_at < unixepoch()` — remove expired rows.
2. `DELETE FROM slot_port_assignments WHERE slot_id NOT IN (SELECT id FROM slots)` — remove orphaned port rows.
3. Log each reaped slot ID.

The reaper does not SSH into hosts or send container stop commands. It is the caller's responsibility to detect (via heartbeat failure or the `DELETE` response) that their slot is gone and tear down their containers.

### 3.5 SQLite Schema Additions

Two new tables are added to the existing `metrics.db` (migrations applied at server startup via `db.init()`).

```sql
CREATE TABLE IF NOT EXISTS slots (
    id              TEXT    PRIMARY KEY,          -- 8-char hex slot ID
    host            TEXT    NOT NULL,             -- host name from config
    host_address    TEXT    NOT NULL,
    ssh_user        TEXT,
    compose_project TEXT    NOT NULL,
    port_offset     INTEGER NOT NULL,
    port_range_start INTEGER NOT NULL,
    port_range_end   INTEGER NOT NULL,
    caller          TEXT    NOT NULL DEFAULT '',
    label           TEXT,
    created_at      INTEGER NOT NULL,
    expires_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_slots_host   ON slots (host);
CREATE INDEX IF NOT EXISTS idx_slots_expiry ON slots (expires_at);
```

No separate `slot_port_assignments` table is needed — the port range is stored inline on the `slots` row, and slot index recovery for reuse is computed at reservation time by querying `port_offset` values for the host.

### 3.6 API Endpoints

All new endpoints are added to `server/server.py` alongside the existing `/api/*` routes. Request and response bodies are JSON.

---

#### `GET /api/capabilities`

Returns fleet-wide slot availability, registry status, and per-host load snapshot. This is the discovery endpoint; callers call it before reserving a slot to understand what is available.

**Response `200 OK`:**

```json
{
  "registry": {
    "url": "192.168.1.93:5000",
    "host": "Spark DGX",
    "status": "online"
  },
  "slot_policy": {
    "default_ttl_seconds": 14400,
    "max_ttl_seconds": 86400,
    "ports_per_slot": 20,
    "port_base": 20000
  },
  "hosts": [
    {
      "name": "Spark DGX",
      "address": "192.168.1.93",
      "ssh_user": "admin",
      "docker": true,
      "max_slots": 2,
      "active_slots": 1,
      "available_slots": 1,
      "load": {
        "online": true,
        "last_seen": 1745349600,
        "cpu_percent": 34.0,
        "memory_percent": 52.0
      },
      "eligible": true
    },
    {
      "name": "mbp15-1",
      "address": "192.168.1.85",
      "ssh_user": null,
      "docker": false,
      "max_slots": 0,
      "active_slots": 0,
      "available_slots": 0,
      "load": {
        "online": true,
        "last_seen": 1745349600,
        "cpu_percent": 12.0,
        "memory_percent": 44.0
      },
      "eligible": false
    }
  ],
  "total_available_slots": 3,
  "total_active_slots": 1
}
```

`registry.status` is `"online"` if the server can reach `http://<registry_url>/v2/` with a 200 response, `"offline"` if the request fails, `"unconfigured"` if no `registry:` block is in `config.yml`.

`hosts[].eligible` reflects whether the host passes all placement filters at the moment of the request (online, under capacity, under load thresholds).

---

#### `POST /api/slots`

Reserve a slot. The server runs the placement algorithm (§3.3), creates the lease row, and returns the slot.

**Request body:**

```json
{
  "caller": "meridian-pre-commit",
  "label": "/Users/chris/repos/meridian",
  "host_hint": "Spark DGX",
  "ttl_seconds": 14400
}
```

All fields are optional except `caller` (required, max 128 chars).

**Response `201 Created`:**

```json
{
  "slot_id": "a3f8c201",
  "host": "Spark DGX",
  "host_address": "192.168.1.93",
  "ssh_user": "admin",
  "registry_url": "192.168.1.93:5000",
  "compose_project": "hlab-a3f8c201",
  "port_offset": 20,
  "port_range_start": 20020,
  "port_range_end": 20039,
  "caller": "meridian-pre-commit",
  "label": "/Users/chris/repos/meridian",
  "created_at": 1745349600,
  "expires_at": 1745364000
}
```

**Response `409 Conflict`** — no eligible host available:

```json
{
  "detail": "No eligible host available. Reasons: Spark DGX over cpu_threshold (91%), Mac Mini docker=false."
}
```

**Response `422 Unprocessable Entity`** — validation error (e.g., `ttl_seconds > max_ttl_seconds`).

---

#### `GET /api/slots`

List all active slots (not expired). Optional query parameter `?host=<name>` to filter by host.

**Response `200 OK`:**

```json
{
  "slots": [
    {
      "slot_id": "a3f8c201",
      "host": "Spark DGX",
      "host_address": "192.168.1.93",
      "ssh_user": "admin",
      "registry_url": "192.168.1.93:5000",
      "compose_project": "hlab-a3f8c201",
      "port_offset": 20,
      "port_range_start": 20020,
      "port_range_end": 20039,
      "caller": "meridian-pre-commit",
      "label": "/Users/chris/repos/meridian",
      "created_at": 1745349600,
      "expires_at": 1745364000
    }
  ]
}
```

---

#### `GET /api/slots/{slot_id}`

Get a single slot by ID.

**Response `200 OK`:** same shape as a single slot object above.

**Response `404 Not Found`** — slot does not exist or has expired and been reaped.

---

#### `DELETE /api/slots/{slot_id}`

Release a slot immediately. The server removes the lease row. The caller is responsible for stopping any containers.

**Response `204 No Content`** — slot released.

**Response `404 Not Found`** — slot not found.

---

#### `POST /api/slots/{slot_id}/heartbeat`

Extend a slot's TTL. Call this before `expires_at` to keep the slot alive.

**Request body (optional):**

```json
{
  "ttl_seconds": 14400
}
```

If omitted, `default_ttl_seconds` is used.

**Response `200 OK`:** full slot object with updated `expires_at`.

**Response `404 Not Found`** — slot not found or already reaped.

**Response `422 Unprocessable Entity`** — `ttl_seconds > max_ttl_seconds`.

---

#### `GET /api/agent-guide`

Returns a single self-contained Markdown document that an AI agent or automation client can fetch at startup to learn what this service provides and how to use it. The response is designed to be read directly — no further documentation lookup required for the common workflow.

The document covers:

1. A one-paragraph description of what the service does and who should call it
2. The base URL and request format (JSON bodies, no auth)
3. The typical workflow: discover → reserve → use → heartbeat → release
4. A concise reference for each endpoint (method, path, one-line purpose)
5. How to interpret `/api/capabilities` output (eligibility rules, load thresholds)
6. Port range semantics — what the slot's `port_range_start`/`port_range_end` mean and how to map services into it
7. SSH access pattern — `DOCKER_HOST=ssh://<ssh_user>@<host_address>`
8. Registry usage — plain HTTP, tag format, insecure-registry requirement
9. Error handling — `409` (no host), `404` (reaped or unknown), `422` (validation)
10. A live pointer to the current slot-policy values and registry URL, fetched at request time so the guide reflects the deployed configuration (not hardcoded defaults)
11. A link to the full spec (`specs/slot-registry-and-cc-v0.1.md`) for callers that need the complete contract

**Content negotiation:**

- `Accept: text/markdown` (default) — returns `text/markdown; charset=utf-8`, body is the Markdown document
- `Accept: application/json` — returns `application/json` with the envelope:
  ```json
  {
    "format": "markdown",
    "version": "0.1",
    "updated_at": 1745349600,
    "body": "<full markdown document as a single string>"
  }
  ```

Both representations carry the same content. The Markdown form is the primary representation; the JSON envelope exists for clients that prefer a structured wrapper (e.g., for embedding in tool-call responses).

**Response `200 OK`** — always. The endpoint has no error modes under normal operation. If the registry or hosts cannot be reached, the guide still renders — it describes the contract, not the live state. Callers who need live state call `/api/capabilities`.

**Caching:** responses include `Cache-Control: public, max-age=300`. The document is largely static; values interpolated from config change only when the server is reconfigured and restarted.

**Example response body (abridged):**

```markdown
# Home Lab Monitor — Slot Reservation Service

You are an AI agent or automation client. This service lets you reserve
isolated Docker Compose slots on a local-network compute fleet, discover
where to push Docker images, and release slots when done.

## Base URL

All endpoints are under `http://<this-host>:<port>/api/`. JSON request
and response bodies. No authentication.

## Typical Workflow

1. `GET /api/capabilities` — inspect the fleet. Hosts with `eligible: true`
   can currently accept a slot.
2. `POST /api/slots` with `{"caller": "<your-agent-id>"}` — reserve one.
   The response contains everything you need to orchestrate containers.
3. Use `host_address` with `ssh_user` for `DOCKER_HOST=ssh://...`.
   Use `compose_project` as your `-p` flag on every compose command.
   Use ports in the range `[port_range_start, port_range_end]`.
   Push and pull images from `registry_url`.
4. Heartbeat every 2 hours (well inside the 4-hour default TTL) via
   `POST /api/slots/{slot_id}/heartbeat`. If you skip this, your slot
   will be reaped and your containers orphaned.
5. When done, `DELETE /api/slots/{slot_id}`. Tear down your containers
   yourself — the server does not stop them.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | /api/capabilities | Current fleet state, registry status, per-host load |
| POST | /api/slots | Reserve a slot |
| GET | /api/slots | List active slots |
| GET | /api/slots/{id} | Inspect one slot |
| DELETE | /api/slots/{id} | Release a slot |
| POST | /api/slots/{id}/heartbeat | Extend TTL |
| GET | /api/agent-guide | This document |

## Port Range

Your slot owns `ports_per_slot` consecutive ports (currently {ports_per_slot}).
Map your services to any ports in `[port_range_start, port_range_end]`.
Ports outside this range are reserved for other slots — do not use them.

## Registry

URL: `{registry_url}`  (plain HTTP, no auth, no TLS)

Tag format: `{registry_url}/<your-project>/<service>:<tag>`

Before pushing or pulling, add `{registry_url}` to your Docker daemon's
`insecure-registries` list. On Colima: edit `~/.colima/default/colima.yaml`
under `docker.insecure-registries`.

## Error Handling

- `409` from `POST /api/slots` — no eligible host right now. Read the
  `detail` field for specifics (over-capacity, over-load, all offline).
  Retry after a delay or relax `host_hint`.
- `404` from `POST /api/slots/{id}/heartbeat` — your slot was reaped.
  Reserve a new one; your old containers must be torn down manually.
- `422` — your request violates a policy (e.g. `ttl_seconds` exceeds
  the configured maximum of {max_ttl_seconds} seconds).

## Current Configuration

Poll interval: {poll_interval}s
Default slot TTL: {default_ttl_seconds}s
Max slot TTL: {max_ttl_seconds}s
Placement thresholds: CPU ≤ {cpu_threshold}%, Memory ≤ {memory_threshold}%

## Full Specification

For the complete contract (slot object schema, placement algorithm,
SQLite schema, rationale) see `specs/slot-registry-and-cc-v0.1.md`
in the home-lab-monitor repo.
```

Placeholders in `{braces}` are substituted from `config.yml` and the derived registry URL at request time. The implementation reads these values once at server startup and caches them; no per-request config reload.

## 4. Schema / Interface Definition

### 4.1 Slot Object (canonical)

```json
{
  "slot_id":         "string (8-char hex)",
  "host":            "string",
  "host_address":    "string",
  "ssh_user":        "string | null",
  "registry_url":    "string | null",
  "compose_project": "string",
  "port_offset":     "integer",
  "port_range_start": "integer",
  "port_range_end":   "integer",
  "caller":          "string",
  "label":           "string | null",
  "created_at":      "integer (unix seconds)",
  "expires_at":      "integer (unix seconds)"
}
```

### 4.2 Capabilities Host Object

```json
{
  "name":            "string",
  "address":         "string",
  "ssh_user":        "string | null",
  "docker":          "boolean",
  "max_slots":       "integer",
  "active_slots":    "integer",
  "available_slots": "integer",
  "load": {
    "online":          "boolean",
    "last_seen":       "integer | null (unix seconds)",
    "cpu_percent":     "float | null",
    "memory_percent":  "float | null"
  },
  "eligible":        "boolean"
}
```

### 4.3 Updated `config.yml` Example

```yaml
server:
  port: 8888

registry:
  host: "Spark DGX"
  port: 5000

slot_policy:
  default_ttl_seconds: 14400
  max_ttl_seconds: 86400
  reap_interval_seconds: 3600
  cpu_threshold: 85
  memory_threshold: 85
  ports_per_slot: 20
  port_base: 20000

hosts:
  - name: "Mac Mini"
    address: "192.168.1.129"
    port: 9100
    docker: false
    max_slots: 0

  - name: "Spark DGX"
    address: "192.168.1.93"
    port: 9100
    docker: true
    max_slots: 2
    ssh_user: "admin"

  - name: "mbp15-1"
    address: "192.168.1.85"
    port: 9100
    docker: true
    max_slots: 1
    ssh_user: "admin"

  - name: "CRs-MacBook-Pro-2"
    address: "192.168.1.229"
    port: 9100
    docker: false
    max_slots: 0
```

### 4.4 Meridian Caller Integration

A Meridian development project calls this API instead of the Mac-D SSH slot scripts from `local-dev-and-precommit-v0.1.md`. Mapping:

| Meridian script | Home-lab-monitor API call |
| --- | --- |
| `docker info` SSH probe to find available Mac | `GET /api/capabilities` |
| `scripts/test-slot-start <slot-id>` | `POST /api/slots` |
| `scripts/test-slot-stop <slot-id>` | `DELETE /api/slots/{slot_id}` |
| `scripts/test-slot-reap` | Handled server-side automatically |
| Mac D lease record | `slots` row in `metrics.db` |
| `config/local-fleet.conf` | `config.yml` `hosts:` section |

The slot response provides everything Meridian's pre-commit hook needs:

```bash
SLOT_ID=$(curl -s -X POST http://hlab-server:8888/api/slots \
  -H 'Content-Type: application/json' \
  -d '{"caller":"meridian-pre-commit","label":"'$GIT_DIR'"}' \
  | jq -r .slot_id)

HOST_ADDR=$(curl -s http://hlab-server:8888/api/slots/$SLOT_ID | jq -r .host_address)
REGISTRY=$(curl -s http://hlab-server:8888/api/slots/$SLOT_ID | jq -r .registry_url)
PROJECT=$(curl -s http://hlab-server:8888/api/slots/$SLOT_ID | jq -r .compose_project)
SSH_USER=$(curl -s http://hlab-server:8888/api/slots/$SLOT_ID | jq -r .ssh_user)

DOCKER_HOST=ssh://$SSH_USER@$HOST_ADDR docker compose -p $PROJECT up -d
```

## 5. Constraints

1. **The server does not start, stop, or inspect containers.** It manages reservations only. Callers own the container lifecycle.
2. **Slot IDs must not be reused.** Once a slot ID is deleted (released or reaped), it is never reassigned. The 8-char hex space (4 billion values) makes collision vanishingly unlikely.
3. **Port ranges must not overlap on the same host.** The slot index assigned during placement must not conflict with any active slot on that host. The assignment query is: `SELECT port_offset FROM slots WHERE host = ? AND expires_at >= unixepoch()`.
4. **Placement must read from the live metrics DB, not from config alone.** Offline status and load gating (constraints 3 and 4 in §3.3) depend on the `metrics` table populated by the polling loop. A host that is configured but has not been polled in `2 × POLL_INTERVAL` seconds is treated as offline for placement purposes.
5. **Expired slots must be reaped automatically.** The server must not require manual cleanup. The reaper runs at intervals no longer than `reap_interval_seconds`.
6. **`caller` is required on `POST /api/slots`.** Anonymous reservations are not permitted — `caller` is used in logs and in the capabilities response to attribute active slots.
7. **`ttl_seconds` must not exceed `max_ttl_seconds`.** Validate at both `POST /api/slots` and `POST /api/slots/{id}/heartbeat`.
8. **The registry health check in `/api/capabilities` must be non-blocking.** Use an async HTTP GET with a short timeout (2 s). If it fails, return `"status": "offline"` — do not fail the capabilities response.
9. **No new Python dependencies may be added.** The slot API is implemented within the existing FastAPI + SQLite stack. No Redis, no task queue, no additional packages.
10. **Monitoring-only hosts (docker: false or max_slots: 0) must never appear as placement candidates.** They appear in the capabilities response for visibility but with `"eligible": false`.
11. **The `/api/agent-guide` response must stay in sync with the live API.** When an endpoint is added, removed, or changes its request/response shape, the guide template must be updated in the same commit. A stale guide mis-directs AI callers and is worse than no guide at all. The template lives alongside the route handler (e.g. `server/agent_guide.md.tmpl`) and is rendered with the current config at request time.
12. **The agent guide must not leak secrets or internal hostnames beyond what `/api/capabilities` already exposes.** It is reachable by anything on the LAN; treat its content as public within the trust boundary of the monitoring network.

## 6. Rationale

**Why build this into home-lab-monitor rather than a separate service?**
Home-lab-monitor already has the data needed for placement decisions (live CPU, memory, online status for every host) and the SQLite persistence layer. A separate service would need to replicate this data or add a dependency on the monitoring server. Merging avoids both.

**Why store lease records in SQLite rather than Mac D JSON files?**
The Meridian design used Mac D as a central coordination point because it had no other suitable service. Home-lab-monitor's server already plays that role — it is always on, accessible over the LAN, and owns the database. SQLite is the natural store.

**Why not have the server manage Docker Compose directly (via SSH)?**
The server is a Python process with no SSH dependency. Adding subprocess + SSH execution adds complexity, a new failure mode (SSH key management), and breaks the constraint that the server has no external dependencies. Callers already have SSH access to their fleet (Meridian's pre-commit hook already does `DOCKER_HOST=ssh://...`). Delegating container orchestration to the caller keeps each component responsible for what it understands.

**Why 8-char hex slot IDs rather than ULIDs or UUIDs?**
Consistency with Meridian's existing slot ID convention (`sha256sum | cut -c1-8`). Short IDs appear in Docker Compose project names, log lines, and URLs — brevity matters. The entropy (4 billion values) is more than sufficient for a single-developer fleet where slots rarely exceed a handful.

**Why inline port range in the slots table rather than a separate assignments table?**
Each slot gets exactly one contiguous range; there is no many-to-many relationship. Inline storage keeps queries simple and avoids a join on every placement or heartbeat request.

**Why piggyback the reaper on the existing poll loop rather than a separate task?**
The poll loop already runs on a predictable interval. A separate `asyncio.create_task` for reaping would need its own error handling and cancellation. Piggybacking on an existing loop with a time-since-last-reap check keeps the concurrency model simple.

**Alternatives considered:**

| Option | Rejected because |
| --- | --- |
| Separate fleet management service | Duplicates monitoring data; adds another process to deploy and maintain |
| Mac D JSON files (Meridian approach) | Requires SSH dependency in server; home-lab-monitor server is the better central authority |
| Server manages containers via SSH | Adds SSH key management, subprocess complexity, and a new failure mode to the server |
| UUID slot IDs | Longer than needed for display; inconsistent with Meridian's convention |
| Per-slot assignments table | Unnecessary join overhead for a strictly one-range-per-slot relationship |
| Redis or external lease store | Violates no-external-dependencies constraint |

## 7. Open Questions

- [ ] **Registry on dedicated host vs. shared host** — the registry currently shares a Docker host with compute slots. On a heavily-loaded DGX this could be a problem. Should the registry run on a non-slot host (e.g., a Mac that has Docker but `max_slots: 0`)? Impact: image push/pull availability during high-slot-utilization periods. Owner: config decision.
- [ ] **Slot affinity on retry** — if a caller's containers fail and the caller wants to retry on the same slot, there is currently no way to request the same slot ID. The caller can heartbeat the existing slot, but if it was reaped it must start fresh on whatever host is available. Should `POST /api/slots` accept an `existing_slot_id` to attempt reuse? Impact: debuggability on failure iteration. Owner: caller workflow decision.
- [ ] **Dashboard visibility** — should active slots appear on the home-lab-monitor dashboard (e.g., a "Slots" section on each host card)? The data is in SQLite; surfacing it is a UI-only change. Owner: UI decision.
- [ ] **Port base per host vs. global** — the spec allows a per-host `port_base` override, but the `port_range_start` derivation assumes a single global `port_base` in the slot placement logic. Clarify whether per-host override is needed or remove it. Owner: implementation decision.
- [ ] **`ssh_user` in config** — if `ssh_user` is absent from a host's config entry, the slot response returns `null`. The caller must then infer the SSH user some other way. Should the server require `ssh_user` for any docker-capable host? Impact: caller ergonomics. Owner: config convention decision.

## 8. Changelog

| Version | Date | Summary |
| --- | --- | --- |
| 0.2 | 2026-04-22 | §3.6 add `GET /api/agent-guide` endpoint — self-contained Markdown runbook for AI agents; content-negotiated JSON envelope; constraint to keep template in sync with the API |
| 0.1 | 2026-04-22 | Initial draft — slot reservation API, capabilities endpoint, registry integration, SQLite schema, reaper, and Meridian caller integration map |
