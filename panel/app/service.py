"""Business logic between the HTTP layer and Xray.

A client's `email` is its stable identity inside Xray and never changes.
`label` is the human name shown in the UI and can be edited freely.
"""
from __future__ import annotations

import re
import sqlite3
import time
from typing import Any

from . import config, db, links, xray

GIB = 1024 ** 3
EMAIL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,40}@ipiran$")


class ServiceError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------

def dashboard_state(conn: sqlite3.Connection) -> dict[str, Any]:
    settings = db.all_settings(conn)
    try:
        online = xray.online_counts()
    except xray.XrayError:
        online = {}

    now = int(time.time())
    clients = []
    for row in db.rows(conn.execute("SELECT * FROM clients ORDER BY id DESC")):
        quota = row["quota_bytes"]
        used = row["used_bytes"]
        clients.append({
            "id": row["id"],
            "email": row["email"],
            "label": row["label"] or row["email"].split("@")[0],
            "enabled": bool(row["enabled"]),
            "disabled_reason": row["disabled_reason"],
            "quota_bytes": quota,
            "used_bytes": used,
            "percent": round(min(100, used * 100 / quota), 1) if quota else None,
            "expires_at": row["expires_at"],
            "expired": bool(row["expires_at"] and row["expires_at"] < now),
            "days_left": (
                max(0, (row["expires_at"] - now) // 86400)
                if row["expires_at"] else None
            ),
            "note": row["note"],
            "online": online.get(row["email"], 0),
            "last_seen": row["last_seen"],
        })

    totals = conn.execute(
        "SELECT COALESCE(SUM(down_bytes + up_bytes), 0) AS all_time FROM usage_daily"
    ).fetchone()["all_time"]
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    month = today[:7]
    today_bytes = conn.execute(
        "SELECT COALESCE(SUM(down_bytes + up_bytes), 0) AS t FROM usage_daily WHERE day = ?",
        (today,),
    ).fetchone()["t"]
    month_bytes = conn.execute(
        "SELECT COALESCE(SUM(down_bytes + up_bytes), 0) AS t FROM usage_daily WHERE day >= ?",
        (month + "-01",),
    ).fetchone()["t"]

    return {
        "clients": clients,
        "online_total": sum(online.values()),
        "today_bytes": today_bytes,
        "month_bytes": month_bytes,
        "all_time_bytes": totals,
        "usage": usage_history(conn),
        "configured": bool(settings.get("reality_private_key")
                           and settings.get("public_host")),
        "title": settings.get("panel_title", "پنل آیپی ایران"),
        "sni": settings.get("reality_sni", ""),
        "public_host": settings.get("public_host", ""),
        "listen_port": settings.get("listen_port", "443"),
    }


def usage_history(conn: sqlite3.Connection, days: int = 14) -> list[dict[str, Any]]:
    start = time.strftime("%Y-%m-%d", time.gmtime(time.time() - (days - 1) * 86400))
    by_day = {
        r["day"]: r["t"]
        for r in conn.execute(
            "SELECT day, SUM(down_bytes + up_bytes) AS t FROM usage_daily "
            "WHERE day >= ? GROUP BY day", (start,)
        )
    }
    out = []
    for i in range(days):
        day = time.strftime("%Y-%m-%d",
                            time.gmtime(time.time() - (days - 1 - i) * 86400))
        out.append({"day": day, "bytes": by_day.get(day, 0)})
    return out


# ---------------------------------------------------------------------------
# mutations
# ---------------------------------------------------------------------------

def _slug(label: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", label.strip().lower()).strip("-")
    return base or "user"


def create_client(conn: sqlite3.Connection, label: str,
                  quota_gb: float | None, expires_days: int | None,
                  note: str = "") -> dict[str, Any]:
    label = (label or "").strip()
    if not label:
        raise ServiceError("نام کاربر لازم است.")

    email = f"{_slug(label)}@ipiran"
    # Guarantee uniqueness of the stable identity.
    suffix = 1
    base_email = email
    while conn.execute("SELECT 1 FROM clients WHERE email = ?", (email,)).fetchone():
        suffix += 1
        email = base_email.replace("@ipiran", f"-{suffix}@ipiran")

    quota = int((config.DEFAULT_QUOTA_GB if quota_gb is None else quota_gb) * GIB)
    days = config.DEFAULT_EXPIRE_DAYS if expires_days is None else expires_days
    expires_at = int(time.time()) + days * 86400 if days else None
    user_uuid = xray.new_uuid()

    conn.execute(
        "INSERT INTO clients (email, label, uuid, quota_bytes, expires_at, note, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (email, label, user_uuid, quota, expires_at, note, int(time.time())),
    )
    row = conn.execute("SELECT * FROM clients WHERE email = ?", (email,)).fetchone()

    _write_config(conn)
    _safe(xray.add_user_live, conn, email, user_uuid)
    return dict(row)


def update_client(conn: sqlite3.Connection, client_id: int,
                  **fields: Any) -> dict[str, Any]:
    before = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if not before:
        raise ServiceError("چنین کاربری وجود ندارد.")
    before = dict(before)

    updates: dict[str, Any] = {}
    if fields.get("label") is not None:
        label = str(fields["label"]).strip()
        if not label:
            raise ServiceError("نام نمی‌تواند خالی باشد.")
        updates["label"] = label
    if fields.get("note") is not None:
        updates["note"] = str(fields["note"])
    if fields.get("quota_gb") is not None:
        updates["quota_bytes"] = int(float(fields["quota_gb"]) * GIB)
    if "enabled" in fields and fields["enabled"] is not None:
        updates["enabled"] = 1 if fields["enabled"] else 0
        updates["disabled_reason"] = "" if fields["enabled"] else "manual"
    if fields.get("add_days"):
        base = before["expires_at"] or int(time.time())
        base = max(base, int(time.time()))
        updates["expires_at"] = base + int(fields["add_days"]) * 86400
    if fields.get("reset_usage"):
        updates["used_bytes"] = 0
        if before["disabled_reason"] == "quota":
            updates["enabled"] = 1
            updates["disabled_reason"] = ""
    if fields.get("rotate_uuid"):
        updates["uuid"] = xray.new_uuid()

    # Re-enabling a quota/expiry lockout should also clear the reason.
    if updates.get("enabled") == 1 and not fields.get("reset_usage"):
        updates.setdefault("disabled_reason", "")

    if not updates:
        return before

    conn.execute(
        f"UPDATE clients SET {', '.join(f'{k} = ?' for k in updates)} WHERE id = ?",
        (*updates.values(), client_id),
    )
    after = dict(conn.execute("SELECT * FROM clients WHERE id = ?",
                              (client_id,)).fetchone())

    # Push to the running instance.
    changed_identity = before["uuid"] != after["uuid"]
    changed_enabled = before["enabled"] != after["enabled"]
    _write_config(conn)
    if changed_identity or changed_enabled:
        if before["enabled"]:
            _safe(xray.remove_user_live, before["email"])
        if after["enabled"]:
            _safe(xray.add_user_live, conn, after["email"], after["uuid"])
    return after


def delete_client(conn: sqlite3.Connection, client_id: int) -> None:
    row = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if not row:
        raise ServiceError("چنین کاربری وجود ندارد.")
    conn.execute("DELETE FROM clients WHERE id = ?", (client_id,))
    conn.execute("DELETE FROM usage_daily WHERE client_id = ?", (client_id,))
    _write_config(conn)
    if row["enabled"]:
        _safe(xray.remove_user_live, row["email"])


def client_share_link(conn: sqlite3.Connection, client_id: int) -> str:
    row = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if not row:
        raise ServiceError("چنین کاربری وجود ندارد.")
    return links.share_link(conn, dict(row))


# ---------------------------------------------------------------------------
# background poll
# ---------------------------------------------------------------------------

def poll(conn: sqlite3.Connection) -> dict[str, Any]:
    """Drain counters into the DB, update online timestamps, enforce limits."""
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
        row = conn.execute("SELECT id FROM clients WHERE email = ?", (email,)).fetchone()
        if not row:
            continue
        conn.execute(
            "UPDATE clients SET used_bytes = used_bytes + ?, last_seen = ? WHERE id = ?",
            (total, now, row["id"]),
        )
        conn.execute(
            "INSERT INTO usage_daily (client_id, day, up_bytes, down_bytes) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(client_id, day) DO UPDATE SET "
            "up_bytes = up_bytes + excluded.up_bytes, "
            "down_bytes = down_bytes + excluded.down_bytes",
            (row["id"], today, amt["up"], amt["down"]),
        )

    try:
        for email in xray.online_counts():
            conn.execute("UPDATE clients SET last_seen = ? WHERE email = ?",
                         (now, email))
    except xray.XrayError:
        pass

    disabled = _enforce_limits(conn, now)
    db.purge_old_usage(conn)
    return {"ok": True, "users": len(traffic), "disabled": disabled}


def _enforce_limits(conn: sqlite3.Connection, now: int) -> list[str]:
    offenders = conn.execute(
        "SELECT id, email, quota_bytes, used_bytes, expires_at FROM clients "
        "WHERE enabled = 1 AND ((quota_bytes > 0 AND used_bytes >= quota_bytes) "
        "OR (expires_at IS NOT NULL AND expires_at < ?))",
        (now,),
    ).fetchall()
    disabled = []
    for row in offenders:
        reason = "expired" if row["expires_at"] and row["expires_at"] < now else "quota"
        conn.execute("UPDATE clients SET enabled = 0, disabled_reason = ? WHERE id = ?",
                     (reason, row["id"]))
        _safe(xray.remove_user_live, row["email"])
        disabled.append(row["email"])
    if disabled:
        _write_config(conn)
    return disabled


def rebuild(conn: sqlite3.Connection) -> None:
    """Rewrite the config and restart Xray so the running state matches the DB."""
    xray.write_config(conn)
    xray.restart()


# ---------------------------------------------------------------------------

def _write_config(conn: sqlite3.Connection) -> None:
    try:
        xray.write_config(conn)
    except xray.XrayError:
        pass


def _safe(fn, *args) -> None:
    try:
        fn(*args)
    except xray.XrayError:
        pass
