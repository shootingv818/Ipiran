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
import secrets
import ssl
import threading
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config, db, links, monitor, service

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
        self._hits: dict[str, list] = defaultdict(lambda: [0, 0.0])
        self.ssl_ctx: ssl.SSLContext | None = None

    def get_request(self):
        """Accept a connection and, for TLS, defer the handshake to the
        worker thread. Wrapping the *listening* socket would run every TLS
        handshake inside this single accept loop, so one client that opened a
        socket and stalled (sitting on the cert warning, a half-open scan)
        would freeze the whole panel. Here the raw TCP accept returns
        immediately and the handshake happens per-connection, under a
        timeout, in its own thread."""
        sock, addr = self.socket.accept()
        sock.settimeout(30)
        if self.ssl_ctx is not None:
            sock = self.ssl_ctx.wrap_socket(
                sock, server_side=True, do_handshake_on_connect=False
            )
        return sock, addr

    def handle_error(self, request, client_address):
        """A reset or a failed TLS handshake from a scanner is routine noise
        here, not something worth a traceback in the journal."""
        pass

    def db(self):
        """Serialize DB access; sqlite connection is shared across threads."""
        return self._conn

    def allow(self, ip: str) -> bool:
        """Fixed-window rate limit, enough to make scanning for the secret
        path impractical without needing any state beyond a counter."""
        with self.lock:
            bucket = self._hits[ip]
            now = time.monotonic()
            if now - bucket[1] > 10:
                bucket[0], bucket[1] = 0, now
            bucket[0] += 1
            if len(self._hits) > 4096:
                self._hits.clear()
            return bucket[0] <= config.RATE_LIMIT


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
        self.send_header("X-Frame-Options", "DENY")
        # Keeps the secret path out of Referer headers if a link ever points out.
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "geolocation=(), camera=(), microphone=()")
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
        if not self.server.allow(self.client_address[0]):
            return self._bytes(b"", "text/plain", status=429)
        parsed = urlparse(self.path)
        ok, rel = self._secret_ok(parsed.path)
        if not ok:
            return self._not_found()
        try:
            if rel == "/":
                return self._serve_asset("index.html")
            if rel.startswith("/vendor/") or rel in ("/app.js", "/style.css"):
                return self._serve_asset(rel.lstrip("/"))
            if rel == "/api/state":
                return self._json(self._with_db(service.dashboard_state))
            if rel == "/api/path":
                peer = self.client_address[0]
                return self._json(
                    self._with_db(lambda c: service.connection_path(c, peer))
                )
            if rel == "/api/monitor":
                return self._json(self._with_db(service.monitor_feed))
            if rel.startswith("/api/clients/") and rel.endswith("/link"):
                cid = int(rel.split("/")[3])
                link = self._with_db(lambda c: service.client_share_link(c, cid))
                return self._bytes(link.encode(), "text/plain; charset=utf-8")
        except (service.ServiceError, links.LinkError, monitor.MonitorError) as exc:
            return self._json({"detail": str(exc)}, status=400)
        except Exception as exc:  # noqa: BLE001
            return self._json({"detail": str(exc)}, status=500)
        return self._not_found()

    def do_POST(self):
        if not self.server.allow(self.client_address[0]):
            return self._bytes(b"", "text/plain", status=429)
        parsed = urlparse(self.path)
        ok, rel = self._secret_ok(parsed.path)
        if not ok:
            return self._not_found()
        body = self._read_body()
        try:
            if rel == "/api/monitor/start":
                minutes = int(body.get("minutes") or monitor.DEFAULT_MINUTES)
                return self._json(
                    self._with_db(lambda c: service.monitor_start(c, minutes))
                )
            if rel == "/api/monitor/stop":
                return self._json(self._with_db(service.monitor_stop))
            if rel == "/api/secret/rotate":
                new = secrets.token_urlsafe(16)

                def _rotate(conn):
                    db.set_setting(conn, "secret_path", new)
                    return {"ok": True, "secret": new}

                result = self._with_db(_rotate)
                self.server.secret = new
                return self._json(result)
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
        except (service.ServiceError, links.LinkError, monitor.MonitorError) as exc:
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
        if target.suffix == ".html":
            # Stamp the asset URLs with each file's mtime so that after an
            # update the browser requests a new URL and cannot serve a stale
            # app.js/style.css against fresh HTML. The HTML itself is never
            # cached. This removes any need to clear the browser cache.
            body = self._versioned_html(target)
            return self._bytes(body, ctype, extra={"Cache-Control": "no-store"})
        self._bytes(target.read_bytes(), ctype, extra={"Cache-Control": "max-age=86400"})

    def _versioned_html(self, target: Path) -> bytes:
        html = target.read_text(encoding="utf-8")
        for asset in ("app.js", "style.css", "vendor/qrcode.js"):
            try:
                version = int((WEB_DIR / asset).stat().st_mtime)
            except OSError:
                continue
            html = html.replace(f'"{asset}"', f'"{asset}?v={version}"')
        return html.encode("utf-8")

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

    scheme = "http"
    if config.TLS_CERT and config.TLS_KEY:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(config.TLS_CERT, config.TLS_KEY)
        # Handshake is done per-connection in get_request, not on the
        # listening socket, so a stalled client cannot block the accept loop.
        server.ssl_ctx = ctx
        scheme = "https"

    threading.Thread(target=_poller, args=(server,), daemon=True).start()

    url_host = config.HOST if config.HOST != "0.0.0.0" else "<server-ip>"
    print(f"panel on {scheme}://{url_host}:{config.PORT}/{secret}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    run()
