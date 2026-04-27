"""Home Lab Monitor - Database layer (SQLite)

Stores 24 hours of per-host metric snapshots and active slot reservations.
Schema: one row per poll cycle per host (metrics), one row per active slot (slots).
"""
import json
import sqlite3
import time
from typing import Dict, List, Optional

RETENTION_SECONDS = 86400  # 24 hours


class Database:
    def __init__(self, path: str):
        self.path = path
        self._conn = None

    # ------------------------------------------------------------------
    # Connection management (thread-local via check_same_thread=False)
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            # WAL mode survives process kills without index corruption
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        return self._conn

    def init(self):
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS metrics (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                host      TEXT    NOT NULL,
                ts        INTEGER NOT NULL,
                online    INTEGER NOT NULL DEFAULT 1,
                data      TEXT    NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_host_ts ON metrics (host, ts);

            CREATE TABLE IF NOT EXISTS slots (
                id          TEXT    PRIMARY KEY,
                host        TEXT    NOT NULL,
                caller      TEXT    NOT NULL,
                label       TEXT    NOT NULL DEFAULT '',
                port_offset INTEGER NOT NULL,
                created_ts  INTEGER NOT NULL,
                expires_ts  INTEGER NOT NULL,
                last_heartbeat_ts INTEGER NOT NULL,
                meta        TEXT    NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_slots_host ON slots (host);
            CREATE INDEX IF NOT EXISTS idx_slots_expires ON slots (expires_ts);
        """)
        conn.commit()

    # ------------------------------------------------------------------
    # Metrics writes
    # ------------------------------------------------------------------

    def insert(self, host: str, data: dict):
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO metrics (host, ts, online, data) VALUES (?, ?, 1, ?)",
            (host, data.get("timestamp", int(time.time())), json.dumps(data))
        )
        conn.commit()

    def insert_offline(self, host: str):
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO metrics (host, ts, online, data) VALUES (?, ?, 0, '{}')",
            (host, int(time.time()))
        )
        conn.commit()

    def prune_old(self):
        cutoff = int(time.time()) - RETENTION_SECONDS
        conn = self._get_conn()
        conn.execute("DELETE FROM metrics WHERE ts < ?", (cutoff,))
        conn.commit()

    # ------------------------------------------------------------------
    # Metrics reads
    # ------------------------------------------------------------------

    def latest(self, host: str) -> Optional[Dict]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT ts, online, data FROM metrics WHERE host = ? ORDER BY ts DESC LIMIT 1",
            (host,)
        ).fetchone()
        if row is None:
            return None
        result = json.loads(row["data"]) if row["online"] else {}
        result["_online"] = bool(row["online"])
        result["_ts"] = row["ts"]
        return result

    def history(self, host: str, limit: int = 1440) -> List[Dict]:
        """Return up to `limit` rows, oldest-first, for the last 24 h."""
        conn = self._get_conn()
        cutoff = int(time.time()) - RETENTION_SECONDS
        rows = conn.execute(
            "SELECT ts, online, data FROM metrics "
            "WHERE host = ? AND ts >= ? "
            "ORDER BY ts ASC LIMIT ?",
            (host, cutoff, limit)
        ).fetchall()
        results = []
        for row in rows:
            entry = {"_ts": row["ts"], "_online": bool(row["online"])}
            if row["online"]:
                entry.update(json.loads(row["data"]))
            results.append(entry)
        return results

    def hosts(self) -> List[str]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT DISTINCT host FROM metrics ORDER BY host"
        ).fetchall()
        return [r["host"] for r in rows]

    # ------------------------------------------------------------------
    # Slot writes
    # ------------------------------------------------------------------

    def insert_slot(self, slot_id: str, host: str, caller: str, label: str,
                    port_offset: int, ttl_seconds: int, meta: dict) -> bool:
        """Insert a new slot. Returns False on PRIMARY KEY collision."""
        now = int(time.time())
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO slots
                   (id, host, caller, label, port_offset, created_ts, expires_ts,
                    last_heartbeat_ts, meta)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (slot_id, host, caller, label, port_offset, now,
                 now + ttl_seconds, now, json.dumps(meta))
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def delete_slot(self, slot_id: str) -> bool:
        """Delete a slot. Returns True if a row was deleted."""
        conn = self._get_conn()
        cursor = conn.execute("DELETE FROM slots WHERE id = ?", (slot_id,))
        conn.commit()
        return cursor.rowcount > 0

    def update_slot_expiry(self, slot_id: str, extend_seconds: int) -> bool:
        """Extend a slot's TTL from now. Returns True if the slot exists."""
        now = int(time.time())
        conn = self._get_conn()
        cursor = conn.execute(
            "UPDATE slots SET expires_ts = ?, last_heartbeat_ts = ? WHERE id = ?",
            (now + extend_seconds, now, slot_id)
        )
        conn.commit()
        return cursor.rowcount > 0

    def delete_expired_slots(self) -> int:
        """Remove all expired slots. Returns count deleted."""
        conn = self._get_conn()
        cursor = conn.execute(
            "DELETE FROM slots WHERE expires_ts < ?", (int(time.time()),)
        )
        conn.commit()
        return cursor.rowcount

    # ------------------------------------------------------------------
    # Slot reads
    # ------------------------------------------------------------------

    def get_slot(self, slot_id: str) -> Optional[Dict]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM slots WHERE id = ?", (slot_id,)
        ).fetchone()
        return _slot_row_to_dict(row) if row else None

    def list_slots(self, host: Optional[str] = None) -> List[Dict]:
        conn = self._get_conn()
        if host:
            rows = conn.execute(
                "SELECT * FROM slots WHERE host = ? ORDER BY created_ts ASC", (host,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM slots ORDER BY host ASC, created_ts ASC"
            ).fetchall()
        return [_slot_row_to_dict(r) for r in rows]

    def count_active_slots_by_host(self, host: str) -> int:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) as n FROM slots WHERE host = ? AND expires_ts >= ?",
            (host, int(time.time()))
        ).fetchone()
        return row["n"] if row else 0

    def used_port_offsets(self, host: str) -> List[int]:
        """Return port_offset values for all non-expired slots on a host."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT port_offset FROM slots WHERE host = ? AND expires_ts >= ?",
            (host, int(time.time()))
        ).fetchall()
        return [r["port_offset"] for r in rows]


def _slot_row_to_dict(row: sqlite3.Row) -> Dict:
    return {
        "id": row["id"],
        "host": row["host"],
        "caller": row["caller"],
        "label": row["label"],
        "port_offset": row["port_offset"],
        "created_ts": row["created_ts"],
        "expires_ts": row["expires_ts"],
        "last_heartbeat_ts": row["last_heartbeat_ts"],
        "meta": json.loads(row["meta"]),
    }
