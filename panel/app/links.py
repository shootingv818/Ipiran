"""VLESS + REALITY share links.

The link points at this Iranian server. A client app (v2rayNG, Hiddify)
imports it and routes through here. For "Iran-only" use the recommendation
is to enable per-app or geo routing on the client so that only Iranian apps
go through the tunnel; the link itself is a plain full-tunnel VLESS URI,
which every client understands.
"""
from __future__ import annotations

import sqlite3
from typing import Any
from urllib.parse import quote, urlencode

from . import config, db


class LinkError(RuntimeError):
    pass


def share_link(conn: sqlite3.Connection, client: dict[str, Any]) -> str:
    s = db.all_settings(conn)
    host = s.get("public_host")
    if not host:
        raise LinkError("public_host تنظیم نشده؛ آدرس سرور را در تنظیمات وارد کن.")
    if not s.get("reality_public_key"):
        raise LinkError("کلید REALITY ساخته نشده.")

    params = {
        "type": "tcp",            # client apps still call raw transport "tcp"
        "security": "reality",
        "encryption": "none",
        "flow": config.FLOW,
        "sni": s.get("reality_sni", ""),
        "fp": "chrome",
        "pbk": s.get("reality_public_key", ""),
    }
    short_id = s.get("reality_short_id", "")
    if short_id:
        params["sid"] = short_id
    query = urlencode({k: v for k, v in params.items() if v})
    label = client.get("label") or client["email"].split("@")[0]
    return (
        f"vless://{client['uuid']}@{host}:{s.get('listen_port', '443')}"
        f"?{query}#{quote(label)}"
    )
