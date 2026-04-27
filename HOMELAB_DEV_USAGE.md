# Home Lab Dev Slots — Usage Guide

A dev slot gives you a reserved Docker deployment target on a home lab machine: an isolated port range, an SSH path into a Colima runtime, and access to the shared image registry. Reserve a slot, push your image, run it, and release — the fleet handles the rest.

**Resource envelope per dev host:** Colima is configured with **12 GB RAM** and 4 CPUs. Each host supports up to 2 concurrent slots, so plan for up to ~6 GB RAM per slot when the host is fully loaded. CPU is generally not a bottleneck for development workloads.

---

## Monitor URL

All examples below use `MONITOR=http://192.168.1.129:8888`. Set this once:

```bash
MONITOR=http://192.168.1.129:8888
```

---

## 1. Check what's available

```bash
curl -s $MONITOR/api/capabilities
```

Each entry in `hosts` shows whether the machine is reachable and how many slots are free:

```json
{
  "hosts": [
    {
      "name": "C02D83TGMD6T",
      "online": true,
      "docker_enabled": true,
      "free_slots": 2,
      "cpu_percent": 12.4,
      "mem_percent": 58.1
    }
  ],
  "registry": {
    "url": "http://192.168.1.93:5000",
    "healthy": true
  },
  "slot_policy": {
    "default_ttl_seconds": 3600,
    "port_base": 20000,
    "port_stride": 10
  }
}
```

Print only the available hosts:

```bash
curl -s $MONITOR/api/capabilities | python3 -c "
import json, sys
d = json.load(sys.stdin)
for h in d['hosts']:
    if h['docker_enabled'] and h['free_slots'] > 0 and h['online']:
        print(h['name'], '—', h['free_slots'], 'free slots,',
              h['cpu_percent'], '% CPU,', h['mem_percent'], '% MEM')
"
```

`mem_percent` is the host machine's memory usage. High values here mean the host OS itself is under load; it does not directly cap the Colima VM's 12 GB.

---

## 2. Reserve a slot

```bash
SLOT=$(curl -s -X POST $MONITOR/api/slots \
  -H "Content-Type: application/json" \
  -d '{"caller": "my-tool", "label": "experiment-1", "ttl_seconds": 3600}')
echo $SLOT
```

`caller` is required. `label` is optional but helps identify the slot in the dashboard. `ttl_seconds` defaults to 3600 (1 hour).

Extract the fields you need:

```bash
SLOT_ID=$(echo $SLOT      | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
HOST_ADDR=$(echo $SLOT    | python3 -c "import json,sys; print(json.load(sys.stdin)['host_address'])")
SSH_USER=$(echo $SLOT     | python3 -c "import json,sys; print(json.load(sys.stdin)['ssh_user'])")
PORT=$(echo $SLOT         | python3 -c "import json,sys; print(json.load(sys.stdin)['port_range_start'])")
REGISTRY=$(echo $SLOT     | python3 -c "import json,sys; print(json.load(sys.stdin)['registry_url'])")
PROJECT=$(echo $SLOT      | python3 -c "import json,sys; print(json.load(sys.stdin)['compose_project'])")
```

**Full response fields:**

| Field | Description |
|-------|-------------|
| `id` | Slot ID (8-char hex) — use for heartbeat and release |
| `host` / `host_address` | Hostname and IP of the assigned machine |
| `ssh_user` | Username for `DOCKER_HOST=ssh://` |
| `registry_url` | Image registry base URL (e.g. `http://192.168.1.93:5000`) |
| `compose_project` | Suggested container/project name (`hlab-<id>`) |
| `port_range_start` | First port in your reserved block |
| `port_range_end` | Last port in your reserved block (stride of 10) |
| `expires_in_seconds` | Seconds until the slot expires |
| `meta` | Your freeform JSON passthrough (echoed back as-is) |

### Passing metadata

`meta` is a freeform JSON object you can attach to a slot for tracking:

```bash
curl -s -X POST $MONITOR/api/slots \
  -H "Content-Type: application/json" \
  -d '{
    "caller": "my-tool",
    "label": "pr-1234",
    "ttl_seconds": 7200,
    "meta": {"pr": 1234, "branch": "feature/xyz", "triggered_by": "pre-commit"}
  }'
```

---

## 3. Run a container

The assigned host runs Colima. Access its Docker daemon over SSH:

```bash
export DOCKER_HOST="ssh://${SSH_USER}@${HOST_ADDR}"

# Pull/run your image — use port_range_start for the host-side port
docker run -d \
  --name "$PROJECT" \
  -p "${PORT}:8080" \
  --memory="5g" \
  -e "SLOT_ID=${SLOT_ID}" \
  "${REGISTRY}/my-image:latest"
```

Set `--memory` to stay within the 12 GB Colima budget (leave headroom for the OS and other slots).

### Push an image first

If you're building locally and pushing to the lab registry:

```bash
IMAGE="${REGISTRY}/my-image:${SLOT_ID}"
docker build -t "$IMAGE" ./my-app
docker push "$IMAGE"   # registry uses plain HTTP — configure insecure-registries first (see below)

# Then run it on the dev host
DOCKER_HOST="ssh://${SSH_USER}@${HOST_ADDR}" docker run -d \
  --name "$PROJECT" -p "${PORT}:8080" "$IMAGE"
```

### Port ranges

Each slot has a block of 10 ports: `[port_range_start, port_range_end]`. For a single-service workload use `port_range_start`. For multiple services:

```bash
APP_PORT=$PORT               # port_range_start + 0
DB_PORT=$((PORT + 1))        # port_range_start + 1
PROXY_PORT=$((PORT + 2))     # port_range_start + 2
```

### Verify the container is reachable

```bash
curl -s "http://${HOST_ADDR}:${PORT}/health"
```

---

## 4. Release the slot

Stop the container first, then release:

```bash
DOCKER_HOST="ssh://${SSH_USER}@${HOST_ADDR}" docker rm -f "$PROJECT"
curl -s -X DELETE "$MONITOR/api/slots/$SLOT_ID"
```

The slot is released immediately and the ports become available to other callers.

---

## 5. Keep a slot alive (long-running jobs)

Slots expire after `ttl_seconds` (default: 1 hour). For longer workloads, send a heartbeat periodically:

```bash
curl -s -X POST "$MONITOR/api/slots/$SLOT_ID/heartbeat" \
  -H "Content-Type: application/json" \
  -d '{"ttl_seconds": 3600}'
```

Call this every 30 minutes or so. If the slot has already expired, the heartbeat returns 404 — clean up the container and re-reserve if needed.

If you don't send heartbeats, the slot expires automatically and the reaper removes it. Containers are **not** stopped automatically — clean up your containers before or after the slot is released.

---

## 6. Errors

| Code | Meaning | What to do |
|------|---------|-----------|
| `409` | No eligible host available | Check `GET /api/capabilities` — hosts may be offline, at capacity, or over 80% CPU / 85% MEM |
| `404` | Slot not found | Slot expired or was already released — clean up containers and re-reserve |
| `400` | `caller` field missing | Include `"caller": "your-tool-name"` in the request body |

---

## Prerequisites

Before running the workflow above, the calling machine needs:

**SSH key on dev hosts** — your public key must be in `~/.ssh/authorized_keys` on each dev host. Verify with:

```bash
ssh chrisrobertson@192.168.1.81 echo ok
```

**Insecure registry** — the lab registry at `192.168.1.93:5000` uses plain HTTP. Add it to your local Docker daemon's insecure-registries:

- **Colima:** add to `~/.colima/default/colima.yaml` and restart: `colima restart`

  ```yaml
  docker:
    insecure-registries:
      - 192.168.1.93:5000
  ```

- **Docker Desktop:** Settings → Docker Engine → add to `"insecure-registries"` array.

- **Linux Docker daemon:** add to `/etc/docker/daemon.json` and restart: `sudo systemctl restart docker`

  ```json
  { "insecure-registries": ["192.168.1.93:5000"] }
  ```

---

## Full runnable example

See `examples/slot-workflow.sh` for a complete end-to-end shell script covering all steps with error handling and cleanup on exit.
