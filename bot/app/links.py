"""Parse incoming VLESS share links (to add an Iranian exit server) and build
outgoing client links (pointing at the foreign server)."""
from __future__ import annotations

import sqlite3
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

from . import config, db


class LinkError(RuntimeError):
    pass


def parse_vless(link: str) -> dict[str, Any]:
    """Pull the fields the bot needs out of a vless://... REALITY link.

    Accepts the exact format the Iran panel (and v2rayNG/Hiddify) produce:
      vless://<uuid>@<host>:<port>?type=tcp&security=reality&pbk=..&sni=..&sid=..&flow=..#label
    """
    link = link.strip()
    if not link.lower().startswith("vless://"):
        raise LinkError("لینک باید با vless:// شروع شود.")
    parsed = urlparse(link)
    uuid = unquote(parsed.username or "")
    host = parsed.hostname or ""
    port = parsed.port or 443
    if not uuid or not host:
        raise LinkError("لینک ناقص است: شناسه یا آدرس سرور پیدا نشد.")

    q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    if q.get("security") != "reality":
        raise LinkError("فقط لینک REALITY پشتیبانی می‌شود (security=reality).")
    pbk = q.get("pbk", "")
    if not pbk:
        raise LinkError("کلید عمومی (pbk) در لینک نیست.")

    label = unquote(parsed.fragment) if parsed.fragment else host
    return {
        "address": host,
        "port": int(port),
        "uuid": uuid,
        "public_key": pbk,
        "short_id": q.get("sid", ""),
        "sni": q.get("sni", q.get("host", "")),
        "fingerprint": q.get("fp", "chrome"),
        "flow": q.get("flow", config.FLOW),
        "label": label,
    }


def client_link(conn: sqlite3.Connection, user: dict[str, Any]) -> str:
    """Build the vless:// link the end user imports. It points at the FOREIGN
    server; the hop to Iran is invisible to the client."""
    s = db.all_settings(conn)
    host = s.get("public_host")
    if not host:
        raise LinkError(
            "آدرس سرور خارجی (public_host) تنظیم نشده. روی سرور بزن: "
            "ipsazctl set public_host <آیپی-سرور-خارجی>"
        )
    if not s.get("reality_public_key"):
        raise LinkError("کلید REALITY ساخته نشده.")

    params = {
        "type": "tcp",
        "security": "reality",
        "encryption": "none",
        "flow": config.FLOW,
        "sni": s.get("reality_sni", ""),
        "fp": "chrome",
        "pbk": s.get("reality_public_key", ""),
    }
    sid = s.get("reality_short_id", "")
    if sid:
        params["sid"] = sid
    query = urlencode({k: v for k, v in params.items() if v})
    label = user.get("label") or user["email"].split("@")[0]
    return (f"vless://{user['uuid']}@{host}:{s.get('listen_port', '443')}"
            f"?{query}#{quote(label)}")
