"""Business logic: users, the Iran-server pool, polling and enforcement.

All config changes funnel through here so the DB and the running Xray stay in
agreement. User add/remove is applied live; anything structural (servers,
keys, port, strategy) rewrites the config and restarts Xray.
"""
from __future__ import annotations

import re
import sqlite3
import time
from typing import Any

from . import config, db, health, links, xray

GIB = 1024 ** 3


class ServiceError(RuntimeError):
    pass


# Online counts come from an `xray api` subprocess; cache them briefly so
# every button press in the bot does not pay for a fresh spawn.
_online_cache: dict[str, int] = {}
_online_ts: float = 0.0


def _online(max_age: float = 10.0) -> dict[str, int]:
    global _online_cache, _online_ts
    if time.monotonic() - _online_ts < max_age:
        return _online_cache
    try:
        _online_cache = xray.online_counts()
    except xray.XrayError:
        _online_cache = {}
    _online_ts = time.monotonic()
    return _online_cache


# --------------------------------------------------------------------------
# users
# --------------------------------------------------------------------------

def _slug(label: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", label.strip().lower()).strip("-")
    return base or "user"


def create_user(conn: sqlite3.Connection, label: str,
                quota_gb: float | None = None,
                expires_days: int | None = None, note: str = "") -> dict[str, Any]:
    label = (label or "").strip()
    if not label:
        raise ServiceError("نام کاربر لازم است.")
    email = f"{_slug(label)}@ipsaz"
    base, n = email, 1
    while conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
        n += 1
        email = base.replace("@ipsaz", f"-{n}@ipsaz")

    quota = int((config.DEFAULT_QUOTA_GB if quota_gb is None else quota_gb) * GIB)
    days = config.DEFAULT_EXPIRE_DAYS if expires_days is None else expires_days
    expires_at = int(time.time()) + days * 86400 if days else None
    uid = xray.new_uuid()
    conn.execute(
        "INSERT INTO users (email,label,uuid,quota_bytes,expires_at,note,created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (email, label, uid, quota, expires_at, note, int(time.time())),
    )
    row = dict(conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone())
    _write_quiet(conn)
    _safe(xray.add_user_live, conn, email, uid)
    return row


def update_user(conn: sqlite3.Connection, user_id: int, **fields: Any) -> dict[str, Any]:
    before = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not before:
        raise ServiceError("چنین کاربری نیست.")
    before = dict(before)
    up: dict[str, Any] = {}
    if fields.get("label") is not None:
        label = str(fields["label"]).strip()
        if not label:
            raise ServiceError("نام خالی نشود.")
        up["label"] = label
    if fields.get("quota_gb") is not None:
        up["quota_bytes"] = int(float(fields["quota_gb"]) * GIB)
    if "enabled" in fields and fields["enabled"] is not None:
        up["enabled"] = 1 if fields["enabled"] else 0
        up["disabled_reason"] = "" if fields["enabled"] else "manual"
    if fields.get("add_days"):
        base = max(before["expires_at"] or 0, int(time.time()))
        up["expires_at"] = base + int(fields["add_days"]) * 86400
    if fields.get("reset_usage"):
        up["used_bytes"] = 0
        if before["disabled_reason"] == "quota":
            up["enabled"] = 1
            up["disabled_reason"] = ""
    if fields.get("rotate_uuid"):
        up["uuid"] = xray.new_uuid()
    if up.get("enabled") == 1 and not fields.get("reset_usage"):
        up.setdefault("disabled_reason", "")
    if not up:
        return before
    conn.execute(f"UPDATE users SET {', '.join(f'{k}=?' for k in up)} WHERE id=?",
                 (*up.values(), user_id))
    after = dict(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())
    _write_quiet(conn)
    if before["uuid"] != after["uuid"] or before["enabled"] != after["enabled"]:
        if before["enabled"]:
            _safe(xray.remove_user_live, before["email"])
        if after["enabled"]:
            _safe(xray.add_user_live, conn, after["email"], after["uuid"])
    return after


def delete_user(conn: sqlite3.Connection, user_id: int) -> None:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        raise ServiceError("چنین کاربری نیست.")
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.execute("DELETE FROM usage_daily WHERE user_id = ?", (user_id,))
    _write_quiet(conn)
    if row["enabled"]:
        _safe(xray.remove_user_live, row["email"])


def user_link(conn: sqlite3.Connection, user_id: int) -> str:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        raise ServiceError("چنین کاربری نیست.")
    return links.client_link(conn, dict(row))


# --------------------------------------------------------------------------
# Iran server pool
# --------------------------------------------------------------------------

def add_iran_server(conn: sqlite3.Connection, link: str,
                    label: str | None = None) -> dict[str, Any]:
    info = links.parse_vless(link)   # raises LinkError on bad input
    # Allocate the next ir-N tag.
    existing = {r["tag"] for r in conn.execute("SELECT tag FROM iran_servers")}
    n = 1
    while f"ir-{n}" in existing:
        n += 1
    tag = f"ir-{n}"
    conn.execute(
        "INSERT INTO iran_servers (tag,label,address,port,uuid,public_key,short_id,"
        "sni,fingerprint,flow,sort_order,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (tag, (label or info["label"] or tag), info["address"], info["port"],
         info["uuid"], info["public_key"], info["short_id"], info["sni"],
         info["fingerprint"], info["flow"], n, int(time.time())),
    )
    row = dict(conn.execute("SELECT * FROM iran_servers WHERE tag = ?", (tag,)).fetchone())
    rebuild(conn)   # structural: rewrite + restart
    return row


def remove_iran_server(conn: sqlite3.Connection, tag: str) -> bool:
    """Deletes the server. Returns True if the running Xray config was left
    stale: build_config() requires at least one enabled server, so if this
    was the last one, rebuilding would just raise and there is nothing
    sensible to replace it with. Xray keeps running its last-known-good
    config (including the now-deleted server) until a new one is added, at
    which point the next add_iran_server() rebuilds normally. The caller is
    expected to surface this to the operator; it is not silent on its own.
    """
    row = conn.execute("SELECT 1 FROM iran_servers WHERE tag = ?", (tag,)).fetchone()
    if not row:
        raise ServiceError("چنین سروری نیست.")
    remaining = conn.execute(
        "SELECT COUNT(*) AS n FROM iran_servers WHERE enabled = 1 AND tag != ?", (tag,)
    ).fetchone()["n"]
    conn.execute("DELETE FROM iran_servers WHERE tag = ?", (tag,))
    conn.execute("DELETE FROM server_health WHERE tag = ?", (tag,))
    if remaining > 0:
        rebuild(conn)
        return False
    return True


def set_server_enabled(conn: sqlite3.Connection, tag: str, enabled: bool) -> None:
    row = conn.execute("SELECT 1 FROM iran_servers WHERE tag = ?", (tag,)).fetchone()
    if not row:
        raise ServiceError("چنین سروری نیست.")
    if not enabled:
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM iran_servers WHERE enabled=1 AND tag!=?", (tag,)
        ).fetchone()["n"]
        if remaining == 0:
            raise ServiceError("حداقل یک سرور ایران باید فعال بماند.")
    conn.execute("UPDATE iran_servers SET enabled=? WHERE tag=?",
                 (1 if enabled else 0, tag))
    rebuild(conn)


# --------------------------------------------------------------------------
# state for the UI
# --------------------------------------------------------------------------

def dashboard(conn: sqlite3.Connection) -> dict[str, Any]:
    s = db.all_settings(conn)
    online = _online()
    now = int(time.time())
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    month = today[:7]

    users = []
    for r in db.rows(conn.execute("SELECT * FROM users ORDER BY id DESC")):
        q = r["quota_bytes"]
        users.append({
            **r,
            "online": online.get(r["email"], 0),
            "percent": round(min(100, r["used_bytes"] * 100 / q), 1) if q else None,
            "expired": bool(r["expires_at"] and r["expires_at"] < now),
            "days_left": (max(0, (r["expires_at"] - now) // 86400)
                          if r["expires_at"] else None),
        })

    hrows = health.snapshot(conn)
    servers = []
    for r in db.rows(conn.execute(
            "SELECT * FROM iran_servers ORDER BY sort_order, id")):
        servers.append({**r, "health": hrows.get(r["tag"], {})})

    today_b = conn.execute(
        "SELECT COALESCE(SUM(up_bytes+down_bytes),0) t FROM usage_daily WHERE day=?",
        (today,)).fetchone()["t"]
    month_b = conn.execute(
        "SELECT COALESCE(SUM(up_bytes+down_bytes),0) t FROM usage_daily WHERE day>=?",
        (month + "-01",)).fetchone()["t"]

    return {
        "users": users, "servers": servers,
        "online_total": sum(online.values()),
        "today_bytes": today_b, "month_bytes": month_b,
        "configured": bool(s.get("reality_private_key") and s.get("public_host")),
        "public_host": s.get("public_host", ""), "sni": s.get("reality_sni", ""),
        "strategy": s.get("strategy", "leastPing"),
        "title": s.get("bot_title", "IP SAZ"),
    }


# --------------------------------------------------------------------------
# background poll
# --------------------------------------------------------------------------

def poll(conn: sqlite3.Connection) -> dict[str, Any]:
    try:
        traffic = xray.pull_traffic(reset=True)
    except xray.XrayError as exc:
        return {"ok": False, "error": str(exc)}
    now = int(time.time())
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    for email, amt in traffic.items():
        total = amt["up"] + amt["down"]
        if total <= 0:
            continue
        row = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if not row:
            continue
        conn.execute("UPDATE users SET used_bytes=used_bytes+?, last_seen=? WHERE id=?",
                     (total, now, row["id"]))
        conn.execute(
            "INSERT INTO usage_daily (user_id,day,up_bytes,down_bytes) VALUES (?,?,?,?)"
            " ON CONFLICT(user_id,day) DO UPDATE SET up_bytes=up_bytes+excluded.up_bytes,"
            " down_bytes=down_bytes+excluded.down_bytes",
            (row["id"], today, amt["up"], amt["down"]))
    global _online_cache, _online_ts
    try:
        _online_cache = xray.online_counts()
        _online_ts = time.monotonic()
        for email in _online_cache:
            conn.execute("UPDATE users SET last_seen=? WHERE email=?", (now, email))
    except xray.XrayError:
        pass
    disabled = _enforce(conn, now)
    db.purge_old_usage(conn)
    return {"ok": True, "disabled": disabled}


def _enforce(conn: sqlite3.Connection, now: int) -> list[str]:
    offenders = conn.execute(
        "SELECT id,email,expires_at,quota_bytes,used_bytes FROM users WHERE enabled=1 "
        "AND ((quota_bytes>0 AND used_bytes>=quota_bytes) "
        "OR (expires_at IS NOT NULL AND expires_at<?))", (now,)).fetchall()
    out = []
    for r in offenders:
        reason = "expired" if r["expires_at"] and r["expires_at"] < now else "quota"
        conn.execute("UPDATE users SET enabled=0, disabled_reason=? WHERE id=?",
                     (reason, r["id"]))
        _safe(xray.remove_user_live, r["email"])
        out.append(r["email"])
    if out:
        _write_quiet(conn)
    return out


# --------------------------------------------------------------------------

def rebuild(conn: sqlite3.Connection) -> None:
    xray.write_config(conn)
    xray.restart()


def _write_quiet(conn: sqlite3.Connection) -> None:
    try:
        xray.write_config(conn)
    except xray.XrayError:
        pass


def _safe(fn, *args) -> None:
    try:
        fn(*args)
    except xray.XrayError:
        pass
