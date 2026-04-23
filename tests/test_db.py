"""Unit tests for Database — metrics and slots layers."""
import time

import pytest


def test_init_creates_tables(tmp_db):
    conn = tmp_db._get_conn()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "metrics" in tables
    assert "slots" in tables


def test_insert_and_latest(tmp_db):
    data = {"hostname": "box", "timestamp": int(time.time()), "cpu": {"percent": 42.0}}
    tmp_db.insert("box", data)
    result = tmp_db.latest("box")
    assert result["_online"] is True
    assert result["cpu"]["percent"] == 42.0


def test_insert_offline(tmp_db):
    tmp_db.insert_offline("box")
    result = tmp_db.latest("box")
    assert result["_online"] is False


def test_prune_old_removes_stale(tmp_db):
    conn = tmp_db._get_conn()
    old_ts = int(time.time()) - 90000
    conn.execute(
        "INSERT INTO metrics (host, ts, online, data) VALUES (?, ?, 1, '{}')",
        ("box", old_ts)
    )
    conn.commit()
    tmp_db.prune_old()
    assert tmp_db.latest("box") is None


def test_hosts_lists_distinct(tmp_db):
    tmp_db.insert("alpha", {"timestamp": int(time.time())})
    tmp_db.insert("alpha", {"timestamp": int(time.time())})
    tmp_db.insert("beta", {"timestamp": int(time.time())})
    assert set(tmp_db.hosts()) == {"alpha", "beta"}


# --- slot tests ---

def test_insert_slot_success(tmp_db):
    ok = tmp_db.insert_slot("abc12345", "host-a", "agent1", "run1", 0, 3600, {})
    assert ok is True
    slot = tmp_db.get_slot("abc12345")
    assert slot is not None
    assert slot["host"] == "host-a"
    assert slot["port_offset"] == 0


def test_insert_slot_collision_returns_false(tmp_db):
    tmp_db.insert_slot("dup00000", "host-a", "agent1", "run1", 0, 3600, {})
    ok = tmp_db.insert_slot("dup00000", "host-a", "agent2", "run2", 1, 3600, {})
    assert ok is False


def test_delete_slot(tmp_db):
    tmp_db.insert_slot("del12345", "host-a", "agent1", "r", 0, 3600, {})
    deleted = tmp_db.delete_slot("del12345")
    assert deleted is True
    assert tmp_db.get_slot("del12345") is None


def test_delete_slot_missing_returns_false(tmp_db):
    assert tmp_db.delete_slot("notexist") is False


def test_update_slot_expiry(tmp_db):
    tmp_db.insert_slot("hb000001", "host-a", "a", "l", 0, 3600, {})
    slot_before = tmp_db.get_slot("hb000001")
    ok = tmp_db.update_slot_expiry("hb000001", 7200)
    assert ok is True
    slot_after = tmp_db.get_slot("hb000001")
    assert slot_after["expires_ts"] > slot_before["expires_ts"]


def test_delete_expired_slots(tmp_db):
    now = int(time.time())
    conn = tmp_db._get_conn()
    conn.execute(
        """INSERT INTO slots (id, host, caller, label, port_offset,
           created_ts, expires_ts, last_heartbeat_ts, meta)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}')""",
        ("exp00001", "host-a", "a", "l", 0, now - 7200, now - 3600, now - 7200)
    )
    conn.commit()
    count = tmp_db.delete_expired_slots()
    assert count == 1
    assert tmp_db.get_slot("exp00001") is None


def test_list_slots_all(tmp_db):
    tmp_db.insert_slot("ls000001", "host-a", "a", "l", 0, 3600, {})
    tmp_db.insert_slot("ls000002", "host-b", "b", "l", 0, 3600, {})
    slots = tmp_db.list_slots()
    assert len(slots) == 2


def test_list_slots_by_host(tmp_db):
    tmp_db.insert_slot("lh000001", "host-a", "a", "l", 0, 3600, {})
    tmp_db.insert_slot("lh000002", "host-b", "b", "l", 0, 3600, {})
    slots = tmp_db.list_slots(host="host-a")
    assert len(slots) == 1
    assert slots[0]["host"] == "host-a"


def test_count_active_slots(tmp_db):
    tmp_db.insert_slot("ca000001", "host-a", "a", "l", 0, 3600, {})
    tmp_db.insert_slot("ca000002", "host-a", "b", "l", 1, 3600, {})
    assert tmp_db.count_active_slots_by_host("host-a") == 2
    assert tmp_db.count_active_slots_by_host("host-b") == 0


def test_used_port_offsets(tmp_db):
    tmp_db.insert_slot("po000001", "host-a", "a", "l", 0, 3600, {})
    tmp_db.insert_slot("po000002", "host-a", "b", "l", 3, 3600, {})
    offsets = tmp_db.used_port_offsets("host-a")
    assert set(offsets) == {0, 3}
