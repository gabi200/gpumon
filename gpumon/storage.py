"""SQLite persistence for samples, alerts and learned baselines."""

from __future__ import annotations

import sqlite3
import threading
from typing import Optional

from .backends import GPUSample

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts REAL, key TEXT, vendor TEXT, idx INTEGER, name TEXT,
    temp_c REAL, hotspot_c REAL, mem_temp_c REAL,
    power_w REAL, power_limit_w REAL,
    load_pct REAL, mem_load_pct REAL,
    clock_mhz REAL, max_clock_mhz REAL, mem_clock_mhz REAL,
    fan_rpm REAL, fan_pct REAL, voltage_mv REAL
);
CREATE INDEX IF NOT EXISTS idx_samples_key_ts ON samples(key, ts);

CREATE TABLE IF NOT EXISTS alerts (
    ts REAL, key TEXT, severity TEXT, code TEXT, message TEXT,
    value REAL, expected REAL
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);

CREATE TABLE IF NOT EXISTS baselines (
    key TEXT PRIMARY KEY,
    max_clock_mhz REAL,
    max_power_w REAL,
    samples_seen INTEGER DEFAULT 0,
    updated_ts REAL
);
"""

_SAMPLE_COLS = [
    "ts", "key", "vendor", "idx", "name", "temp_c", "hotspot_c", "mem_temp_c",
    "power_w", "power_limit_w", "load_pct", "mem_load_pct", "clock_mhz",
    "max_clock_mhz", "mem_clock_mhz", "fan_rpm", "fan_pct", "voltage_mv",
]


class Storage:
    def __init__(self, path: str = "gpumon.db", retention_days: float = 30.0):
        self._path = path
        self._retention_s = retention_days * 86400.0
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- writes --------------------------------------------------------
    def insert_sample(self, s: GPUSample) -> None:
        row = (
            s.ts, s.key, s.vendor, s.index, s.name, s.temp_c, s.hotspot_c,
            s.mem_temp_c, s.power_w, s.power_limit_w, s.load_pct, s.mem_load_pct,
            s.clock_mhz, s.max_clock_mhz, s.mem_clock_mhz, s.fan_rpm, s.fan_pct,
            s.voltage_mv,
        )
        ph = ",".join("?" * len(_SAMPLE_COLS))
        with self._lock:
            self._db.execute(
                f"INSERT INTO samples ({','.join(_SAMPLE_COLS)}) VALUES ({ph})",
                row,
            )
            self._db.commit()

    def insert_alert(self, key, severity, code, message, value, expected) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO alerts (ts,key,severity,code,message,value,expected)"
                " VALUES (?,?,?,?,?,?,?)",
                (_time(), key, severity, code, message, value, expected),
            )
            self._db.commit()

    def save_baseline(self, key, max_clock, max_power, seen) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO baselines (key,max_clock_mhz,max_power_w,"
                "samples_seen,updated_ts) VALUES (?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET max_clock_mhz=excluded.max_clock_mhz,"
                " max_power_w=excluded.max_power_w, samples_seen=excluded.samples_seen,"
                " updated_ts=excluded.updated_ts",
                (key, max_clock, max_power, seen, _time()),
            )
            self._db.commit()

    def prune(self) -> None:
        cutoff = _time() - self._retention_s
        with self._lock:
            self._db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            self._db.execute("DELETE FROM alerts WHERE ts < ?", (cutoff,))
            self._db.commit()

    # -- reads ---------------------------------------------------------
    def load_baselines(self) -> dict:
        with self._lock:
            rows = self._db.execute("SELECT * FROM baselines").fetchall()
        return {r["key"]: dict(r) for r in rows}

    def history(self, key: Optional[str] = None, limit: int = 500) -> list:
        q = "SELECT * FROM samples"
        args = []
        if key:
            q += " WHERE key = ?"
            args.append(key)
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    def recent_alerts(self, limit: int = 100) -> list:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM alerts ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def _time() -> float:
    import time
    return time.time()
