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

## Server API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Dashboard HTML |
| `GET` | `/api/hosts` | All known host names |
| `GET` | `/api/config` | Active host list and poll interval |
| `GET` | `/api/summary` | Latest metrics for all configured hosts |
| `GET` | `/api/metrics/{host}/latest` | Latest snapshot for one host |
| `GET` | `/api/metrics/{host}/history` | Up to 1440 data points (24 h) |
| `GET` | `/api/capabilities` | Fleet capacity + Docker slot availability + registry |
| `POST` | `/api/slots` | Reserve a deployment slot |
| `GET` | `/api/slots` | List active slots (optional `?host=` filter) |
| `GET` | `/api/slots/{id}` | Get a specific slot |
| `DELETE` | `/api/slots/{id}` | Release a slot |
| `POST` | `/api/slots/{id}/heartbeat` | Extend a slot's TTL |
| `GET` | `/api/agent-guide` | Markdown usage guide for AI agents |

## Slot reservation workflow

Slots give AI agents or pipelines a reserved Docker Compose port range on a host:

```bash
# 1. Discover capacity
curl http://localhost:8888/api/capabilities

# 2. Reserve a slot
curl -X POST http://localhost:8888/api/slots \
  -H 'Content-Type: application/json' \
  -d '{"caller": "my-agent", "label": "experiment-1", "ttl_seconds": 3600}'

# 3. Deploy on host_address:port_range_start
# 4. Heartbeat while running
curl -X POST http://localhost:8888/api/slots/<id>/heartbeat \
  -H 'Content-Type: application/json' \
  -d '{"ttl_seconds": 3600}'

# 5. Release when done
curl -X DELETE http://localhost:8888/api/slots/<id>
```

For the full guide (intended for AI agent consumption):

```bash
curl http://localhost:8888/api/agent-guide
```

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
