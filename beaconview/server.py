"""HTTP: the JSON API and the pages. Stdlib only, one thread per connection."""

import base64
import gzip
import hashlib
import hmac
import json
import mimetypes
import logging
import os
import sys
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

from . import __version__, query

log = logging.getLogger("beaconview.http")

STATS_CACHE_SECONDS = 10
ANSWER_CACHE_SECONDS = 5      # identical queries within this window share one answer (see AnswerCache)
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
PAGES = {"/": "overview.html", "/explore": "explore.html"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


class AnswerCache:
    """A short-lived cache of query answers, with single flight: identical requests arriving together run the
    query once and share the result. Pages round relative ranges to 5 s, so every open tab watching "last hour"
    asks the same question and ten tabs cost about what one does."""

    def __init__(self, ttl=ANSWER_CACHE_SECONDS, size=256):
        self.ttl, self.size = ttl, size
        self.data = OrderedDict()       # key -> (stored at, answer)
        self.inflight = {}              # key -> Event
        self.lock = threading.Lock()
        self.hits = self.misses = 0

    def get(self, key, compute):
        with self.lock:
            hit = self.data.get(key)
            if hit and time.monotonic() - hit[0] < self.ttl:
                self.data.move_to_end(key)
                self.hits += 1
                return hit[1]
            event = self.inflight.get(key)
            owner = event is None
            if owner:
                event = self.inflight[key] = threading.Event()
        if not owner:
            event.wait(query.QUERY_TIMEOUT_SECONDS + 5)
            with self.lock:
                hit = self.data.get(key)
                if hit:
                    self.hits += 1
                    return hit[1]
            return compute()          # the first caller failed; answer (or fail) on our own
        try:
            answer = compute()
            with self.lock:
                self.misses += 1
                self.data[key] = (time.monotonic(), answer)
                self.data.move_to_end(key)
                while len(self.data) > self.size:
                    self.data.popitem(last=False)
            return answer
        finally:
            with self.lock:
                self.inflight.pop(key, None)
            event.set()


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
        self.runner = query.Runner(store)
        self.answers = AnswerCache()
        self._static = {}

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

    def static_file(self, name):
        """(bytes, gzipped bytes or None, content type, etag) for a file under static/, or None. Cached in memory
        (the files are small) unless BEACONVIEW_DEV is set, so edits show without a restart."""
        hit = None if os.environ.get("BEACONVIEW_DEV") else self._static.get(name)
        if hit is None:
            path = os.path.normpath(os.path.join(STATIC_DIR, name))
            if not path.startswith(STATIC_DIR + os.sep) or not os.path.isfile(path):
                return None
            with open(path, "rb") as f:
                data = f.read()
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            if path.endswith(".js"):
                ctype = "text/javascript"
            if ctype.startswith("text/") or ctype == "image/svg+xml":
                ctype += "; charset=utf-8"
            gz = gzip.compress(data, 6) if len(data) > 1024 else None
            hit = (data, gz, ctype, '"' + hashlib.sha1(data).hexdigest()[:16] + '"')
            self._static[name] = hit
        return hit

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
        url = urlparse(self.path)
        path = url.path
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
            elif path.startswith("/api/"):
                self.api(path, parse_qs(url.query))
            elif path in PAGES:
                self.send_static(PAGES[path])
            elif path.startswith("/static/"):
                self.send_static(path[len("/static/"):])
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

    def api(self, path, params):
        runner = self.app.runner
        try:
            if path == "/api/fields":
                body = runner.fields()
                body["instances"] = [{"instance": s.instance, "format": s.format} for s in self.app.cfg.sources]
                body["retention"] = {"raw_days": self.app.cfg.retention_days,
                                     "rollup_days": self.app.cfg.rollup_retention_days}
                st = self.app.storage()
                body["data"] = {"oldest_ms": st["oldest_ms"], "newest_ms": st["newest_ms"]}
                self.send_json(body)
                return
            if path not in ("/api/summary", "/api/series", "/api/breakdown", "/api/top", "/api/logs"):
                self.send_json({"error": "not found"}, 404)
                return
            q = query.parse(params)
            compute = lambda: self.answer(path, q, params)   # noqa: E731
            if "after" in params:
                # following the log: each tab asks for its own newest rows, never worth caching
                self.send_json(compute())
            else:
                key = path + "?" + urlencode(sorted((k, v) for k, vs in params.items() for v in vs))
                self.send_json(self.app.answers.get(key, compute))
        except query.BadQuery as e:
            self.send_json({"error": str(e)}, 400)
        except query.QueryTimeout as e:
            self.send_json({"error": str(e)}, 504)

    def answer(self, path, q, params):
        runner = self.app.runner
        if path == "/api/summary":
            return runner.summary(q)
        if path == "/api/series":
            return runner.series(q, buckets=_int(params, "buckets", 120), split=params.get("split", [None])[0] or None,
                                 measure=params.get("measure", ["requests"])[0])
        if path == "/api/breakdown":
            by = params.get("by", ["service"])[0]
            return runner.breakdown(q, None if by in ("", "none") else by, sort=params.get("sort", ["requests"])[0],
                                    limit=_int(params, "limit", 20), with_unique=params.get("unique", [""])[0] == "1")
        if path == "/api/top":
            return runner.top(q, params.get("field", ["path"])[0], limit=_int(params, "limit", 10))
        after = params.get("after", [None])[0]
        return runner.logs(q, limit=_int(params, "limit", 100), before=_cursor(params, "before"),
                           after_seq=int(after) if after and after.isdigit() else None,
                           since=_cursor(params, "since"))

    def send_static(self, name):
        hit = self.app.static_file(name)
        if hit is None:
            self.send_json({"error": "not found"}, 404)
            return
        data, gz, ctype, etag = hit
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        use_gz = gz is not None and "gzip" in (self.headers.get("Accept-Encoding") or "")
        extra = {"ETag": etag, "Cache-Control": "no-cache", "Vary": "Accept-Encoding"}
        if use_gz:
            extra["Content-Encoding"] = "gzip"
        if ctype.startswith("text/html"):
            extra["Content-Security-Policy"] = CSP
            extra["Referrer-Policy"] = "no-referrer"
        self.send_body(200, gz if use_gz else data, ctype, extra)

    def send_json(self, obj, status=200):
        self.send_body(status, json.dumps(obj, separators=(",", ":")).encode(), "application/json")

    def send_text(self, text, status=200):
        self.send_body(status, text.encode(), "text/plain; charset=utf-8")

    def send_body(self, status, body, ctype, extra=None):
        if extra is None and len(body) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            body = gzip.compress(body, 5)
            extra = {"Content-Encoding": "gzip", "Vary": "Accept-Encoding"}
        headers = {"Cache-Control": "no-store"}
        headers.update(extra or {})
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_HEAD = do_GET


def _cursor(params, name):
    """A log position, `<ts_ms>:<seq>`, or None."""
    raw = params.get(name, [None])[0]
    if not raw:
        return None
    try:
        ts_ms, seq = raw.split(":")
        return int(ts_ms), int(seq)
    except ValueError:
        raise query.BadQuery(f"{name} must be <ts_ms>:<seq>") from None


def _int(params, name, default):
    raw = params.get(name, [None])[0]
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError:
        raise query.BadQuery(f"{name} must be a whole number") from None


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
