#!/usr/bin/env python3
"""Home Lab Monitor - Server

Polls configured agents every 60 seconds, stores metrics in SQLite,
and serves a read-only web dashboard on port 8888.

Usage:
    python server.py [config.yml]
    CONFIG_PATH=/path/to/config.yml python server.py
"""
import asyncio
import hashlib
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).parent))
from db import Database
from config import load_server_config, load_raw, SlotPolicyConfig
from placement import pick_host
from registry import make_registry_client

CONFIG_PATH = os.environ.get("CONFIG_PATH", sys.argv[1] if len(sys.argv) > 1 else "config.yml")
DB_PATH = os.environ.get("DB_PATH", "metrics.db")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 60))

_db: Database = None
_slot_lock = asyncio.Lock()
_registry_client = None
_last_reap_ts: float = 0.0


def load_config() -> dict:
    return load_raw(CONFIG_PATH)


# ---------------------------------------------------------------------------
# Background polling + reaper
# ---------------------------------------------------------------------------

async def poll_host(client: httpx.AsyncClient, host: dict, db: Database):
    name = host["name"]
    url = f"http://{host['address']}:{host.get('port', 9100)}/metrics"
    try:
        resp = await client.get(url, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        db.insert(name, data)
        print(f"[poll] {name} OK  cpu={data.get('cpu', {}).get('percent', '?')}%")
    except Exception as e:
        print(f"[poll] {name} OFFLINE — {e}")
        db.insert_offline(name)


async def polling_loop(db: Database):
    global _last_reap_ts
    print(f"[server] Polling every {POLL_INTERVAL}s")
    while True:
        try:
            config = load_config()
            hosts = config.get("hosts", [])
            if hosts:
                async with httpx.AsyncClient() as client:
                    await asyncio.gather(
                        *[poll_host(client, h, db) for h in hosts],
                        return_exceptions=True,
                    )
            db.prune_old()

            # Reap expired slots on every poll cycle
            reaped = db.delete_expired_slots()
            if reaped:
                print(f"[reaper] Removed {reaped} expired slot(s)")
            _last_reap_ts = time.time()

        except Exception as e:
            print(f"[poll] Error in polling loop: {e}")
        await asyncio.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _db, _registry_client
    _db = Database(DB_PATH)
    _db.init()

    server_cfg = load_server_config(CONFIG_PATH)
    _registry_client = make_registry_client(server_cfg.registry)

    task = asyncio.create_task(polling_loop(_db))
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Home Lab Monitor", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Metrics API routes (existing)
# ---------------------------------------------------------------------------

@app.get("/api/hosts")
def api_hosts():
    config = load_config()
    configured = [h["name"] for h in config.get("hosts", [])]
    known = _db.hosts()
    all_hosts = list(dict.fromkeys(configured + known))
    return JSONResponse({"hosts": all_hosts})


@app.get("/api/config")
def api_config():
    config = load_config()
    return JSONResponse({
        "hosts": [
            {
                "name": h["name"],
                "address": h["address"],
                "port": h.get("port", 9100),
                "role": h.get("role", "monitor"),
                "docker": h.get("docker", False),
                "ssh_user": h.get("ssh_user", ""),
            }
            for h in config.get("hosts", [])
        ],
        "poll_interval": POLL_INTERVAL,
    })


@app.get("/api/metrics/{host}/latest")
def api_latest(host: str):
    data = _db.latest(host)
    if data is None:
        raise HTTPException(404, f"No data for host '{host}'")
    return JSONResponse(data)


@app.get("/api/metrics/{host}/history")
def api_history(host: str):
    data = _db.history(host)
    return JSONResponse({"host": host, "points": data})


@app.get("/api/summary")
def api_summary():
    """Returns latest metrics for all configured hosts in one call."""
    config = load_config()
    result = {}
    for h in config.get("hosts", []):
        name = h["name"]
        latest = _db.latest(name)
        result[name] = latest if latest else {"_online": False, "_ts": None}
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------

@app.get("/api/capabilities")
async def api_capabilities():
    """Fleet capacity view: per-host availability + registry health."""
    server_cfg = load_server_config(CONFIG_PATH)
    sp = server_cfg.slot_policy

    hosts_out = []
    for hcfg in server_cfg.hosts:
        m = _db.latest(hcfg.name)
        online = bool(m and m.get("_online"))
        max_slots = hcfg.max_slots if hcfg.max_slots > 0 else sp.max_slots_per_host
        active = _db.count_active_slots_by_host(hcfg.name) if hcfg.docker else 0
        hosts_out.append({
            "name": hcfg.name,
            "address": hcfg.address,
            "role": hcfg.role,
            "online": online,
            "docker_enabled": hcfg.docker,
            "active_slots": active,
            "max_slots": max_slots if hcfg.docker else 0,
            "free_slots": max(0, max_slots - active) if hcfg.docker else 0,
            "cpu_percent": m.get("cpu", {}).get("percent") if online else None,
            "mem_percent": m.get("memory", {}).get("percent") if online else None,
        })

    registry_info = None
    if _registry_client:
        registry_info = await _registry_client.health()

    return JSONResponse({
        "hosts": hosts_out,
        "registry": registry_info,
        "slot_policy": {
            "default_ttl_seconds": sp.default_ttl_seconds,
            "port_base": sp.port_base,
            "port_stride": sp.port_stride,
            "max_slots_per_host": sp.max_slots_per_host,
            "max_port_offsets": sp.max_port_offsets,
        },
    })


# ---------------------------------------------------------------------------
# Slot reservation API
# ---------------------------------------------------------------------------

def _make_slot_id(caller: str, label: str) -> str:
    raw = f"{caller}|{label}|{time.time_ns()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:8]


def _slot_response(slot: dict, server_cfg) -> dict:
    sp = server_cfg.slot_policy
    host_cfg = next(
        (h for h in server_cfg.hosts if h.name == slot["host"]), None
    )
    port_range_start = sp.port_base + slot["port_offset"] * sp.port_stride
    now = int(time.time())
    return {
        "id": slot["id"],
        "host": slot["host"],
        "host_address": host_cfg.address if host_cfg else None,
        "caller": slot["caller"],
        "label": slot["label"],
        "port_base": sp.port_base,
        "port_offset": slot["port_offset"],
        "port_stride": sp.port_stride,
        "port_range_start": port_range_start,
        "created_ts": slot["created_ts"],
        "expires_ts": slot["expires_ts"],
        "expires_in_seconds": max(0, slot["expires_ts"] - now),
        "last_heartbeat_ts": slot["last_heartbeat_ts"],
        "meta": slot["meta"],
    }


@app.post("/api/slots", status_code=201)
async def api_create_slot(request: Request):
    body = await request.json()
    caller = body.get("caller", "").strip()
    if not caller:
        raise HTTPException(400, "caller is required")

    label = body.get("label", "")
    host_hint = body.get("host_hint")
    server_cfg = load_server_config(CONFIG_PATH)
    sp = server_cfg.slot_policy
    ttl = int(body.get("ttl_seconds", sp.default_ttl_seconds))
    meta = body.get("meta", {})

    async with _slot_lock:
        metrics_by_host = {
            hcfg.name: (_db.latest(hcfg.name) or {})
            for hcfg in server_cfg.hosts
        }
        slots_by_host = {
            hcfg.name: _db.count_active_slots_by_host(hcfg.name)
            for hcfg in server_cfg.hosts
        }
        used_offsets_by_host = {
            hcfg.name: _db.used_port_offsets(hcfg.name)
            for hcfg in server_cfg.hosts
        }

        chosen_host, port_offset, detail = pick_host(
            host_configs=server_cfg.hosts,
            metrics_by_host=metrics_by_host,
            slots_by_host=slots_by_host,
            policy=sp,
            used_offsets_by_host=used_offsets_by_host,
            host_hint=host_hint,
        )

        if chosen_host is None:
            raise HTTPException(409, f"No slot available: {detail}")

        # Retry on (rare) ID collision
        for _ in range(3):
            slot_id = _make_slot_id(caller, label)
            ok = _db.insert_slot(slot_id, chosen_host, caller, label,
                                  port_offset, ttl, meta)
            if ok:
                break
        else:
            raise HTTPException(500, "Failed to generate unique slot ID")

    slot = _db.get_slot(slot_id)
    return JSONResponse(_slot_response(slot, server_cfg), status_code=201)


@app.get("/api/slots")
def api_list_slots(host: Optional[str] = Query(None)):
    server_cfg = load_server_config(CONFIG_PATH)
    slots = _db.list_slots(host=host)
    return JSONResponse({
        "slots": [_slot_response(s, server_cfg) for s in slots],
        "count": len(slots),
    })


@app.get("/api/slots/{slot_id}")
def api_get_slot(slot_id: str):
    slot = _db.get_slot(slot_id)
    if slot is None:
        raise HTTPException(404, f"Slot '{slot_id}' not found")
    server_cfg = load_server_config(CONFIG_PATH)
    return JSONResponse(_slot_response(slot, server_cfg))


@app.delete("/api/slots/{slot_id}", status_code=204)
def api_delete_slot(slot_id: str):
    deleted = _db.delete_slot(slot_id)
    if not deleted:
        raise HTTPException(404, f"Slot '{slot_id}' not found")
    return JSONResponse(None, status_code=204)


@app.post("/api/slots/{slot_id}/heartbeat")
async def api_heartbeat(slot_id: str, request: Request):
    slot = _db.get_slot(slot_id)
    if slot is None:
        raise HTTPException(404, f"Slot '{slot_id}' not found")

    body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
    server_cfg = load_server_config(CONFIG_PATH)
    ttl = int(body.get("ttl_seconds", server_cfg.slot_policy.default_ttl_seconds))

    ok = _db.update_slot_expiry(slot_id, ttl)
    if not ok:
        raise HTTPException(404, f"Slot '{slot_id}' not found")

    slot = _db.get_slot(slot_id)
    return JSONResponse(_slot_response(slot, server_cfg))


# ---------------------------------------------------------------------------
# Dashboard (serve static HTML)
# ---------------------------------------------------------------------------

STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", response_class=HTMLResponse)
def dashboard():
    index = STATIC_DIR / "index.html"
    if not index.exists():
        return HTMLResponse("<h1>Dashboard not found</h1>", status_code=500)
    return HTMLResponse(index.read_text())


if __name__ == "__main__":
    import uvicorn
    config = load_config()
    port = config.get("server", {}).get("port", 8888)
    print(f"[server] Starting on http://0.0.0.0:{port}")
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
