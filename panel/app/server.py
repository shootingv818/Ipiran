"""A tiny HTTP server on the Python standard library.

Why no framework: this runs on a bare Ubuntu box inside Iran where installing
PyPI packages is unreliable and every extra dependency is extra attack
surface. http.server plus sqlite3 is enough for a single-admin panel.

Everything lives under a secret path prefix, so the panel is /<SECRET>/...
and a bare scan of the IP sees nothing. A background thread drains Xray's
counters on an interval.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config, db, links, service

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".woff2": "font/woff2",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
}


class Panel(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, secret: str):
        super().__init__(addr, Handler)
        self.secret = secret
        self.lock = threading.Lock()
        self._conn = db.connect()

    def db(self):
        """Serialize DB access; sqlite connection is shared across threads."""
        return self._conn


class Handler(BaseHTTPRequestHandler):
    server_version = "ipiran"
    protocol_version = "HTTP/1.1"

    # -- helpers ----------------------------------------------------------
    def _secret_ok(self, path: str) -> tuple[bool, str]:
        prefix = f"/{self.server.secret}"
        if path == prefix:
            return True, "/"
        if path.startswith(prefix + "/"):
            return True, path[len(prefix):]
        return False, ""

    def _json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, body: bytes, ctype: str, status: int = 200,
               extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _not_found(self):
        # Anything outside the secret path looks like an empty server.
        self._bytes(b"", "text/plain", status=404)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    def _with_db(self, fn):
        with self.server.lock:
            return fn(self.server.db())

    # -- routing ----------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        ok, rel = self._secret_ok(parsed.path)
        if not ok:
            return self._not_found()
        qs = parse_qs(parsed.query)
        try:
            if rel == "/":
                return self._serve_asset("index.html")
            if rel.startswith("/vendor/") or rel in ("/app.js", "/style.css"):
                return self._serve_asset(rel.lstrip("/"))
            if rel == "/api/state":
                return self._json(self._with_db(service.dashboard_state))
            if rel.startswith("/api/clients/") and rel.endswith("/link"):
                cid = int(rel.split("/")[3])
                link = self._with_db(lambda c: service.client_share_link(c, cid))
                return self._bytes(link.encode(), "text/plain; charset=utf-8")
        except (service.ServiceError, links.LinkError) as exc:
            return self._json({"detail": str(exc)}, status=400)
        except Exception as exc:  # noqa: BLE001
            return self._json({"detail": str(exc)}, status=500)
        return self._not_found()

    def do_POST(self):
        parsed = urlparse(self.path)
        ok, rel = self._secret_ok(parsed.path)
        if not ok:
            return self._not_found()
        body = self._read_body()
        try:
            if rel == "/api/clients":
                client = self._with_db(lambda c: service.create_client(
                    c,
                    label=body.get("label", ""),
                    quota_gb=_num(body.get("quota_gb")),
                    expires_days=_int(body.get("expires_days")),
                    note=body.get("note", ""),
                ))
                return self._json({"ok": True, "id": client["id"]})
            if rel == "/api/apply":
                self._with_db(service.rebuild)
                return self._json({"ok": True})
            if rel == "/api/poll":
                return self._json(self._with_db(service.poll))
            if rel.startswith("/api/clients/"):
                parts = rel.split("/")
                cid = int(parts[3])
                action = parts[4] if len(parts) > 4 else ""
                if action == "delete":
                    self._with_db(lambda c: service.delete_client(c, cid))
                    return self._json({"ok": True})
                self._with_db(lambda c: service.update_client(c, cid, **body))
                return self._json({"ok": True})
        except (service.ServiceError, links.LinkError) as exc:
            return self._json({"detail": str(exc)}, status=400)
        except Exception as exc:  # noqa: BLE001
            return self._json({"detail": str(exc)}, status=500)
        return self._not_found()

    def _serve_asset(self, rel: str):
        # Resolve inside WEB_DIR only.
        target = (WEB_DIR / rel).resolve()
        if not str(target).startswith(str(WEB_DIR.resolve())) or not target.is_file():
            return self._not_found()
        ctype = _CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        cache = "no-store" if target.suffix in (".html",) else "max-age=86400"
        self._bytes(target.read_bytes(), ctype, extra={"Cache-Control": cache})

    def log_message(self, *args):  # silence default stderr logging
        pass


def _num(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v):
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _poller(server: Panel):
    while True:
        time.sleep(config.POLL_SECONDS)
        try:
            with server.lock:
                service.poll(server.db())
        except Exception:  # noqa: BLE001 - a bad poll must not kill the thread
            pass


def run() -> None:
    conn = db.init()
    secret = db.get_setting(conn, "secret_path")
    conn.close()

    server = Panel((config.HOST, config.PORT), secret)
    threading.Thread(target=_poller, args=(server,), daemon=True).start()

    url_host = config.HOST if config.HOST != "0.0.0.0" else "<server-ip>"
    print(f"panel on http://{url_host}:{config.PORT}/{secret}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    run()
