---
openapi: "3.0"
info:
  title: "Home Lab Monitor — Architecture Spec"
  version: "0.1"
  status: "draft"
  authors: []
  updated: "2026-04-22"
  scope: "System-level architecture for Home Lab Monitor — agent/server split, metrics contract, storage model, dashboard design, and deployment model"
  owner: "specs"
  components:
    - "agent"
    - "server"
    - "server/static"
---

# Home Lab Monitor — Architecture Spec

> Authoritative reference for architecture decisions and component contracts. Read this before making structural changes to the agent, server, or dashboard.

## 1. Scope

This spec governs:

- The agent/server/dashboard component split and responsibilities
- The `/metrics` JSON contract between agent and server
- The SQLite storage model and data retention policy
- The server API surface
- The deployment model for agent and server

Out of scope: service check implementation details (covered by the service check types table in `CLAUDE.md`), dashboard visual design, and OS-specific packaging.

## 2. Context

Home Lab Monitor is a local-network monitoring tool for a personal home lab. The design priorities, in order, are:

1. **Minimal install surface** — agents must run with two pip packages (`psutil`, `pyyaml`).
2. **No external dependencies** — no cloud services, no external databases, no auth infrastructure.
3. **Simple deployment** — `python agent.py` and `python server.py` is the full story.
4. **Good-enough data** — 1-minute polling and 24-hour history is sufficient; sub-second precision is not a goal.

These priorities drive every architectural constraint below.

## 3. Decision / Specification

### 3.1 Component Responsibilities

**Agent** (`agent/agent.py`) runs on each monitored host. It:

- Collects system metrics on demand (no background caching)
- Serves metrics as JSON at `GET /metrics`
- Serves `GET /health` as a liveness probe
- Checks configured services (systemd, port, http, process) synchronously on each `/metrics` request
- Has no knowledge of the server or any other agent

**Server** (`server/server.py`) runs on one machine (typically the monitoring workstation or a dedicated box). It:

- Polls all configured agents every `POLL_INTERVAL` seconds (default 60)
- Stores each poll result in SQLite via `server/db.py`
- Prunes data older than 24 hours after each poll cycle
- Serves the dashboard at `GET /`
- Exposes a JSON API for the dashboard (see §3.3)

**Dashboard** (`server/static/index.html`) is a single-page application. It:

- Fetches data from the server API (never directly from agents)
- Displays a card per host with current metrics and service status
- Renders 24-hour history charts (Chart.js) for each host on expansion
- Auto-refreshes on a countdown timer

### 3.2 Agent `/metrics` JSON Contract

The agent outputs a flat JSON object at `GET /metrics`. This is the API contract between agent and server. Both sides must be kept in sync.

```json
{
  "hostname": "<string>",
  "timestamp": "<integer, unix seconds>",
  "cpu": {
    "percent": "<float>",
    "count_logical": "<integer>",
    "count_physical": "<integer>",
    "freq_mhz": "<float | null>",
    "load_avg_1m": "<float>",
    "load_avg_5m": "<float>",
    "load_avg_15m": "<float>"
  },
  "memory": {
    "total_gb": "<float>",
    "used_gb": "<float>",
    "available_gb": "<float>",
    "percent": "<float>",
    "swap_total_gb": "<float>",
    "swap_used_gb": "<float>",
    "swap_percent": "<float>"
  },
  "disk": [
    {
      "mountpoint": "<string>",
      "device": "<string>",
      "fstype": "<string>",
      "total_gb": "<float>",
      "used_gb": "<float>",
      "free_gb": "<float>",
      "percent": "<float>"
    }
  ],
  "io": {
    "disk": {
      "read_mbps": "<float>",
      "write_mbps": "<float>",
      "read_total_gb": "<float>",
      "write_total_gb": "<float>"
    },
    "network": {
      "recv_mbps": "<float>",
      "sent_mbps": "<float>",
      "recv_total_gb": "<float>",
      "sent_total_gb": "<float>"
    }
  },
  "gpu": [
    {
      "index": "<integer>",
      "name": "<string>",
      "util_percent": "<integer>",
      "mem_used_gb": "<float>",
      "mem_total_gb": "<float>",
      "mem_percent": "<float>",
      "temp_c": "<integer>",
      "power_w": "<float | null>",
      "power_limit_w": "<float | null>"
    }
  ],
  "services": [
    {
      "name": "<string>",
      "up": "<boolean>",
      "type": "<string: systemd | port | http | process>"
    }
  ]
}
```

**Notes:**

- `gpu` is an empty array `[]` when no NVIDIA GPU is present or `nvidia-ml-py` is not installed.
- `io` rates (`read_mbps`, `write_mbps`, `recv_mbps`, `sent_mbps`) are `0.0` on the first request because rate calculation requires two samples.
- A GPU entry may have an `"error"` key instead of the normal fields if NVML returns an error for that device index.
- `disk` excludes pseudo-filesystems: `tmpfs`, `devtmpfs`, `squashfs`, `overlay`, `proc`, `sysfs`.

### 3.3 Server API Surface

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/` | Dashboard HTML |
| `GET` | `/api/hosts` | List of all known host names (configured + seen in DB) |
| `GET` | `/api/config` | Active host list and poll interval (no secrets) |
| `GET` | `/api/summary` | Latest metrics snapshot for all configured hosts in one call |
| `GET` | `/api/metrics/{host}/latest` | Latest snapshot for a single host |
| `GET` | `/api/metrics/{host}/history` | Up to 1440 data points (24h) for a single host, oldest-first |

The dashboard uses `/api/summary` for the main view and `/api/metrics/{host}/history` for the detail chart view.

### 3.4 Storage Model

SQLite database (`metrics.db`, location configurable via `DB_PATH`).

**Schema:**

```sql
CREATE TABLE metrics (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    host    TEXT    NOT NULL,
    ts      INTEGER NOT NULL,         -- unix seconds
    online  INTEGER NOT NULL DEFAULT 1,  -- 1 = data present, 0 = agent unreachable
    data    TEXT    NOT NULL DEFAULT '{}'  -- full /metrics JSON blob
);
CREATE INDEX idx_host_ts ON metrics (host, ts);
```

**Retention:** rows older than 86400 seconds (24 hours) are deleted after each poll cycle via `DELETE WHERE ts < cutoff`. This keeps the database at approximately 1440 rows per host.

**Offline rows:** when an agent is unreachable, an `online=0` row with empty `data` is inserted. This preserves the gap in history charts rather than treating offline periods as missing data.

### 3.5 Deployment Model

**Agent:** `python agent.py [config.yml]`

- Listens on `0.0.0.0:9100` (configurable via `port:` in config)
- One instance per monitored host
- Service files: `systemd/hlab-agent.service` (Linux), `launchd/com.homelab.monitor.agent.plist` (macOS)

**Server:** `python server.py [config.yml]` from the `server/` directory, or `CONFIG_PATH=../config.yml python server.py`

- Listens on the port specified in `config.yml` under `server.port` (default 8080, currently configured as 8888)
- One instance; runs wherever the dashboard is accessed from
- Service files: `systemd/hlab-server.service` (Linux), `launchd/com.homelab.monitor.server.plist` (macOS)

## 5. Constraints

1. `agent/agent.py` must be a single self-contained file with no imports outside the Python standard library, `psutil`, and `pyyaml`. GPU support is the only permitted optional dependency.
2. The `/metrics` JSON schema (§3.2) is the API contract. Removing or renaming fields is a MINOR or MAJOR version bump depending on whether the server can handle the absence gracefully.
3. The server must never connect directly to monitored hosts for anything other than polling `GET /metrics`. No SSH, no SNMP, no agent push.
4. The SQLite database must not be committed to the repository.
5. `config.yml` at the repo root contains real IP addresses and must not be committed. Use `config/config.example.yml` as the public reference.
6. The dashboard must function with no build step — `static/index.html` is the deployable artifact as-is.

## 6. Rationale

**Alternatives considered:**

| Option | Rejected because |
| --- | --- |
| Prometheus + Grafana | Far heavier install; requires persistent services and complex config for a personal lab |
| Push-based agents | Requires the server to be reachable from every host; pull is simpler when the server is the initiator |
| PostgreSQL or other external DB | Violates the no-external-dependencies constraint; SQLite is sufficient for 1440 rows/host |
| React/Next.js dashboard | Build step adds deployment complexity; a single HTML file is simpler to serve and edit |
| Per-metric DB columns | JSON blob storage is simpler to evolve — adding a new metric field requires no schema migration |

## 7. Open Questions

- [ ] Disk partition filtering is hardcoded — should it be configurable per host? Currently excludes `tmpfs`, `devtmpfs`, `squashfs`, `overlay`, `proc`, `sysfs`.
- [ ] The server port in `config.yml` (8888) differs from the README default (8080). Pick one and update the other.
- [ ] No `.gitignore` exists yet — `metrics.db`, `__pycache__/`, and `*.pyc` should be excluded.
- [ ] No authentication. Acceptable for local-network use, but worth noting if the server is ever exposed beyond the LAN.

## 8. Changelog

| Version | Date | Summary |
| --- | --- | --- |
| 0.1 | 2026-04-22 | Initial draft — documents existing implementation |
