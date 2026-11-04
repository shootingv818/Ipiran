"""Business logic between the HTTP layer and Xray.

A client's `email` is its stable identity inside Xray and never changes.
`label` is the human name shown in the UI and can be edited freely.
"""
from __future__ import annotations

import ipaddress
import re
import sqlite3
import time
from typing import Any

from . import config, db, links, monitor, xray

GIB = 1024 ** 3
EMAIL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,40}@ipiran$")


class ServiceError(RuntimeError):
    pass


# Live rates, derived from the gap between two counter drains. Kept in memory
# only: this is a speedometer, not a history.
_speed: dict[str, dict[str, float]] = {}
_speed_ts: float = 0.0

# Online device counts are read by a subprocess, so the poller caches them
# instead of every page refresh paying for it.
_online_cache: dict[str, int] = {}
_online_ts: float = 0.0


def _online(max_age: float = 20.0) -> dict[str, int]:
    global _online_cache, _online_ts
    if time.monotonic() - _online_ts < max_age:
        return _online_cache
    try:
        _online_cache = xray.online_counts()
    except xray.XrayError:
        _online_cache = {}
    _online_ts = time.monotonic()
    return _online_cache


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------

def dashboard_state(conn: sqlite3.Connection) -> dict[str, Any]:
    settings = db.all_settings(conn)
    online = _online()

    now = int(time.time())
    clients = []
    for row in db.rows(conn.execute("SELECT * FROM clients ORDER BY id DESC")):
        quota = row["quota_bytes"]
        used = row["used_bytes"]
        rate = _speed.get(row["email"], {})
        clients.append({
            "up_bps": round(rate.get("up", 0.0)),
            "down_bps": round(rate.get("down", 0.0)),
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
        "up_bps": round(sum(r.get("up", 0.0) for r in _speed.values())),
        "down_bps": round(sum(r.get("down", 0.0) for r in _speed.values())),
        "usage": usage_history(conn),
        "configured": bool(settings.get("reality_private_key")
                           and settings.get("public_host")),
        "title": settings.get("panel_title", "پنل آیپی ایران"),
        "sni": settings.get("reality_sni", ""),
        "public_host": settings.get("public_host", ""),
        "listen_port": settings.get("listen_port", "443"),
        "monitor": {
            "active": monitor.is_active(conn),
            "seconds_left": monitor.seconds_left(conn),
            "summary": monitor.summary(),
        },
    }


# ---------------------------------------------------------------------------
# traffic path: what the connection actually goes through
# ---------------------------------------------------------------------------

def _is_local(ip: str, public_host: str) -> bool:
    if ip == public_host:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_loopback or addr.is_private or addr.is_link_local


def connection_path(conn: sqlite3.Connection, peer_ip: str) -> dict[str, Any]:
    """Describe the real chain a packet takes, read from the live config, plus
    a verdict on whether *this* browser is currently inside the tunnel.

    The verdict works because the panel is served by the same server that
    terminates the tunnel: if your traffic is going through it, the source
    address the panel sees is the server itself, not your ISP address.
    """
    settings = db.all_settings(conn)
    public_host = settings.get("public_host", "")
    sni = settings.get("reality_sni", "")
    port = settings.get("listen_port", "443")

    inside = _is_local(peer_ip, public_host)
    try:
        cfg = xray.build_config(conn)
        inbound = cfg["inbounds"][0]
        sniffing = inbound["sniffing"]
        reality = inbound["streamSettings"]["realitySettings"]
        dns = cfg["dns"]["servers"]
        egress = next(o for o in cfg["outbounds"] if o["tag"] == "direct")
        blocked = [r.get("ruleTag") for r in cfg["routing"]["rules"]]
    except Exception:  # noqa: BLE001 - diagram must never break the page
        return {
            "inside_tunnel": inside,
            "your_ip": peer_ip,
            "exit_ip": public_host,
            "steps": [],
        }

    return {
        "inside_tunnel": inside,
        "your_ip": peer_ip,
        "exit_ip": public_host,
        "steps": [
            {
                "key": "device",
                "title": "دستگاه تو",
                "detail": (f"از دید سرور: {public_host} (خود سرور)" if inside
                           else f"آی‌پی واقعی تو: {peer_ip}"),
                "note": ("ترافیکت از تونل رد می‌شود، پس اپراتور آی‌پی مقصد "
                         "واقعی را نمی‌بیند."
                         if inside else
                         "این صفحه را بیرون از تونل باز کرده‌ای."),
                "ok": inside,
            },
            {
                "key": "disguise",
                "title": "استتار REALITY",
                "detail": f"پورت {port} با نام {reality['serverNames'][0]}",
                "note": ("برای اپراتور، ترافیک شبیه بازدید از "
                         f"{sni} است و قابل تشخیص نیست."),
                "ok": True,
            },
            {
                "key": "sniff",
                "title": "اسنیفینگ",
                "detail": ("روشن برای " + "، ".join(sniffing["destOverride"])
                           if sniffing["enabled"] else "خاموش"),
                "note": ("نام مقصد روی سرور خوانده می‌شود، پس درخواست DNS "
                         "دستگاهت به اپراتور نمی‌رسد."),
                "ok": bool(sniffing["enabled"]) and not sniffing["routeOnly"],
            },
            {
                "key": "dns",
                "title": "حل نام با DNS سرور",
                "detail": "، ".join(str(s) for s in dns),
                "note": "آدرس سایت‌های ایرانی از داخل ایران حل می‌شود.",
                "ok": True,
            },
            {
                "key": "exit",
                "title": "خروج به اینترنت",
                "detail": f"با آی‌پی {public_host}",
                "note": ("سایت‌ها همین آی‌پی را می‌بینند، نه آی‌پی تو. "
                         "دسترسی به شبکه‌ی داخلی سرور بسته است."),
                "ok": egress["protocol"] == "freedom",
            },
        ],
        "blocked_rules": [r for r in blocked if r],
    }


# ---------------------------------------------------------------------------
# ephemeral monitor
# ---------------------------------------------------------------------------

def monitor_start(conn: sqlite3.Connection, minutes: int) -> dict[str, Any]:
    monitor.arm(conn, minutes)
    try:
        xray.write_config(conn)
        xray.restart()
    except Exception:
        # Put the config back as well, not just the flag, so the file on disk
        # never claims logging is on while the panel thinks it is off.
        monitor.disarm(conn)
        try:
            xray.write_config(conn)
        except Exception:  # noqa: BLE001
            pass
        raise
    return {"ok": True, "seconds_left": monitor.seconds_left(conn)}


def monitor_stop(conn: sqlite3.Connection) -> dict[str, Any]:
    monitor.disarm(conn)
    xray.write_config(conn)
    xray.restart()
    return {"ok": True}


def monitor_feed(conn: sqlite3.Connection) -> dict[str, Any]:
    if monitor.is_active(conn):
        if monitor.drain() == "too_big":
            monitor_stop(conn)
            return {"active": False, "entries": [],
                    "error": "حجم لاگ از حد گذشت؛ مونیتور خاموش شد."}
    return {
        "active": monitor.is_active(conn),
        "seconds_left": monitor.seconds_left(conn),
        "summary": monitor.summary(),
        "entries": monitor.entries(),
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
    global _speed, _speed_ts

    # Close an expired monitor window before anything else.
    if monitor.active_until(conn) and not monitor.is_active(conn):
        try:
            monitor_stop(conn)
        except Exception:  # noqa: BLE001
            pass
    elif monitor.is_active(conn):
        if monitor.drain() == "too_big":
            try:
                monitor_stop(conn)
            except Exception:  # noqa: BLE001
                pass

    try:
        traffic = xray.pull_traffic(reset=True)
    except xray.XrayError as exc:
        return {"ok": False, "error": str(exc)}

    # Counters were just reset, so this interval's bytes are the rate.
    mono = time.monotonic()
    elapsed = mono - _speed_ts if _speed_ts else 0.0
    if 0.5 < elapsed < 600:
        _speed = {
            email: {"up": amt["up"] / elapsed, "down": amt["down"] / elapsed}
            for email, amt in traffic.items()
        }
    else:
        _speed = {}
    _speed_ts = mono

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

    global _online_cache, _online_ts
    try:
        _online_cache = xray.online_counts()
        _online_ts = time.monotonic()
        for email, count in _online_cache.items():
            if count:
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
