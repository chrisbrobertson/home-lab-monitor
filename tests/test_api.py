"""Integration tests for server API endpoints via httpx.AsyncClient + ASGITransport."""
import os
import sys
import time
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))


@pytest.fixture
def seeded_db(minimal_config, tmp_path):
    """Fresh DB pre-seeded with metrics for host-a."""
    from db import Database
    db_path = str(tmp_path / "api_test.db")
    db = Database(db_path)
    db.init()
    now = int(time.time())
    db.insert("host-a", {
        "hostname": "host-a", "timestamp": now,
        "cpu": {"percent": 15.0, "count_logical": 8, "count_physical": 4,
                "freq_mhz": 3200.0, "load_avg_1m": 0.5,
                "load_avg_5m": 0.4, "load_avg_15m": 0.3},
        "memory": {"total_gb": 32.0, "used_gb": 8.0, "available_gb": 24.0,
                   "percent": 25.0, "swap_total_gb": 0.0,
                   "swap_used_gb": 0.0, "swap_percent": 0.0},
        "disk": [],
        "io": {"disk": {"read_mbps": 0.0, "write_mbps": 0.0,
                        "read_total_gb": 0.0, "write_total_gb": 0.0},
               "network": {"recv_mbps": 0.0, "sent_mbps": 0.0,
                           "recv_total_gb": 0.0, "sent_total_gb": 0.0}},
        "gpu": [], "services": [],
    })
    return db


@pytest.fixture(autouse=False)
def patched_server(seeded_db, minimal_config, monkeypatch):
    """Patch module-level globals in server so tests don't need a running process."""
    import server as srv

    monkeypatch.setattr(srv, "CONFIG_PATH", minimal_config)
    monkeypatch.setattr(srv, "_db", seeded_db)
    monkeypatch.setattr(srv, "_registry_client", None)

    async def _always_healthy(address, port, client):
        return True
    monkeypatch.setattr(srv, "_probe_host_health", _always_healthy)

    return srv


@pytest.mark.asyncio
async def test_hosts_endpoint(patched_server):
    import httpx
    from httpx import ASGITransport
    async with httpx.AsyncClient(
        transport=ASGITransport(app=patched_server.app), base_url="http://test"
    ) as client:
        r = await client.get("/api/hosts")
    assert r.status_code == 200
    assert "host-a" in r.json()["hosts"]


@pytest.mark.asyncio
async def test_config_endpoint(patched_server):
    import httpx
    from httpx import ASGITransport
    async with httpx.AsyncClient(
        transport=ASGITransport(app=patched_server.app), base_url="http://test"
    ) as client:
        r = await client.get("/api/config")
    assert r.status_code == 200
    assert any(h["name"] == "host-a" for h in r.json()["hosts"])


@pytest.mark.asyncio
async def test_summary_endpoint(patched_server):
    import httpx
    from httpx import ASGITransport
    async with httpx.AsyncClient(
        transport=ASGITransport(app=patched_server.app), base_url="http://test"
    ) as client:
        r = await client.get("/api/summary")
    assert r.status_code == 200
    assert "host-a" in r.json()


@pytest.mark.asyncio
async def test_latest_endpoint(patched_server):
    import httpx
    from httpx import ASGITransport
    async with httpx.AsyncClient(
        transport=ASGITransport(app=patched_server.app), base_url="http://test"
    ) as client:
        r = await client.get("/api/metrics/host-a/latest")
    assert r.status_code == 200
    assert r.json()["_online"] is True


@pytest.mark.asyncio
async def test_latest_missing_host(patched_server):
    import httpx
    from httpx import ASGITransport
    async with httpx.AsyncClient(
        transport=ASGITransport(app=patched_server.app), base_url="http://test"
    ) as client:
        r = await client.get("/api/metrics/does-not-exist/latest")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_history_endpoint(patched_server):
    import httpx
    from httpx import ASGITransport
    async with httpx.AsyncClient(
        transport=ASGITransport(app=patched_server.app), base_url="http://test"
    ) as client:
        r = await client.get("/api/metrics/host-a/history")
    assert r.status_code == 200
    assert "points" in r.json()


@pytest.mark.asyncio
async def test_capabilities_endpoint(patched_server):
    import httpx
    from httpx import ASGITransport
    async with httpx.AsyncClient(
        transport=ASGITransport(app=patched_server.app), base_url="http://test"
    ) as client:
        r = await client.get("/api/capabilities")
    assert r.status_code == 200
    data = r.json()
    assert "hosts" in data
    assert "slot_policy" in data
    host_a = next((h for h in data["hosts"] if h["name"] == "host-a"), None)
    assert host_a is not None
    assert host_a["docker_enabled"] is True


@pytest.mark.asyncio
async def test_slot_lifecycle(patched_server):
    import httpx
    from httpx import ASGITransport
    async with httpx.AsyncClient(
        transport=ASGITransport(app=patched_server.app), base_url="http://test"
    ) as client:
        # Create
        r = await client.post(
            "/api/slots",
            json={"caller": "test-agent", "label": "ci-run"},
        )
        assert r.status_code == 201
        slot = r.json()
        slot_id = slot["id"]
        assert slot["host"] in ("host-a", "host-b")
        assert slot["port_range_start"] >= 20000

        # Get
        r = await client.get(f"/api/slots/{slot_id}")
        assert r.status_code == 200
        assert r.json()["id"] == slot_id

        # List
        r = await client.get("/api/slots")
        assert r.status_code == 200
        ids = [s["id"] for s in r.json()["slots"]]
        assert slot_id in ids

        # Heartbeat
        r = await client.post(
            f"/api/slots/{slot_id}/heartbeat",
            json={"ttl_seconds": 1800},
        )
        assert r.status_code == 200

        # Delete
        r = await client.delete(f"/api/slots/{slot_id}")
        assert r.status_code == 204

        # Confirm gone
        r = await client.get(f"/api/slots/{slot_id}")
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_slot_missing_caller(patched_server):
    import httpx
    from httpx import ASGITransport
    async with httpx.AsyncClient(
        transport=ASGITransport(app=patched_server.app), base_url="http://test"
    ) as client:
        r = await client.post("/api/slots", json={"label": "no-caller"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_slot_host_hint(patched_server, seeded_db):
    """host-b has no metrics → but it's still docker-eligible. Seed host-b metrics."""
    now = int(time.time())
    seeded_db.insert("host-b", {
        "hostname": "host-b", "timestamp": now,
        "cpu": {"percent": 5.0, "count_logical": 4, "count_physical": 2,
                "freq_mhz": 2400.0, "load_avg_1m": 0.1,
                "load_avg_5m": 0.1, "load_avg_15m": 0.1},
        "memory": {"total_gb": 16.0, "used_gb": 2.0, "available_gb": 14.0,
                   "percent": 12.0, "swap_total_gb": 0.0,
                   "swap_used_gb": 0.0, "swap_percent": 0.0},
        "disk": [], "io": {"disk": {"read_mbps": 0.0, "write_mbps": 0.0,
                                    "read_total_gb": 0.0, "write_total_gb": 0.0},
                           "network": {"recv_mbps": 0.0, "sent_mbps": 0.0,
                                       "recv_total_gb": 0.0, "sent_total_gb": 0.0}},
        "gpu": [], "services": [],
    })
    import httpx
    from httpx import ASGITransport
    async with httpx.AsyncClient(
        transport=ASGITransport(app=patched_server.app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/api/slots",
            json={"caller": "agent", "host_hint": "host-b"},
        )
    assert r.status_code == 201
    assert r.json()["host"] == "host-b"


@pytest.mark.asyncio
async def test_slot_host_hint_unavailable(patched_server):
    import httpx
    from httpx import ASGITransport
    async with httpx.AsyncClient(
        transport=ASGITransport(app=patched_server.app), base_url="http://test"
    ) as client:
        # host-c is not docker-enabled
        r = await client.post(
            "/api/slots",
            json={"caller": "agent", "host_hint": "host-c"},
        )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_delete_missing_slot(patched_server):
    import httpx
    from httpx import ASGITransport
    async with httpx.AsyncClient(
        transport=ASGITransport(app=patched_server.app), base_url="http://test"
    ) as client:
        r = await client.delete("/api/slots/notexist")
    assert r.status_code == 404
