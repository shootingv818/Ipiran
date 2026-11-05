"""Health of the Iranian exit servers, from the foreign server's vantage.

Two signals are combined:
  * a plain TCP connect to the exit's port, for latency and reachability;
  * the balancer's own view (which exits it currently routes to), which
    reflects the live REALITY probe, not just a bare port check.
"""
from __future__ import annotations

import socket
import sqlite3
import time
from typing import Any

from . import xray


def tcp_latency(host: str, port: int, timeout: float = 4.0):
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError as exc:
        return False, None, str(exc)
    return True, int((time.monotonic() - started) * 1000), ""


def refresh(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    selected = set(xray.balancer_selection())
    now = int(time.time())
    out: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT * FROM iran_servers ORDER BY sort_order, id"
    ).fetchall():
        reachable, latency, err = tcp_latency(row["address"], row["port"])
        is_selected = row["tag"] in selected
        # "ok" means the balancer would actually use it; reachability alone is
        # not enough because the exit can be up yet filtered from Iran.
        ok = reachable and (is_selected or not selected)
        detail = err if not reachable else ("" if ok else "از مسیر فعلی خارج است")
        conn.execute(
            "INSERT INTO server_health (tag, ok, latency_ms, selected, checked_at, detail)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT(tag) DO UPDATE SET ok=excluded.ok,"
            " latency_ms=excluded.latency_ms, selected=excluded.selected,"
            " checked_at=excluded.checked_at, detail=excluded.detail",
            (row["tag"], 1 if ok else 0, latency, 1 if is_selected else 0, now, detail),
        )
        out.append({"tag": row["tag"], "label": row["label"], "ok": ok,
                    "latency_ms": latency, "selected": is_selected,
                    "reachable": reachable, "detail": detail})
    return out


def snapshot(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {r["tag"]: dict(r) for r in conn.execute("SELECT * FROM server_health")}
