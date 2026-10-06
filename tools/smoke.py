"""Smoke-test a running BeaconView: it is healthy, ingests the log it was given, and serves its pages and API.

    python tools/smoke.py http://127.0.0.1:8080 2000      # expect 2000 requests ingested

Used by CI against the freshly built image (see .github/workflows/build.yml); exits non-zero with a reason on the
first failure.
"""

import json
import sys
import time
import urllib.error
import urllib.request


def get(base, path, timeout=10):
    with urllib.request.urlopen(base + path, timeout=timeout) as r:
        return r.status, r.read()


def wait(what, check, seconds):
    end = time.monotonic() + seconds
    last = None
    while time.monotonic() < end:
        try:
            ok, last = check()
            if ok:
                return last
        except (OSError, urllib.error.URLError, ValueError) as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(1)
    sys.exit(f"FAIL: {what} (last seen: {last})")


def main():
    base, expected = sys.argv[1].rstrip("/"), int(sys.argv[2])

    wait("/healthz answers ok", lambda: (json.loads(get(base, "/healthz")[1]).get("ok") is True, "not ok"), 60)
    print("ok   /healthz")

    def ingested():
        src = json.loads(get(base, "/api/status")[1])["ingest"]["sources"][0]
        if src["error"]:
            sys.exit(f"FAIL: the log cannot be read: {src['error']}")
        return src["rows"] >= expected, f"{src['rows']} rows, {src['bad_lines']} unreadable"
    print("ok   ingest:", wait(f"{expected} requests ingested", ingested, 60))

    now = int(time.time() * 1000)
    summary = json.loads(get(base, f"/api/summary?from={now - 2 * 86_400_000}&to={now + 60_000}&min_ms=0")[1])
    if summary["requests"] != expected:
        sys.exit(f"FAIL: /api/summary counts {summary['requests']} requests, expected {expected}")
    print("ok   /api/summary:", summary["requests"], "requests")

    for path in ("/", "/explore", "/settings", "/static/app.css", "/static/explore.js", "/api/fields", "/api/settings",
                 f"/api/series?from={now - 3_600_000}&to={now}&buckets=60",
                 f"/api/logs?from={now - 2 * 86_400_000}&to={now + 60_000}&limit=5"):
        status, _ = get(base, path)
        if status != 200:
            sys.exit(f"FAIL: {path} answered {status}")
    print("ok   pages and API")
    print("PASS")


if __name__ == "__main__":
    main()
