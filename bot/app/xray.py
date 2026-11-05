"""Foreign-server Xray: config generation for the relay topology, live user
management, traffic stats, and reading which Iran exit the balancer picked.

Topology (proven against xray v26.10.31):

    client --REALITY--> clients-in --balancer--> ir-N --REALITY--> Iran exit

Everything the clients send is routed to the balancer, which selects the
healthiest Iran outbound via the observatory and fails over on its own. There
is deliberately no direct outbound for client traffic: if every Iran server
is down the connection fails closed rather than leaking out of the foreign
server's own IP.
"""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
import subprocess
import tempfile
import uuid as uuidlib
from pathlib import Path
from typing import Any

from . import config

_NETWORK = "raw"


class XrayError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# keys / ids
# --------------------------------------------------------------------------

def new_uuid() -> str:
    return str(uuidlib.uuid4())


def new_short_id() -> str:
    return secrets.token_hex(4)


def generate_keypair() -> tuple[str, str]:
    out = _run([config.XRAY_BIN, "x25519"])
    priv = pub = ""
    for line in out.splitlines():
        if line.startswith("PrivateKey:"):
            priv = line.split(":", 1)[1].strip()
        elif "PublicKey" in line or line.startswith("Password"):
            pub = line.split(":", 1)[1].strip()
    if not priv or not pub:
        raise XrayError(f"could not parse x25519 output:\n{out}")
    return priv, pub


# --------------------------------------------------------------------------
# config generation
# --------------------------------------------------------------------------

def _iran_outbound(srv: dict[str, Any]) -> dict[str, Any]:
    return {
        "tag": srv["tag"],
        "protocol": "vless",
        "settings": {
            "vnext": [{
                "address": srv["address"],
                "port": int(srv["port"]),
                "users": [{
                    "id": srv["uuid"],
                    "encryption": "none",
                    "flow": srv.get("flow") or config.FLOW,
                    "level": 0,
                }],
            }]
        },
        "streamSettings": {
            "network": _NETWORK,
            "security": "reality",
            "realitySettings": {
                "serverName": srv["sni"],
                "fingerprint": srv.get("fingerprint") or "chrome",
                "publicKey": srv["public_key"],
                "shortId": srv.get("short_id", ""),
            },
            # Server-to-server hop: TCP Fast Open saves a round trip per new
            # connection. Harmless if the exit doesn't support it (falls back
            # to a normal handshake). The installer sets net.ipv4.tcp_fastopen.
            "sockopt": {"tcpFastOpen": True},
        },
    }


def build_config(conn: sqlite3.Connection) -> dict[str, Any]:
    s = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
    if not s.get("reality_private_key"):
        raise XrayError("REALITY key missing; run setup first.")

    users = conn.execute(
        "SELECT uuid, email FROM users WHERE enabled = 1 ORDER BY id"
    ).fetchall()
    servers = _db_rows(conn.execute(
        "SELECT * FROM iran_servers WHERE enabled = 1 ORDER BY sort_order, id"
    ))
    if not servers:
        raise XrayError("no enabled Iran servers; add at least one exit.")

    sni = s.get("reality_sni", config.DEFAULT_SNI)
    strategy = s.get("strategy", "leastPing")
    if strategy not in ("leastPing", "random", "roundRobin"):
        strategy = "leastPing"

    outbounds: list[dict[str, Any]] = [_iran_outbound(srv) for srv in servers]
    outbounds.append({"tag": "block", "protocol": "blackhole", "settings": {}})

    cfg: dict[str, Any] = {
        "log": {"access": "none", "loglevel": "warning", "dnsLog": False},
        "api": {
            "tag": "api",
            "listen": config.XRAY_API,
            "services": ["HandlerService", "StatsService", "RoutingService"],
        },
        "stats": {},
        "policy": {
            "levels": {
                "0": {
                    "statsUserUplink": True,
                    "statsUserDownlink": True,
                    "statsUserOnline": True,
                    "handshake": 4,
                    "connIdle": 300,
                }
            },
            "system": {"statsInboundUplink": True, "statsInboundDownlink": True},
        },
        "dns": {"servers": ["localhost"], "queryStrategy": "UseIP"},
        "inbounds": [
            {
                "tag": config.INBOUND_TAG,
                "listen": "0.0.0.0",
                "port": int(s.get("listen_port", config.LISTEN_PORT)),
                "protocol": "vless",
                "settings": {
                    "clients": [
                        {"id": u["uuid"], "email": u["email"],
                         "flow": config.FLOW, "level": 0}
                        for u in users
                    ],
                    "decryption": "none",
                },
                "streamSettings": {
                    "network": _NETWORK,
                    "security": "reality",
                    "realitySettings": {
                        "show": False,
                        "target": f"{sni}:{s.get('reality_target_port', '443')}",
                        "xver": 0,
                        "serverNames": [sni],
                        "privateKey": s["reality_private_key"],
                        "shortIds": [s.get("reality_short_id", "")],
                    },
                },
                # Sniffing so the Iran exit resolves the real hostname with
                # Iranian DNS and the client's lookups never leak.
                "sniffing": {
                    "enabled": True,
                    "destOverride": ["http", "tls", "quic"],
                    "routeOnly": False,
                },
            }
        ],
        "outbounds": outbounds,
        "observatory": {
            "subjectSelector": [srv["tag"] for srv in servers],
            "probeURL": "https://www.aparat.com/",
            "probeInterval": "10s",
            "enableConcurrency": True,
        },
        "routing": {
            "domainStrategy": "AsIs",
            "balancers": [{
                "tag": config.BALANCER_TAG,
                "selector": [srv["tag"] for srv in servers],
                "strategy": {"type": strategy},
                "fallbackTag": servers[0]["tag"],
            }],
            "rules": [
                {"ruleTag": "block-bittorrent", "protocol": ["bittorrent"],
                 "outboundTag": "block"},
                {"ruleTag": "to-iran", "network": "tcp,udp",
                 "balancerTag": config.BALANCER_TAG},
            ],
        },
    }
    return cfg


def write_config(conn: sqlite3.Connection, path: Path | None = None) -> Path:
    target = path or config.XRAY_CONFIG
    body = json.dumps(build_config(conn), indent=2, ensure_ascii=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=target.parent, prefix=".cfg-", suffix=".json",
        delete=False, encoding="utf-8",
    ) as tmp:
        tmp.write(body + "\n")
        tmp_path = Path(tmp.name)
    try:
        validate(tmp_path)
    except XrayError:
        tmp_path.unlink(missing_ok=True)
        raise
    tmp_path.chmod(0o600)
    tmp_path.replace(target)
    return target


def validate(path: Path) -> None:
    if not Path(config.XRAY_BIN).exists():
        return
    proc = subprocess.run(
        [config.XRAY_BIN, "run", "-test", "-config", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise XrayError("Xray rejected the config:\n"
                        + (proc.stderr.strip() or proc.stdout.strip()))


def restart() -> None:
    import os
    cmd = ["systemctl", "restart", config.XRAY_SERVICE]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n", *cmd]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise XrayError("could not restart Xray: "
                        + (proc.stderr.strip() or proc.stdout.strip()))


# --------------------------------------------------------------------------
# live user management (no restart)
# --------------------------------------------------------------------------

def add_user_live(conn: sqlite3.Connection, email: str, user_uuid: str) -> None:
    port = int(_db_get(conn, "listen_port", str(config.LISTEN_PORT)))
    payload = {"inbounds": [{
        "tag": config.INBOUND_TAG, "listen": "0.0.0.0", "port": port,
        "protocol": "vless",
        "settings": {"decryption": "none",
                     "clients": [{"id": user_uuid, "email": email,
                                  "flow": config.FLOW, "level": 0}]},
    }]}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as tmp:
        json.dump(payload, tmp)
        tmp_path = tmp.name
    try:
        out = _run([config.XRAY_BIN, "api", "adu",
                    f"--server={config.XRAY_API}", tmp_path])
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    if "Added 1 user" not in out and "already exists" not in out:
        raise XrayError(f"adding user failed: {out.strip()}")


def remove_user_live(email: str) -> None:
    out = _run([config.XRAY_BIN, "api", "rmu",
                f"--server={config.XRAY_API}",
                f"-tag={config.INBOUND_TAG}", email])
    if "Removed 1 user" not in out and "not found" not in out:
        raise XrayError(f"removing user failed: {out.strip()}")


# --------------------------------------------------------------------------
# stats / balancer
# --------------------------------------------------------------------------

_STAT_RE = re.compile(r"^user>>>(?P<email>.+)>>>traffic>>>(?P<dir>uplink|downlink)$")


def pull_traffic(reset: bool = True) -> dict[str, dict[str, int]]:
    cmd = [config.XRAY_BIN, "api", "statsquery",
           f"--server={config.XRAY_API}", "-pattern", "user>>>"]
    if reset:
        cmd.append("-reset")
    parsed = _run_json(cmd)
    result: dict[str, dict[str, int]] = {}
    for stat in parsed.get("stat") or []:
        m = _STAT_RE.match(stat.get("name", ""))
        if not m:
            continue
        bucket = result.setdefault(m.group("email"), {"up": 0, "down": 0})
        key = "up" if m.group("dir") == "uplink" else "down"
        bucket[key] += int(stat.get("value") or 0)
    return result


def online_counts() -> dict[str, int]:
    parsed = _run_json([config.XRAY_BIN, "api", "statsonlineiplist",
                        f"--server={config.XRAY_API}", "-all"])
    counts: dict[str, int] = {}
    for user in parsed.get("users") or []:
        email = user.get("email")
        if email:
            counts[email] = len(user.get("ips") or [])
    return counts


def balancer_selection() -> list[str]:
    """Tags the balancer currently considers usable, best first. Parses the
    text output of `xray api bi` (there is no json flag for it)."""
    try:
        raw = _run([config.XRAY_BIN, "api", "bi",
                    f"--server={config.XRAY_API}", config.BALANCER_TAG])
    except XrayError:
        return []
    tags: list[str] = []
    in_selects = False
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("- Selects"):
            in_selects = True
            continue
        if in_selects:
            # rows look like: "1   ir-1"
            parts = stripped.split()
            if len(parts) >= 2 and parts[0].isdigit():
                tags.append(parts[1])
    return tags


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _db_rows(result) -> list[dict[str, Any]]:
    return [dict(r) for r in result]


def _db_get(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def _run(cmd: list[str], timeout: int = 15) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise XrayError(f"xray binary not found at {config.XRAY_BIN}") from exc
    except subprocess.TimeoutExpired as exc:
        raise XrayError(f"xray command timed out: {' '.join(cmd)}") from exc
    if proc.returncode != 0:
        raise XrayError(proc.stderr.strip() or proc.stdout.strip()
                        or "xray command failed")
    return proc.stdout


def _run_json(cmd: list[str]) -> dict[str, Any]:
    raw = _run(cmd)
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise XrayError(f"unexpected xray output: {raw[:200]}") from exc
