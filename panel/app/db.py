"""SQLite storage. Thin helpers over a single connection per request.

Design notes:
- WAL mode so the background poller and the request handlers don't block
  each other.
- No per-user IP history is stored. Online IP *count* is derived live from
  Xray and never written to disk, because this box sits inside Iran and a
  record of who connected from where is exactly what must not exist here.
"""
from __future__ import annotations

import secrets
import sqlite3
import time
from typing import Any, Iterable

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clients (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email       TEXT    NOT NULL UNIQUE,   -- stable xray identity, e.g. ali@ipiran
    label       TEXT    NOT NULL DEFAULT '',
    uuid        TEXT    NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    quota_bytes INTEGER NOT NULL DEFAULT 0,   -- 0 = unlimited
    used_bytes  INTEGER NOT NULL DEFAULT 0,
    expires_at  INTEGER,                        -- epoch seconds, NULL = never
    note        TEXT    NOT NULL DEFAULT '',
    disabled_reason TEXT NOT NULL DEFAULT '',   -- '', 'manual', 'quota', 'expired'
    last_seen   INTEGER,
    created_at  INTEGER NOT NULL
);

-- Daily totals so the dashboard can draw a chart without keeping raw samples.
CREATE TABLE IF NOT EXISTS usage_daily (
    client_id  INTEGER NOT NULL,
    day        TEXT    NOT NULL,          -- YYYY-MM-DD, UTC
    up_bytes   INTEGER NOT NULL DEFAULT 0,
    down_bytes INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (client_id, day)
);
"""

DEFAULT_SETTINGS = {
    "listen_port": "443",
    "public_host": "",            # IP or hostname clients dial
    "reality_private_key": "",
    "reality_public_key": "",
    "reality_short_id": "",
    "reality_sni": "www.varzesh3.com",
    "reality_target_port": "443",
    "panel_title": "پنل آیپی ایران",
    # Epoch second the ephemeral traffic monitor closes. 0 = off.
    "monitor_until": "0",
}


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=10, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect()
    conn.executescript(SCHEMA)
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO NOTHING",
            (key, value),
        )
    # The secret path is seeded from the environment on first setup and then
    # owned by the database, so rotating it from the panel sticks.
    if not get_setting(conn, "secret_path"):
        set_setting(conn, "secret_path",
                    config.SECRET_PATH or secrets.token_urlsafe(16))
    try:
        config.DB_PATH.chmod(0o600)
    except OSError:
        pass
    return conn


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def all_settings(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}


def rows(result: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in result]


def purge_old_usage(conn: sqlite3.Connection) -> None:
    cutoff = time.strftime(
        "%Y-%m-%d",
        time.gmtime(time.time() - config.USAGE_RETENTION_DAYS * 86400),
    )
    conn.execute("DELETE FROM usage_daily WHERE day < ?", (cutoff,))
