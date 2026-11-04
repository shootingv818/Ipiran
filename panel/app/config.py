"""Runtime configuration, all overridable through environment variables.

The panel is intentionally dependency-free: it runs on the Python standard
library that ships with Ubuntu, so a fresh server needs nothing installed
beyond python3 and the xray binary.
"""
from __future__ import annotations

import os
from pathlib import Path


def _path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser()


# Panel state. The SQLite database is the single source of truth; the Xray
# config file is always regenerated from it.
DATA_DIR: Path = _path("IPIRAN_DATA_DIR", "/var/lib/ipiran")
DB_PATH: Path = DATA_DIR / "panel.db"

# Xray on this server.
XRAY_BIN: str = os.environ.get("IPIRAN_XRAY_BIN", "/usr/local/bin/xray")
XRAY_CONFIG: Path = _path("IPIRAN_XRAY_CONFIG", "/usr/local/etc/xray/config.json")
XRAY_API: str = os.environ.get("IPIRAN_XRAY_API", "127.0.0.1:10085")
XRAY_SERVICE: str = os.environ.get("IPIRAN_XRAY_SERVICE", "xray")
XRAY_ASSETS: str = os.environ.get("XRAY_LOCATION_ASSET", "/usr/local/share/xray")

# Tag of the client-facing inbound. Must stay in sync with xray_conf.
INBOUND_TAG: str = "clients-in"

# Panel HTTP listener. Binds localhost by default so it is only reachable
# through the secret-path URL behind the system's TLS, or an SSH tunnel.
HOST: str = os.environ.get("IPIRAN_HOST", "127.0.0.1")
PORT: int = int(os.environ.get("IPIRAN_PORT", "8777"))

# Secret path prefix. The whole panel lives under /<SECRET>/ so that a bare
# scan of the IP and port finds nothing. Generated on first setup.
SECRET_PATH: str = os.environ.get("IPIRAN_SECRET_PATH", "")

# How often the background worker drains counters and refreshes online state.
POLL_SECONDS: int = int(os.environ.get("IPIRAN_POLL_SECONDS", "15"))

# Usage samples older than this are purged. Keeps the DB from holding a
# long history of behaviour on a server that lives inside Iran.
USAGE_RETENTION_DAYS: int = int(os.environ.get("IPIRAN_USAGE_RETENTION_DAYS", "60"))

# Defaults applied to a new client when the form leaves them blank.
DEFAULT_QUOTA_GB: float = float(os.environ.get("IPIRAN_DEFAULT_QUOTA_GB", "20"))
DEFAULT_EXPIRE_DAYS: int = int(os.environ.get("IPIRAN_DEFAULT_EXPIRE_DAYS", "30"))

FLOW = "xtls-rprx-vision"
