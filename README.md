# Home Lab Monitor

Simple home lab monitoring dashboard. No auth, no cloud, 1-minute polling, 24-hour history. Supports slot reservation for Docker deployments.

Monitors: CPU, GPU (NVIDIA), memory, disk, I/O rates, and service health.

```
┌────────────────────────────────────────────────────┐
│  Home Lab Monitor                   Updated 14:22  │
├──────────────────────┬─────────────────────────────┤
│  AI Server  ●        │  NAS  ●                     │
│  CPU   ████░  67%    │  CPU   █░░░░   12%          │
│  MEM   ██████ 82%    │  MEM   ████░   61%          │
│  GPU   ███░░  55%    │  Disk  /: ████ 73%          │
│  VRAM  ██░░░  40%    │       /mnt: █░  18%         │
│  Disk  /: ██░  38%   │                             │
│  ● Ollama  ● Openhds │  ● Samba  ✗ Plex           │
└──────────────────────┴─────────────────────────────┘
  Click any host → 24-hour history charts
```

## Architecture

```
[Each host]  agent.py  →  :9100/metrics  (JSON)
                                ↑
[Monitor box]  server.py  polls every 60s  →  SQLite
                    ↓
              :8888  (dashboard + API)
```

## Quick Start

### 1. Install the agent on each host

```bash
# On each machine you want to monitor
pip install psutil pyyaml
pip install nvidia-ml-py   # optional, for NVIDIA GPU stats

# Copy agent files
scp agent/agent.py user@host:/opt/hlab-agent/
scp config/config.example.yml user@host:/opt/hlab-agent/config.yml
```

Edit `/opt/hlab-agent/config.yml` on each host:

```yaml
port: 9100
services:
  - name: "Ollama"
    type: systemd
    unit: ollama
  - name: "OpenHands"
    type: port
    port: 3000
  - name: "vLLM"
    type: port
    port: 8000
```

# Babysit discovery (optional — surfaces running babysit.sh instances)
# The agent scans these paths for .stop files and matching logs.
# NOTE: If the agent runs as a service account, you must configure explicit paths
# for each user who runs babysit.sh, otherwise instances won't appear.
babysit:
  scan_paths:
    - "~/sisyphus-logs"           # Default; expands to agent's $HOME
    # If agent runs as 'hlab-agent' but babysit runs as 'chris':
    # - "/home/chris/sisyphus-logs"
  include_last_action: false      # Default off (redacted for security)
```

Start the agent:
```bash
python /opt/hlab-agent/agent.py /opt/hlab-agent/config.yml
# Or install the systemd unit: systemd/hlab-agent.service
```

### 2. Set up the server (one machine, or your laptop)

```bash
pip install -r requirements.txt

# Edit config
cp config/config.example.yml config.yml
# Add your hosts' IPs to config.yml

# Start
cd server
python server.py ../config.yml
```

Open `http://localhost:8888` in your browser.

## Babysit Tab

The dashboard includes a **Babysit** tab that surfaces running `babysit.sh` instances across all hosts. The agent discovers instances by scanning configured paths for `.stop` files and matching log files.

**Configuration (agent-side):**

```yaml
babysit:
  scan_paths:
    - "~/sisyphus-logs"           # Default path
  include_last_action: false      # Set true to show last tool/text action (redacted by default)
```

**Important:** If the agent runs as a service account with a different `$HOME` than the user running `babysit.sh`, you must configure explicit paths or instances won't appear.

**Dashboard features:**

- Real-time state (running, backoff, crashed, finished)
- Iteration progress (N / MAX_ITER)
- Live backoff countdown when throttled
- Started-at timestamp and PID
- Termination reason for finished/crashed instances
- Optional last-action line (when `include_last_action: true`)


## Server API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Dashboard HTML |
| `GET` | `/api/hosts` | All known host names |
| `GET` | `/api/config` | Active host list and poll interval |
| `GET` | `/api/summary` | Latest metrics for all configured hosts |
| `GET` | `/api/metrics/{host}/latest` | Latest snapshot for one host |
| `GET` | `/api/metrics/{host}/history` | Up to 1440 data points (24 h) |
| `GET` | `/api/babysit` | Babysit instances from all hosts |

| `GET` | `/api/capabilities` | Fleet capacity + Docker slot availability + registry |
| `POST` | `/api/slots` | Reserve a deployment slot |
| `GET` | `/api/slots` | List active slots (optional `?host=` filter) |
| `GET` | `/api/slots/{id}` | Get a specific slot |
| `DELETE` | `/api/slots/{id}` | Release a slot |
| `POST` | `/api/slots/{id}/heartbeat` | Extend a slot's TTL |

## Agent API guide

This section is the canonical guide for AI agents and automated callers. It describes how to discover host capacity, reserve a deployment slot, and release it when done.

The values shown below (port `8888`, `default_ttl_seconds: 3600`, `port_base: 20000`, `port_stride: 10`, `poll_interval: 60s`) are the defaults from `config/config.example.yml`. For live values from a running server, fetch `/api/capabilities` — it returns the actual `slot_policy` and registry URL in use.

### 1. Discover available capacity

```
GET http://<server>:8888/api/capabilities
```

Returns per-host availability (online status, load, free slots) and registry info.

**Response shape:**
```json
{
  "hosts": [
    {
      "name": "string",
      "online": true,
      "docker_enabled": true,
      "active_slots": 0,
      "max_slots": 4,
      "free_slots": 4,
      "cpu_percent": 12.3,
      "mem_percent": 41.0
    }
  ],
  "registry": {
    "url": "http://host:5000",
    "healthy": true,
    "repository_count": 7,
    "repositories": ["myapp", "myapp-worker"]
  },
  "slot_policy": {
    "default_ttl_seconds": 3600,
    "port_base": 20000,
    "port_stride": 10
  }
}
```

### 2. Reserve a slot

```
POST http://<server>:8888/api/slots
Content-Type: application/json
```

**Request body:**
```json
{
  "caller": "my-agent-id",
  "label": "my-experiment",
  "ttl_seconds": 3600,
  "host_hint": "AI Server"
}
```

- `caller` (required) — unique ID for your agent or pipeline run
- `label` (optional) — human-readable tag for the reservation
- `ttl_seconds` (optional) — slot lifetime; defaults to `slot_policy.default_ttl_seconds` (3600s in the example config)
- `host_hint` (optional) — prefer a specific host by name

**Response (201):**
```json
{
  "id": "a1b2c3d4",
  "host": "AI Server",
  "host_address": "192.168.1.10",
  "port_base": 20000,
  "port_offset": 2,
  "port_range_start": 20020,
  "expires_ts": 1234567890,
  "expires_in_seconds": 3600
}
```

`port_range_start = port_base + port_offset * port_stride`

Map your container's host ports starting from `port_range_start` (you have `port_stride` ports available in this slot — 10 in the example config).

### 3. Extend a slot (heartbeat)

Send a heartbeat before the slot expires to extend its TTL:

```
POST http://<server>:8888/api/slots/{id}/heartbeat
Content-Type: application/json

{"ttl_seconds": 3600}
```

### 4. Release a slot

```
DELETE http://<server>:8888/api/slots/{id}
```

Always release your slot when done. Unreleased slots expire after TTL.

### 5. List active slots

```
GET http://<server>:8888/api/slots
GET http://<server>:8888/api/slots?host=AI+Server
```

### 6. Check a specific slot

```
GET http://<server>:8888/api/slots/{id}
```

### Quick-start workflow

1. `GET /api/capabilities` — find a host with `free_slots > 0`
2. `POST /api/slots` — reserve a slot, capture `id` and `port_range_start`
3. Deploy your container using ports `port_range_start` through `port_range_start + port_stride - 1` on `host_address`
4. `POST /api/slots/{id}/heartbeat` — extend TTL while running
5. `DELETE /api/slots/{id}` — release when done

### Notes

- Slots without heartbeats expire automatically after TTL.
- The reaper runs each poll cycle (`POLL_INTERVAL`, 60s by default).
- Image pull: `docker pull <registry_url>/<image>:<tag>` — get `registry_url` from `/api/capabilities`.

## Service check types

| type | description | required fields |
|------|-------------|-----------------|
| `systemd` | `systemctl is-active <unit>` | `unit` |
| `port` | TCP connect | `port`, optional `host` |
| `http` | HTTP GET (200 = up) | `url` or `port` |
| `process` | psutil process name match | `process` |

## Environment variables (server)

| var | default | description |
|-----|---------|-------------|
| `CONFIG_PATH` | `config.yml` | Path to config file |
| `DB_PATH` | `metrics.db` | SQLite database path |
| `POLL_INTERVAL` | `60` | Seconds between polls |

## Data retention

SQLite stores 24 hours of 1-minute snapshots (~1440 rows/host). Old data is pruned automatically after each poll cycle. Expired slots are reaped on each poll cycle.
