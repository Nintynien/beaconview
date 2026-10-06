import base64
import http.client
import json
import threading

import pytest
from conftest import make_cfg

from beaconview import config
from beaconview.ingest import Ingestor
from beaconview.server import App, make_server
from beaconview.store import Store


def test_sources():
    one = config.parse_sources("", "/l/access.log", "host1", "traefik")
    assert one == (config.LogSource("host1", "/l/access.log", "traefik"),)
    many = config.parse_sources(r"a=/x.log, b=traefik:/y.log, c=C:\logs\z.log", "", "", "traefik")
    assert [(s.instance, s.path, s.format) for s in many] == [
        ("a", "/x.log", "traefik"), ("b", "/y.log", "traefik"), ("c", r"C:\logs\z.log", "traefik")]
    for bad in ("nameonly", "=path", "a=/x,a=/y"):
        with pytest.raises(config.ConfigError):
            config.parse_sources(bad, "", "", "traefik")
    with pytest.raises(config.ConfigError):
        config.parse_sources("", "/x", "h", "nginx-json")


def test_load_validates():
    with pytest.raises(config.ConfigError):
        config.load({"RETENTION_DAYS": "lots"})
    with pytest.raises(config.ConfigError):
        config.load({"BASIC_AUTH_USER": "me"})
    cfg = config.load({"RETENTION_DAYS": "30", "ROLLUP_RETENTION_DAYS": "7"})
    assert cfg.retention_days == 30 and cfg.rollup_retention_days == 30   # rollups never shorter than raw


@pytest.fixture
def site(tmp_path):
    def build(**env):
        cfg = make_cfg(tmp_path, **env)
        store = Store(cfg.db_path, "256MB", 2)
        ing = Ingestor(cfg, store)
        ing.start()
        server = make_server(App(cfg, store, ing), host="127.0.0.1", port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        built.append((server, ing, store))
        return server.server_address
    built = []
    yield build
    for server, ing, store in built:
        server.shutdown()
        ing.stop()
        ing.join(10)
        store.close()


def get(addr, path, headers=None):
    conn = http.client.HTTPConnection(*addr, timeout=10)
    conn.request("GET", path, headers=headers or {})
    r = conn.getresponse()
    body = r.read()
    conn.close()
    return r.status, dict(r.getheaders()), body


def test_status_and_health(site):
    addr = site()
    status, _, body = get(addr, "/api/status")
    d = json.loads(body)
    assert status == 200 and d["ingest"]["alive"] and d["storage"]["rows"] == 0
    assert d["ingest"]["sources"][0]["instance"] == "edge"
    assert get(addr, "/healthz")[0] == 200
    assert get(addr, "/nope")[0] == 404


def test_basic_auth(site):
    addr = site(BASIC_AUTH_USER="admin", BASIC_AUTH_PASSWORD="s3cret")
    status, headers, _ = get(addr, "/api/status")
    assert status == 401 and headers["WWW-Authenticate"].startswith("Basic")
    wrong = "Basic " + base64.b64encode(b"admin:nope").decode()
    assert get(addr, "/api/status", {"Authorization": wrong})[0] == 401
    right = "Basic " + base64.b64encode(b"admin:s3cret").decode()
    assert get(addr, "/api/status", {"Authorization": right})[0] == 200
    assert get(addr, "/healthz")[0] == 200            # the container probe needs no credentials


def test_api_routes_and_errors(site):
    addr = site()
    for path in ("/api/fields", "/api/summary", "/api/series?buckets=30&split=status_class",
                 "/api/top?field=ip", "/api/logs?limit=5"):
        status, _, body = get(addr, path)
        assert status == 200, (path, body)
        json.loads(body)
    status, _, body = get(addr, "/api/summary?status=teapot")
    assert status == 400 and "status" in json.loads(body)["error"]
    assert get(addr, "/api/series?split=nope")[0] == 400
    assert get(addr, "/api/logs?before=garbage")[0] == 400
    assert get(addr, "/api/series?buckets=lots")[0] == 400
    assert get(addr, "/api/nothing")[0] == 404


def test_pages_and_static(site):
    addr = site()
    status, headers, body = get(addr, "/explore")
    assert status == 200 and b"BeaconView" in body
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert get(addr, "/")[0] == 200
    status, headers, _ = get(addr, "/static/explore.js")
    assert status == 200 and headers["Content-Type"].startswith("text/javascript")
    etag = headers["ETag"]
    assert get(addr, "/static/explore.js", {"If-None-Match": etag})[0] == 304
    assert get(addr, "/static/../server.py")[0] == 404
    assert get(addr, "/static/%2e%2e/server.py")[0] == 404
    assert get(addr, "/static/nope.js")[0] == 404
