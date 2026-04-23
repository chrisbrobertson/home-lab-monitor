# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Home Lab Monitor** — a lightweight home lab monitoring dashboard. No auth, no cloud dependencies, 1-minute polling, 24-hour history.

Monitors: CPU, memory, disk, I/O rates (disk + network), NVIDIA GPU (optional), and service health checks.

**Status:** Functional and deployed. Architecture spec at `specs/home-lab-monitor-spec-v0.1.md`.

**Components:**

| Component | Role |
| --- | --- |
| Agent | Runs on each monitored host; collects metrics and serves them as JSON over HTTP |
| Server | Polls all agents on a 60-second interval; stores data in SQLite; serves the web dashboard and slot/registry APIs |
| Dashboard | Single-page HTML/JS frontend with Chart.js; dark-theme host cards with 24-hour history charts |
| Slot API | Reservation layer built into the server — callers discover fleet capacity, claim isolated Docker Compose slots, and get registry/port assignments |
| Image Registry | Docker Distribution (`registry:2`) running on a designated host; URL and health surfaced via `/api/capabilities` |

## Repository Structure

```
home-lab-monitor/
├── agent/
│   ├── agent.py            # Metric collector + HTTP server (runs on each monitored host)
│   └── requirements.txt    # Agent-only deps: psutil, pyyaml, nvidia-ml-py (optional)
├── server/
│   ├── server.py           # FastAPI polling server, monitoring API, and slot/registry API
│   ├── db.py               # SQLite database layer (metrics 24h retention, slots lease table)
│   └── static/
│       └── index.html      # Single-file dashboard (Chart.js, vanilla JS)
├── config/
│   └── config.example.yml  # Template for host, registry, and slot_policy configuration
├── scripts/
│   └── setup-dev-host.sh   # Idempotent macOS dev slot host bootstrap (see specs/dev-host-setup-v0.1.md)
├── systemd/                # Linux service units for agent and server
├── launchd/                # macOS plist files for agent and server (user + system variants)
├── templates/
│   └── spec-template.md    # Template for all specs in this repo
├── specs/                  # Architecture and decision specs
├── config.yml              # Active server config (hosts, server port, registry, slot_policy)
└── requirements.txt        # Server deps: fastapi, uvicorn, httpx, pyyaml
```

## Where Specs Live

Specs describe decisions and contracts that aren't obvious from reading the code. Since this is a small single-layer project, all specs live at `specs/`:

- **`specs/`** — architecture decisions, service contracts, configuration conventions, deployment patterns

Use the template at `templates/spec-template.md` when creating a new spec.

### Spec Filename and Version Convention

Spec filenames include a **stable version suffix** (e.g. `-v0.1.md`) chosen at file creation that **never changes**, even as the spec content is revised.

- **Frontmatter `version:` is authoritative** for the current content version. Bump minor for backward-compatible changes, major for breaking ones.
- **The filename suffix is stable.** Do not rename `home-lab-monitor-spec-v0.1.md` to `-v0.2.md` when the frontmatter bumps.
- **Only bump the filename suffix for MAJOR semantic replacements** (rare — e.g. a complete architectural rewrite).

## Python Conventions

- Agent and server are plain Python 3. No framework on the agent; FastAPI + uvicorn on the server.
- No type stubs required, but use type hints for new functions.
- `agent/agent.py` is a self-contained single file — keep it that way. It must run with only `psutil` and `pyyaml` installed (plus optional `nvidia-ml-py`).
- `server/` is split across `server.py` (routing + polling) and `db.py` (persistence). Keep these concerns separate.
- The dashboard is vanilla JS in a single `static/index.html` — no build step, no bundler.

## Configuration

`config.yml` at the repo root is the active server config. `config/config.example.yml` is the template.

Structure:
```yaml
server:
  port: 8888        # Dashboard and API port

registry:
  host: "Spark DGX"   # name of a configured host running Docker registry:2
  port: 5000

slot_policy:
  default_ttl_seconds: 14400   # 4 hours
  max_ttl_seconds: 86400       # 24 hours
  reap_interval_seconds: 3600
  cpu_threshold: 85
  memory_threshold: 85
  ports_per_slot: 20
  port_base: 20000

hosts:
  - name: "Host Name"
    address: "192.168.1.x"
    port: 9100
    role: server        # server | llm-server | dev-laptop | monitor (default: monitor)
    docker: true        # eligible for slot placement
    max_slots: 2        # max concurrent slots on this host
    ssh_user: "admin"   # returned to callers for DOCKER_HOST=ssh:// use
```

Each host runs `agent.py` (with its own per-host `config.yml` for service checks). Agent port defaults to 9100. The `role`, `docker`, `max_slots`, and `ssh_user` fields are optional. `role` defaults to `"monitor"` and controls dashboard grouping and which role-specific panels are shown (slot registry, active models, Colima status). The `docker` field defaults to `false` — hosts without it are monitoring-only and ineligible for slot placement.

## Slot and Registry API

The server exposes a reservation API on top of the monitoring data. Callers (e.g. Meridian pre-commit hooks) use this to discover available compute, claim a slot, and get connection details. Full contract in `specs/slot-registry-and-cc-v0.1.md`.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/capabilities` | Fleet-wide slot availability, registry URL/status, per-host load |
| `POST` | `/api/slots` | Reserve a slot; returns host, port range, compose project name, registry URL |
| `GET` | `/api/slots` | List all active (non-expired) slots |
| `GET` | `/api/slots/{slot_id}` | Get a single slot |
| `DELETE` | `/api/slots/{slot_id}` | Release a slot immediately |
| `POST` | `/api/slots/{slot_id}/heartbeat` | Extend a slot's TTL |

The full AI-agent-facing usage guide (endpoint reference, workflow, request/response shapes) lives in the **Agent API guide** section of `README.md`.

Slot placement uses live monitoring data from the metrics DB: hosts over `cpu_threshold` or `memory_threshold` (default 85%) are excluded regardless of available slot count. Expired slots are reaped automatically by the polling loop.

The image registry (`registry:2`) runs independently on a designated host. The server does not manage containers — it issues reservations and surfaces the registry URL. Container orchestration is the caller's responsibility.

## Service Check Types

| type | description | required fields | detail field |
| --- | --- | --- | --- |
| `systemd` | `systemctl is-active <unit>` | `unit` | — |
| `port` | TCP connect | `port`, optional `host` | — |
| `http` | HTTP GET (200 = up) | `url` or `port` | — |
| `process` | psutil process name match | `process` | — |
| `colima` | `colima status` exit code | — | — |
| `ollama` | `GET /api/ps` — up if reachable | optional `url` | comma-separated active model names |

The `detail` field is included in service check results only when non-null. The dashboard uses it to render the Active Models panel on `llm-server` host cards.

## Environment Variables (Server)

| var | default | description |
| --- | --- | --- |
| `CONFIG_PATH` | `config.yml` | Path to config file |
| `DB_PATH` | `metrics.db` | SQLite database path |
| `POLL_INTERVAL` | `60` | Seconds between polls |

## Testing

No test suite exists yet. When adding tests:

- Use `pytest` for unit tests.
- Agent metric collectors (`get_cpu`, `get_memory`, `get_disk`, etc.) are pure functions — test them directly.
- Server API routes can be tested with FastAPI's `TestClient`.
- Do not mock `psutil` unless testing error-path behavior — prefer calling real functions.

## Documentation

Every user-facing change (new config option, new service check type, new deployment step, changed default) must be reflected in `README.md`. `CLAUDE.md` is Claude Code guidance only — it is not a substitute for `README.md`.

## Versioning

This project uses [Semantic Versioning](https://semver.org/).

**When to bump:**
- **PATCH** — bug fixes, minor UI tweaks, docs-only changes
- **MINOR** — new metric types, new service check types, new API endpoints, new dashboard features
- **MAJOR** — breaking changes to the agent `/metrics` JSON schema, config file format, or database schema that require migration

No `VERSION` file exists yet. Add one when versioning becomes necessary.

## Committing

Commit after each logical work item. Commit messages should state *why*, not just what:

```
Add network I/O rates to agent metrics so the dashboard can show bandwidth
Fix disk partition deduplication to avoid double-counting bind mounts
```

Do not commit:
- `metrics.db` or any SQLite database file
- Any file containing secrets or credentials

## Key Architectural Constraints

- **Agent is pull-based.** The server polls agents; agents never push. This keeps agents stateless and simple.
- **Agent is dependency-minimal.** `agent.py` must run with only `psutil` + `pyyaml`. GPU support (`nvidia-ml-py`) is always optional.
- **No auth.** The dashboard and API are intentionally unauthenticated — this is a local-network-only tool.
- **SQLite only.** No external database. 24-hour retention with automatic pruning keeps the file small. The slot lease table lives in the same `metrics.db`.
- **Single-file dashboard.** `static/index.html` has no build step. Keep it that way — the deployment model is `python server.py`.
- **Agent JSON schema is the API contract.** Both `server.py` (consumer) and `agent.py` (producer) must stay in sync on the shape of `/metrics` output. Changes to the schema are MINOR or MAJOR version bumps depending on backward compatibility.
- **Server does not manage containers.** The slot API issues reservations and port assignments; the caller runs `docker compose`. No SSH execution, no subprocess container management in the server.
- **No new Python dependencies for the slot API.** Implemented within the existing FastAPI + SQLite stack.
- **Placement uses live metrics.** Slot placement decisions must read from the `metrics` table, not config alone. A host that has not been polled in `2 × POLL_INTERVAL` seconds is treated as offline regardless of config.
