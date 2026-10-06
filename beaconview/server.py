"""HTTP: the JSON API and (from milestone 2) the pages. Stdlib only, one thread per connection."""

import base64
import hmac
import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import __version__

log = logging.getLogger("beaconview.http")

STATS_CACHE_SECONDS = 10


class App:
    """What the handlers need: config, store, ingestor, and a short cache for the storage numbers (counting a
    billion rows on every health probe would be silly)."""

    def __init__(self, cfg, store, ingestor):
        self.cfg = cfg
        self.store = store
        self.ingestor = ingestor
        self.started = time.time()
        self._stats = None
        self._stats_at = 0.0
        self._stats_lock = threading.Lock()

    def storage(self):
        with self._stats_lock:
            if self._stats is None or time.monotonic() - self._stats_at > STATS_CACHE_SECONDS:
                self._stats = self.store.storage_stats()
                self._stats_at = time.monotonic()
            return self._stats

    def status(self):
        return {
            "version": __version__,
            "now_ms": int(time.time() * 1000),
            "uptime_s": int(time.time() - self.started),
            "process": {"cpu_s": round(time.process_time(), 2), "rss_bytes": rss_bytes()},
            "retention": {"raw_days": self.cfg.retention_days, "rollup_days": self.cfg.rollup_retention_days},
            "ingest": self.ingestor.status(),
            "storage": self.storage(),
        }

    def check_auth(self, header):
        if not self.cfg.basic_auth_user:
            return True
        if not header or not header.startswith("Basic "):
            return False
        try:
            user, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except ValueError:
            return False
        ok_user = hmac.compare_digest(user.encode(), self.cfg.basic_auth_user.encode())
        ok_pass = hmac.compare_digest(password.encode(), self.cfg.basic_auth_password.encode())
        return ok_user and ok_pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"beaconview/{__version__}"
    timeout = 60
    app = None   # set by make_server

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/healthz":
                # for the container's own probe: no auth, no data, just whether ingest is running
                alive = self.app.ingestor.is_alive()
                self.send_json({"ok": alive}, 200 if alive else 503)
                return
            if not self.app.check_auth(self.headers.get("Authorization")):
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="BeaconView", charset="UTF-8"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path == "/api/status":
                self.send_json(self.app.status())
            elif path == "/":
                self.send_text("BeaconView is collecting. The pages arrive in milestone 2; see /api/status.\n")
            else:
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception:
            log.exception("error handling %s", self.path)
            try:
                self.send_json({"error": "internal error"}, 500)
            except OSError:
                pass

    def send_json(self, obj, status=200):
        self.send_body(status, json.dumps(obj, separators=(",", ":")).encode(), "application/json")

    def send_text(self, text, status=200):
        self.send_body(status, text.encode(), "text/plain; charset=utf-8")

    def send_body(self, status, body, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_HEAD = do_GET


def make_server(app, host="0.0.0.0", port=8080):
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def rss_bytes():
    """Resident memory of this process, or None where we cannot tell cheaply."""
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) * 1024
        except OSError:
            return None
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                    (name, ctypes.c_size_t) for name in (
                        "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                        "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage",
                        "PeakPagefileUsage")]
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            c = Counters()
            c.cb = ctypes.sizeof(c)
            if k32.K32GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
                return c.WorkingSetSize
        except (OSError, AttributeError):
            return None
    return None
    return None
