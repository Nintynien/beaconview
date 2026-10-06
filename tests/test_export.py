import csv
import http.client
import io
import threading
import time

import pytest
from helpers import traefik_line

from beaconview.ingest import Ingestor
from beaconview.parsers import TraefikParser
from beaconview.server import App, make_server
from beaconview.store import Store
from conftest import make_cfg

NOW = int(time.time() * 1000)


@pytest.fixture
def site(tmp_path):
    cfg = make_cfg(tmp_path)
    store = Store(cfg.db_path, "256MB", 2)
    p = TraefikParser()
    rows = [
        (1, "edge") + p.parse(traefik_line(NOW - 60_000)) + ("US",),
        (2, "edge") + p.parse(traefik_line(NOW - 50_000, DownstreamStatus=502, RequestHost="b.com",
                                           RequestPath='/a,"quoted"\nline?x=1')) + ("DE",),
        (3, "edge") + p.parse(traefik_line(NOW - 40_000, **{"request_User-Agent": "=HYPERLINK(\"http://evil\")"})) + ("US",),
    ]
    store.write_batch(rows, [])
    store.rollup()
    ing = Ingestor(cfg, store)
    ing.start()
    server = make_server(App(cfg, store, ing), host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address
    server.shutdown()
    ing.stop(); ing.join(10); store.close()


def get(addr, path):
    conn = http.client.HTTPConnection(*addr, timeout=10)
    conn.request("GET", path)
    r = conn.getresponse()
    body = r.read()
    conn.close()
    return r.status, {k.lower(): v for k, v in r.getheaders()}, body


def table(body):
    return list(csv.reader(io.StringIO(body.decode("utf-8"))))


RANGE = f"from={NOW - 3_600_000}&to={NOW + 60_000}"


def test_requests_csv(site):
    status, headers, body = get(site, f"/api/export?kind=logs&{RANGE}")
    assert status == 200 and headers["content-type"].startswith("text/csv")
    assert headers["content-disposition"].startswith('attachment; filename="beaconview-logs-')
    rows = table(body)
    assert rows[0][:4] == ["time_utc", "instance", "method", "host"] and len(rows) == 4
    newest, middle, oldest = rows[1:]
    head = rows[0]
    assert newest[head.index("user_agent")] == "'=HYPERLINK(\"http://evil\")"   # cannot run as a formula
    assert middle[head.index("path")] == '/a,"quoted"\nline'                   # commas, quotes, newlines survive
    assert middle[head.index("status")] == "502" and middle[head.index("country")] == "DE"
    assert oldest[head.index("duration_ms")] == "1.234" and oldest[head.index("time_utc")].endswith("Z")


def test_filters_apply(site):
    rows = table(get(site, f"/api/export?kind=logs&{RANGE}&status=5xx")[2])
    assert len(rows) == 2 and rows[1][rows[0].index("host")] == "b.com"
    rows = table(get(site, f"/api/export?kind=logs&{RANGE}&country=US")[2])
    assert len(rows) == 3


def test_breakdown_and_series_csv(site):
    rows = table(get(site, f"/api/export?kind=breakdown&by=host&{RANGE}")[2])
    assert rows[0][:3] == ["host", "requests", "requests_per_sec"]
    assert {r[0]: r[1] for r in rows[1:]} == {"example.com": "2", "b.com": "1"}
    assert rows[1][rows[0].index("unique_ips")] in ("1", "2")
    rows = table(get(site, f"/api/export?kind=breakdown&by=none&{RANGE}")[2])
    assert rows[1][:2] == ["(all)", "3"]
    rows = table(get(site, f"/api/export?kind=series&buckets=20&split=status_class&{RANGE}")[2])
    assert rows[0][0] == "bucket_start_utc" and set(rows[0][1:]) == {"2xx", "5xx"}
    assert sum(int(v) for r in rows[1:] for v in r[1:]) == 3


def test_bad_export(site):
    assert get(site, f"/api/export?kind=nope&{RANGE}")[0] == 400
    assert get(site, f"/api/export?kind=logs&status=teapot&{RANGE}")[0] == 400
