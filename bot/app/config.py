"""Runtime configuration for the Telegram-controlled relay bot.

The bot runs on the FOREIGN server (the only side that can reach Telegram).
That server is the entry point and middleware: clients connect to it, and it
forwards their traffic over a REALITY tunnel to one of several Iranian exit
servers, failing over automatically. The destination therefore sees an
Iranian IP, while the Iranian servers never see the client's real IP — only
the foreign server.

Standard library only, so a bare VPS needs nothing beyond python3 + xray.
"""
from __future__ import annotations

import os
from pathlib import Path


def _path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser()


# --- Telegram -------------------------------------------------------------
# Token from @BotFather, and the numeric id of the one person allowed to use
# the bot. Everyone else is ignored.
BOT_TOKEN: str = os.environ.get("IPSAZ_BOT_TOKEN", "")
OWNER_ID: int = int(os.environ.get("IPSAZ_OWNER_ID", "0") or "0")
# Optional: route Telegram API calls through a local proxy if the foreign
# server ever needs it. Empty = direct.
TELEGRAM_PROXY: str = os.environ.get("IPSAZ_TELEGRAM_PROXY", "")

# --- storage --------------------------------------------------------------
DATA_DIR: Path = _path("IPSAZ_DATA_DIR", "/var/lib/ipsaz")
DB_PATH: Path = DATA_DIR / "bot.db"

# --- local xray (on the foreign server) -----------------------------------
XRAY_BIN: str = os.environ.get("IPSAZ_XRAY_BIN", "/usr/local/bin/xray")
XRAY_CONFIG: Path = _path("IPSAZ_XRAY_CONFIG", "/usr/local/etc/xray/config.json")
XRAY_API: str = os.environ.get("IPSAZ_XRAY_API", "127.0.0.1:10100")
XRAY_SERVICE: str = os.environ.get("IPSAZ_XRAY_SERVICE", "xray")
XRAY_ASSETS: str = os.environ.get("XRAY_LOCATION_ASSET", "/usr/local/share/xray")

INBOUND_TAG: str = "clients-in"
BALANCER_TAG: str = "ir-balancer"
FLOW = "xtls-rprx-vision"

# --- client-facing REALITY inbound on the foreign server ------------------
# The foreign server disguises itself as this SNI. It must be a real TLS1.3 +
# HTTP/2 site reachable from the foreign server; the installer verifies it.
DEFAULT_SNI: str = os.environ.get("IPSAZ_SNI", "www.cloudflare.com")
LISTEN_PORT: int = int(os.environ.get("IPSAZ_PORT", "443"))

# --- behaviour ------------------------------------------------------------
POLL_SECONDS: int = int(os.environ.get("IPSAZ_POLL_SECONDS", "20"))
HEALTH_SECONDS: int = int(os.environ.get("IPSAZ_HEALTH_SECONDS", "30"))
USAGE_RETENTION_DAYS: int = int(os.environ.get("IPSAZ_USAGE_RETENTION_DAYS", "60"))
DEFAULT_QUOTA_GB: float = float(os.environ.get("IPSAZ_DEFAULT_QUOTA_GB", "20"))
DEFAULT_EXPIRE_DAYS: int = int(os.environ.get("IPSAZ_DEFAULT_EXPIRE_DAYS", "30"))

# Public address clients dial (the foreign server's IP or hostname).
# Stored in the DB once set via the bot; this is only the env seed.
PUBLIC_HOST: str = os.environ.get("IPSAZ_PUBLIC_HOST", "")
