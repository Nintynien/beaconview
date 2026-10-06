"""HTTP: the JSON API and the pages. Stdlib only, one thread per connection."""

import base64
import gzip
import hashlib
import hmac
import json
import mimetypes
import select
import socket
import logging
import os
import sys
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

from . import __version__, query
from .settings import SettingsError

log = logging.getLogger("beaconview.http")

STATS_CACHE_SECONDS = 10
ANSWER_CACHE_SECONDS = 2      # identical queries within this window share one answer (see AnswerCache)
STREAM_LIFETIME = 600         # a stream ends after this long; the browser reconnects (and resumes) on its own
STREAM_HEARTBEAT = 15         # a comment line this often keeps proxies from closing a quiet stream
STREAM_CHECK = 5              # how often an idle stream checks whether the browser is still there
MAX_STREAMS = 50              # beyond this, /api/stream answers 503 and pages fall back to polling
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
PAGES = {"/": "overview.html", "/explore": "explore.html", "/settings": "settings.html"}
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
        self.settings = ingestor.settings
        self.hub = ingestor.hub
        self.streams = 0
        self._streams_lock = threading.Lock()
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
            "retention": {"raw_days": self.settings.retention_days, "rollup_days": self.settings.rollup_retention_days},
            "streams": self.streams,
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

    def acquire_stream(self):
        with self._streams_lock:
            if self.streams >= MAX_STREAMS:
                return False
            self.streams += 1
            return True

    def release_stream(self):
        with self._streams_lock:
            self.streams -= 1

    def restart_only(self):
        """The settings that need a restart, for the settings page (read-only there)."""
        c = self.cfg
        return [
            {"label": "Logs", "env": "ACCESS_LOG / ACCESS_LOGS",
             "value": ", ".join(f"{s.instance}: {s.path} ({s.format})" for s in c.sources)},
            {"label": "Read rotated logs on first start", "env": "BACKFILL", "value": "on" if c.backfill else "off"},
            {"label": "Data directory", "env": "DATA_DIR", "value": c.data_dir},
            {"label": "Database memory limit", "env": "DB_MEMORY_LIMIT", "value": c.db_memory_limit},
            {"label": "Database threads", "env": "DB_THREADS", "value": str(c.db_threads)},
            {"label": "Port", "env": "PORT", "value": str(c.port)},
            {"label": "Built-in authentication", "env": "BASIC_AUTH_USER / BASIC_AUTH_PASSWORD",
             "value": "on" if c.basic_auth_user else "off (use your proxy's authentication)"},
        ]

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
            elif path == "/api/stream":
                self.stream(parse_qs(url.query))
            elif path == "/api/settings":
                self.send_json(self.settings_body())
            elif path == "/api/settings/preview":
                self.send_json(self.settings_preview(parse_qs(url.query)))
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

    do_HEAD = do_GET

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if not self.app.check_auth(self.headers.get("Authorization")):
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="BeaconView", charset="UTF-8"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path != "/api/settings":
                self.send_json({"error": "not found"}, 404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= 16384:
                self.send_json({"error": "expected a small JSON body"}, 400)
                return
            try:
                changes = json.loads(self.rfile.read(length))
                if not isinstance(changes, dict):
                    raise ValueError
            except ValueError:
                self.send_json({"error": "the body must be a JSON object of settings"}, 400)
                return
            try:
                changed = self.app.settings.update(changes)
            except SettingsError as e:
                self.send_json({"error": str(e)}, 400)
                return
            body = self.settings_body()
            body["changed"] = sorted(changed)
            self.send_json(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception:
            log.exception("error handling POST %s", self.path)
            self.send_json({"error": "internal error"}, 500)

    def settings_body(self):
        return {"editable": self.app.settings.describe(), "restart_only": self.app.restart_only()}

    def settings_preview(self, params):
        """What saving these retentions would delete now, so the page can say so before it happens."""
        def days(name):
            raw = params.get(name, [None])[0]
            try:
                return float(raw) if raw not in (None, "") else getattr(self.app.settings, name)
            except ValueError:
                return getattr(self.app.settings, name)
        now = int(time.time() * 1000)
        raw_cut = now - int(days("retention_days") * 86_400_000)
        roll_cut = now - int(max(days("rollup_retention_days"), days("retention_days")) * 86_400_000)
        run = self.app.runner.run
        requests = run("SELECT count(*) FROM requests WHERE ts < epoch_ms(?::BIGINT)", [raw_cut])[0][0]
        rollups = run("SELECT count(*) FROM minute_stats WHERE minute < epoch_ms(?::BIGINT)", [roll_cut])[0][0]
        return {"requests_deleted": requests, "rollup_rows_deleted": rollups, "requests_cutoff_ms": raw_cut,
                "rollups_cutoff_ms": roll_cut}

    def stream(self, params):
        """Server-Sent Events. kind=logs: new requests matching the filters, as they are written (event "rows",
        id = the newest seq sent, so a reconnect resumes with Last-Event-ID). kind=changes: a "change" event when
        the data moved, at most every min_interval seconds; the page then re-fetches what it shows. A logs stream
        with changes=1 carries both, so a live Explore tab needs one connection."""
        kind = params.get("kind", ["logs"])[0]
        want_changes = kind == "changes" or params.get("changes", [""])[0] == "1"
        if kind not in ("logs", "changes"):
            self.send_json({"error": "kind must be logs or changes"}, 400)
            return
        try:
            q = query.parse(params)
            min_interval = min(300.0, max(0.5, float(params.get("min_interval", ["1"])[0])))
        except (query.BadQuery, ValueError) as e:
            self.send_json({"error": str(e)}, 400)
            return
        last = self.headers.get("Last-Event-ID") or params.get("after", [""])[0]
        after = int(last) if last.isdigit() else None
        if not self.app.acquire_stream():
            self.send_json({"error": "too many open streams; polling instead"}, 503)
            return
        hub = self.app.hub
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")       # nginx-style proxies: do not buffer
            self.send_header("Connection", "close")           # no Content-Length: the stream ends when we close
            self.end_headers()
            self.close_connection = True
            version, max_seq, _ = hub.snapshot()
            if after is None:
                after = max_seq                                # start from now
            self._sse("retry: 3000\n\n")
            self._sse(f"event: hello\ndata: {json.dumps({'max_seq': max_seq})}\n\n")
            deadline = time.monotonic() + STREAM_LIFETIME
            wrote = last_change = time.monotonic()
            pending = False
            if kind == "logs" and after < max_seq:
                version = -1                                   # resuming: send what was missed right away
            while time.monotonic() < deadline and not hub.closed:
                if self._client_gone():
                    break
                wait = STREAM_CHECK
                if pending:
                    wait = max(0.05, min_interval - (time.monotonic() - last_change))
                new_version, max_seq, rollup_version = hub.wait(version, wait)
                if hub.closed:
                    break
                moved = new_version != version
                version = new_version
                if kind == "logs" and moved and max_seq > after:
                    now = int(time.time() * 1000)
                    live = query.Query(q.from_ms, now + 60_000, q.conds, q.min_us, q.max_us, q.headers)
                    page = self.app.runner.logs(live, limit=500, after_seq=after)
                    if page["rows"]:
                        top = max(r[0] for r in page["rows"])
                        self._sse(f"event: rows\nid: {top}\ndata: {json.dumps(page, separators=(',', ':'))}\n\n")
                        wrote = time.monotonic()
                    # rows up to max_seq that did not match never will: skip past them for good
                    after = max(after, max_seq, max((r[0] for r in page["rows"]), default=0))
                if want_changes and moved and version > 0:
                    pending = True
                if pending and time.monotonic() - last_change >= min_interval:
                    self._sse(f"event: change\ndata: {json.dumps({'max_seq': max_seq, 'rollup': rollup_version})}\n\n")
                    last_change = wrote = time.monotonic()
                    pending = False
                if time.monotonic() - wrote >= STREAM_HEARTBEAT:
                    self._sse(": ping\n\n")
                    wrote = time.monotonic()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass                                               # the tab went away
        finally:
            self.app.release_stream()

    def _client_gone(self):
        """True once the browser has closed the connection (it never sends anything on a stream, so a readable
        socket means end of file)."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def _sse(self, text):
        self.wfile.write(text.encode("utf-8"))
        self.wfile.flush()

    def api(self, path, params):
        runner = self.app.runner
        try:
            if path == "/api/fields":
                body = runner.fields()
                body["instances"] = [{"instance": s.instance, "format": s.format} for s in self.app.cfg.sources]
                body["retention"] = {"raw_days": self.app.settings.retention_days,
                                     "rollup_days": self.app.settings.rollup_retention_days}
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
