"""SQLite storage for the relay bot. The database is the single source of
truth; the foreign server's Xray config is always regenerated from it."""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Iterable

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- End users. They authenticate to the FOREIGN server only; their identity
-- never reaches the Iranian servers.
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email       TEXT    NOT NULL UNIQUE,
    label       TEXT    NOT NULL DEFAULT '',
    uuid        TEXT    NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    quota_bytes INTEGER NOT NULL DEFAULT 0,
    used_bytes  INTEGER NOT NULL DEFAULT 0,
    expires_at  INTEGER,
    note        TEXT    NOT NULL DEFAULT '',
    disabled_reason TEXT NOT NULL DEFAULT '',
    last_seen   INTEGER,
    created_at  INTEGER NOT NULL
);

-- The pool of Iranian exit servers. Each is added by pasting its VLESS
-- share link; the bot turns it into an outbound + balancer selector.
CREATE TABLE IF NOT EXISTS iran_servers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tag         TEXT    NOT NULL UNIQUE,
    label       TEXT    NOT NULL DEFAULT '',
    address     TEXT    NOT NULL,
    port        INTEGER NOT NULL DEFAULT 443,
    uuid        TEXT    NOT NULL,
    public_key  TEXT    NOT NULL,
    short_id    TEXT    NOT NULL DEFAULT '',
    sni         TEXT    NOT NULL,
    fingerprint TEXT    NOT NULL DEFAULT 'chrome',
    flow        TEXT    NOT NULL DEFAULT 'xtls-rprx-vision',
    enabled     INTEGER NOT NULL DEFAULT 1,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS server_health (
    tag        TEXT PRIMARY KEY,
    ok         INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER,
    selected   INTEGER NOT NULL DEFAULT 0,
    checked_at INTEGER,
    detail     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS usage_daily (
    user_id    INTEGER NOT NULL,
    day        TEXT    NOT NULL,
    up_bytes   INTEGER NOT NULL DEFAULT 0,
    down_bytes INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);
"""

DEFAULT_SETTINGS = {
    "listen_port": str(config.LISTEN_PORT),
    "public_host": config.PUBLIC_HOST,
    "reality_private_key": "",
    "reality_public_key": "",
    "reality_short_id": "",
    "reality_sni": config.DEFAULT_SNI,
    "reality_target_port": "443",
    "strategy": "leastPing",   # leastPing | random | roundRobin
    "bot_title": "IP SAZ",
}


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=10, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
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
        "%Y-%m-%d", time.gmtime(time.time() - config.USAGE_RETENTION_DAYS * 86400)
    )
    conn.execute("DELETE FROM usage_daily WHERE day < ?", (cutoff,))
