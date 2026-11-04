"""Ephemeral live traffic monitor.

Normally Xray records nothing. On a server that sits inside Iran, a log of who
connected from where and which hostnames they reached is the one artifact that
must not exist, so the default is to keep none.

This opens a short, self-expiring window so you can watch traffic with your
own eyes and confirm that sniffing really works:

  * the log goes under /run, which is RAM on Ubuntu, never the disk
  * it reads Xray's *diagnostic* log, not its access log. The access log is
    the one that contains client source addresses; it stays switched off, so
    no source IP is ever written anywhere
  * only the hostname, the original destination and a timestamp are kept, in
    a bounded in-memory ring
  * the window closes on its own and the file is deleted
  * if the file passes a size cap the monitor stops itself

The pairing of the two values is the proof. Xray logs the destination it was
handed and then the name it recovered from the traffic:

    received request for tcp:149.6.x.x:443      <- what the client asked for
    sniffed domain: rubika.ir                   <- what sniffing recovered
    default route for tcp:rubika.ir:443         <- what the server connected to

Seeing a name in the third line is what tells you the DNS lookup happened
here rather than on your phone, where the operator could have watched it.
"""
from __future__ import annotations

import ipaddress
import re
import sqlite3
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any

from . import db

# /run is a tmpfs on Ubuntu: this never touches persistent storage.
MONITOR_DIR = Path("/run/ipiran")
LOG_PATH = MONITOR_DIR / "trace.log"

MAX_MINUTES = 30
DEFAULT_MINUTES = 10
MAX_BYTES = 12 * 1024 * 1024
RING_SIZE = 250
PENDING_MAX = 600

_ring: deque[dict[str, Any]] = deque(maxlen=RING_SIZE)
_pending: OrderedDict[str, dict[str, Any]] = OrderedDict()
_offset = 0
_lock = threading.Lock()

_TIME_RE = re.compile(r"^\d{4}/\d{2}/\d{2} (?P<time>\d{2}:\d{2}:\d{2})")
_RECV_RE = re.compile(
    r"\[(?P<id>\d+)\] proxy/\S+?: received request for (?P<net>\w+):(?P<dest>\S+)"
)
_SNIFF_RE = re.compile(
    r"\[(?P<id>\d+)\] app/dispatcher: sniffed domain: (?P<host>\S+)"
)
_ROUTE_RE = re.compile(
    r"\[(?P<id>\d+)\] [^:]*(?:app/dispatcher|app/router)\S*: .*?route(?: for)? "
    r"\[?(?P<net>\w+):(?P<dest>[^\s\]]+)"
)


class MonitorError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# window state (persisted so a panel restart does not lose track)
# ---------------------------------------------------------------------------

def active_until(conn: sqlite3.Connection) -> int:
    try:
        return int(db.get_setting(conn, "monitor_until", "0") or 0)
    except ValueError:
        return 0


def is_active(conn: sqlite3.Connection) -> bool:
    return active_until(conn) > int(time.time())


def seconds_left(conn: sqlite3.Connection) -> int:
    return max(0, active_until(conn) - int(time.time()))


def log_overrides(conn: sqlite3.Connection) -> dict[str, Any]:
    """Extra `log` keys for the generated Xray config.

    The access log is never enabled: it is the one that would record client
    source addresses. Only the diagnostic log is redirected, and only while
    the window is open.
    """
    if not is_active(conn):
        return {}
    return {"error": str(LOG_PATH), "loglevel": "info"}


def arm(conn: sqlite3.Connection, minutes: int = DEFAULT_MINUTES) -> int:
    """Mark the window open. The caller must then rewrite the config and
    restart Xray for it to take effect."""
    minutes = max(1, min(int(minutes), MAX_MINUTES))
    if not MONITOR_DIR.is_dir():
        raise MonitorError(
            f"{MONITOR_DIR} does not exist, so enabling the log would stop Xray "
            "from starting. Re-run the installer to create it."
        )
    until = int(time.time()) + minutes * 60
    db.set_setting(conn, "monitor_until", str(until))
    with _lock:
        _ring.clear()
        _pending.clear()
        _reset_offset()
    return until


def disarm(conn: sqlite3.Connection) -> None:
    db.set_setting(conn, "monitor_until", "0")
    with _lock:
        _pending.clear()
        _reset_offset()
    # Unlinking works because the directory is group-writable by the panel
    # user, even though Xray owns the file itself.
    try:
        LOG_PATH.unlink()
    except OSError:
        pass


def _reset_offset() -> None:
    global _offset
    _offset = 0


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _split_dest(dest: str) -> tuple[str, str]:
    if dest.startswith("["):                       # [::1]:443
        host, _, port = dest.partition("]:")
        return host.lstrip("["), port
    host, _, port = dest.rpartition(":")
    return (host, port) if host else (dest, "")


def _touch(conn_id: str, stamp: str) -> dict[str, Any]:
    entry = _pending.get(conn_id)
    if entry is None:
        entry = {"time": stamp, "raw_host": "", "raw_port": "",
                 "host": "", "port": "", "network": ""}
        _pending[conn_id] = entry
        while len(_pending) > PENDING_MAX:
            _pending.popitem(last=False)
    return entry


def _finalise(conn_id: str) -> None:
    entry = _pending.pop(conn_id, None)
    if not entry or not entry["host"]:
        return
    sniffed = not _is_ip(entry["host"])
    _ring.append({
        "time": entry["time"],
        "host": entry["host"],
        "port": entry["port"] or entry["raw_port"],
        "raw": (f"{entry['raw_host']}:{entry['raw_port']}"
                if entry["raw_host"] and sniffed else ""),
        "network": entry["network"],
        "sniffed": sniffed,
    })


def _consume(line: str) -> None:
    stamp_match = _TIME_RE.match(line)
    stamp = stamp_match.group("time") if stamp_match else ""

    match = _RECV_RE.search(line)
    if match:
        entry = _touch(match.group("id"), stamp)
        host, port = _split_dest(match.group("dest"))
        entry["raw_host"], entry["raw_port"] = host, port
        entry["network"] = match.group("net")
        return

    match = _SNIFF_RE.search(line)
    if match:
        entry = _touch(match.group("id"), stamp)
        entry["host"] = match.group("host")
        return

    match = _ROUTE_RE.search(line)
    if match:
        conn_id = match.group("id")
        entry = _touch(conn_id, stamp)
        host, port = _split_dest(match.group("dest"))
        entry["host"], entry["port"] = host, port
        entry["network"] = entry["network"] or match.group("net")
        _finalise(conn_id)


def drain() -> str:
    """Read whatever Xray appended since the last call.

    Returns "" normally, or "too_big" when the file passed the cap and the
    caller should stop the monitor.
    """
    global _offset
    with _lock:
        try:
            size = LOG_PATH.stat().st_size
        except OSError:
            _offset = 0
            return ""
        if size > MAX_BYTES:
            return "too_big"
        if size < _offset:              # file replaced underneath us
            _offset = 0
        if size == _offset:
            return ""
        try:
            with LOG_PATH.open("r", errors="replace") as fh:
                fh.seek(_offset)
                chunk = fh.read()
                _offset = fh.tell()
        except OSError:
            return ""
        for line in chunk.splitlines():
            _consume(line)
    return ""


def entries(limit: int = 120) -> list[dict[str, Any]]:
    items = list(_ring)[-limit:]
    items.reverse()                     # newest first
    return items


def summary() -> dict[str, Any]:
    items = list(_ring)
    hosts = [e["host"] for e in items if e["sniffed"]]
    return {
        "total": len(items),
        "sniffed": len(hosts),
        "raw_ip": sum(1 for e in items if not e["sniffed"]),
        "unique_hosts": len(set(hosts)),
    }
