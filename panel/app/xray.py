"""Everything that talks to the xray binary: config generation, live user
add/remove, traffic counters, online state and key generation.

All the command shapes and output formats here were verified against
xray v26.10.31. Two sharp edges that the code works around:

  * `xray api adu` and `rmu` exit 0 even when they fail; the only signal is
    a line in stdout. We parse that line.
  * `adu` needs the inbound's own `listen` and `port` in its payload, not
    just the tag.
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
import uuid as uuidlib
import secrets
import sqlite3
from pathlib import Path
from typing import Any

from . import config, monitor

# REALITY only supports RAW / XHTTP / gRPC; "raw" is the current name for
# what used to be called "tcp".
_NETWORK = "raw"


class XrayError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# identities and keys
# ---------------------------------------------------------------------------

def new_uuid() -> str:
    return str(uuidlib.uuid4())


def new_short_id() -> str:
    return secrets.token_hex(4)


def generate_keypair() -> tuple[str, str]:
    """(private_key, public_key) via `xray x25519`."""
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


# ---------------------------------------------------------------------------
# config generation
# ---------------------------------------------------------------------------

def build_config(conn: sqlite3.Connection) -> dict[str, Any]:
    s = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
    if not s.get("reality_private_key"):
        raise XrayError("REALITY key missing; run `ipiranctl init-keys` first.")

    clients = conn.execute(
        "SELECT uuid, email FROM clients WHERE enabled = 1 ORDER BY id"
    ).fetchall()

    sni = s.get("reality_sni", "www.varzesh3.com")
    target_port = s.get("reality_target_port", "443")
    return {
        "log": {
            # The access log is what records client source addresses, so it
            # stays off permanently on a server inside Iran. The panel's
            # live monitor redirects the *diagnostic* log instead, to a
            # RAM-backed file, for a short self-expiring window.
            "access": "none",
            "loglevel": "warning",
            "dnsLog": False,
            **monitor.log_overrides(conn),
        },
        "api": {
            "tag": "api",
            "listen": config.XRAY_API,
            "services": ["HandlerService", "StatsService"],
        },
        "stats": {},
        "policy": {
            # Per-user counters are what the dashboard reads. "online" powers
            # the live connected-device count.
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
                "port": int(s.get("listen_port", "443")),
                "protocol": "vless",
                "settings": {
                    "clients": [
                        {"id": c["uuid"], "email": c["email"],
                         "flow": config.FLOW, "level": 0}
                        for c in clients
                    ],
                    "decryption": "none",
                },
                "streamSettings": {
                    "network": _NETWORK,
                    "security": "reality",
                    "realitySettings": {
                        "show": False,
                        "target": f"{sni}:{target_port}",
                        "xver": 0,
                        "serverNames": [sni],
                        "privateKey": s["reality_private_key"],
                        "shortIds": [s.get("reality_short_id", "")],
                    },
                },
                # Sniffing rewrites the connection's destination to the real
                # hostname so the server resolves it with Iranian DNS (correct
                # answers for CDN-hosted Iranian sites) and the client's own
                # DNS queries never leak to its ISP.
                "sniffing": {
                    "enabled": True,
                    "destOverride": ["http", "tls", "quic"],
                    "routeOnly": False,
                },
            }
        ],
        "outbounds": [
            {
                "tag": "direct",
                "protocol": "freedom",
                # v26 freedom blocks private/loopback ranges by default, which
                # stops a client from reaching this server's own LAN.
                "settings": {"domainStrategy": "UseIP"},
            },
            {"tag": "block", "protocol": "blackhole", "settings": {}},
        ],
        "routing": {
            "domainStrategy": "AsIs",
            "rules": [
                {"ruleTag": "block-bittorrent", "protocol": ["bittorrent"],
                 "outboundTag": "block"},
                {"ruleTag": "block-ads", "domain": ["geosite:category-ads-all"],
                 "outboundTag": "block"},
            ],
        },
    }


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
        _validate(tmp_path)
    except XrayError:
        tmp_path.unlink(missing_ok=True)
        raise
    tmp_path.chmod(0o600)
    tmp_path.replace(target)
    return target


def _validate(path: Path) -> None:
    if not Path(config.XRAY_BIN).exists():
        return  # running the panel off-box; skip
    proc = subprocess.run(
        [config.XRAY_BIN, "run", "-test", "-config", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise XrayError("Xray rejected the config:\n"
                        + (proc.stderr.strip() or proc.stdout.strip()))


def restart() -> None:
    """Restart Xray. Needed for structural changes (keys, port, SNI).
    Client add/remove is applied live and does not come through here."""
    import os
    cmd = [config.XRAY_SERVICE_RESTART] if hasattr(config, "XRAY_SERVICE_RESTART") \
        else ["systemctl", "restart", config.XRAY_SERVICE]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n", *cmd]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise XrayError("could not restart Xray: "
                        + (proc.stderr.strip() or proc.stdout.strip()))


# ---------------------------------------------------------------------------
# live user management (no restart, existing connections survive)
# ---------------------------------------------------------------------------

def add_user_live(conn: sqlite3.Connection, email: str, user_uuid: str) -> None:
    port = int(get_listen_port(conn))
    payload = {
        "inbounds": [{
            "tag": config.INBOUND_TAG,
            "listen": "0.0.0.0",
            "port": port,
            "protocol": "vless",
            "settings": {
                "decryption": "none",
                "clients": [{"id": user_uuid, "email": email,
                             "flow": config.FLOW, "level": 0}],
            },
        }]
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as tmp:
        json.dump(payload, tmp)
        tmp_path = tmp.name
    try:
        out = _run([config.XRAY_BIN, "api", "adu",
                    f"--server={config.XRAY_API}", tmp_path])
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    # adu exits 0 even on failure; "already exists" is fine (idempotent).
    if "Added 1 user" not in out and "already exists" not in out:
        raise XrayError(f"adding user failed: {out.strip()}")


def remove_user_live(email: str) -> None:
    out = _run([config.XRAY_BIN, "api", "rmu",
                f"--server={config.XRAY_API}",
                f"-tag={config.INBOUND_TAG}", email])
    if "Removed 1 user" not in out and "not found" not in out:
        raise XrayError(f"removing user failed: {out.strip()}")


def get_listen_port(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'listen_port'"
    ).fetchone()
    return row["value"] if row else "443"


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------

_STAT_RE = re.compile(r"^user>>>(?P<email>.+)>>>traffic>>>(?P<dir>uplink|downlink)$")


def pull_traffic(reset: bool = True) -> dict[str, dict[str, int]]:
    """Drain per-user counters. Returns {email: {"up", "down"}} for the
    interval since the previous call (counters are reset)."""
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
    """{email: connected_device_count}. IP addresses themselves are fetched
    but only counted, never returned or stored."""
    parsed = _run_json([config.XRAY_BIN, "api", "statsonlineiplist",
                        f"--server={config.XRAY_API}", "-all"])
    counts: dict[str, int] = {}
    for user in parsed.get("users") or []:
        email = user.get("email")
        if email:
            counts[email] = len(user.get("ips") or [])
    return counts


# ---------------------------------------------------------------------------
# process helpers
# ---------------------------------------------------------------------------

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
