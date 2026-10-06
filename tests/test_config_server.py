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
