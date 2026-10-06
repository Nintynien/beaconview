import http.client
import json
import threading
import time

import pytest
from conftest import make_cfg
from helpers import append, traefik_line, wait_for

from beaconview import server as server_mod
from beaconview.ingest import Ingestor
from beaconview.server import App, make_server
from beaconview.settings import Settings, SettingsError
from beaconview.store import Store


# ---------------------------------------------------------------- settings

def test_env_wins_and_locks(tmp_path):
    cfg = make_cfg(tmp_path, RETENTION_DAYS=3)
    store = Store(cfg.db_path, "256MB", 2)
    s = Settings(cfg, {"RETENTION_DAYS": "3"}, store)
    assert s.retention_days == 3 and s.locked == {"retention_days": "RETENTION_DAYS"}
    with pytest.raises(SettingsError, match="RETENTION_DAYS"):
        s.update({"retention_days": 10})
    assert s.update({"rollup_retention_days": 30}) == {"rollup_retention_days"}
    store.close()


def test_saved_values_survive_a_restart_unless_env_set_later(tmp_path):
    cfg = make_cfg(tmp_path)
    store = Store(cfg.db_path, "256MB", 2)
    Settings(cfg, {}, store).update({"retention_days": 14, "client_ip_header": "CF-Connecting-IP"})
    store.close()
    store = Store(cfg.db_path, "256MB", 2)
    s = Settings(cfg, {}, store)
    assert s.retention_days == 14 and s.client_ip_header == "CF-Connecting-IP"
    assert s.rollup_retention_days == 90
    # an environment variable added later wins over the saved value
    s2 = Settings(make_cfg(tmp_path, RETENTION_DAYS=2), {"RETENTION_DAYS": "2"}, store)
    assert s2.retention_days == 2
    store.close()


def test_validation(tmp_path):
    s = Settings(make_cfg(tmp_path))
    for bad in ({"retention_days": "lots"}, {"retention_days": 0}, {"port": 1},
                {"rollup_retention_days": 1, "retention_days": 7}):
        with pytest.raises(SettingsError):
            s.update(bad)
    assert s.retention_days == 7                      # nothing applied
    s.update({"retention_days": 120})                 # totals follow up when only the raw retention moves
    assert s.rollup_retention_days == 120
    assert s.update({"retention_days": 120}) == set()  # no change, nothing saved or announced


def test_changes_reach_the_running_ingest(tmp_path):
    cfg = make_cfg(tmp_path)
    store = Store(cfg.db_path, "256MB", 2)
    ing = Ingestor(cfg, store, settings=Settings(cfg, {}, store))
    ing.start()
    log = tmp_path / "access.log"
    append(log, traefik_line(**{"request_Cf-Connecting-Ip": "198.51.100.9"}))
    assert wait_for(lambda: store.reader().execute("SELECT count(*) FROM requests").fetchone()[0] == 1)
    ing.prune_due = 1e12
    ing.settings.update({"client_ip_header": "CF-Connecting-IP", "anonymize_ips": True, "retention_days": 3})
    assert ing.prune_due == 0.0                       # a retention change prunes soon
    append(log, traefik_line(**{"request_Cf-Connecting-Ip": "198.51.100.9"}))
    assert wait_for(lambda: store.reader().execute("SELECT count(*) FROM requests").fetchone()[0] == 2)
    ips = [r[0] for r in store.reader().execute("SELECT ip FROM requests ORDER BY seq").fetchall()]
    assert ips == ["203.0.113.7", "198.51.100.0"]     # the old row as it was, the new one header + anonymized
    ing.stop(); ing.join(10); store.close()


# ---------------------------------------------------------------- HTTP

@pytest.fixture
def live(tmp_path):
    cfg = make_cfg(tmp_path, RETENTION_DAYS=7)
    store = Store(cfg.db_path, "256MB", 2)
    ing = Ingestor(cfg, store, settings=Settings(cfg, {"RETENTION_DAYS": "7"}, store))
    ing.start()
    app = App(cfg, store, ing)
    srv = make_server(app, host="127.0.0.1", port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address, tmp_path / "access.log", app
    ing.hub.close()
    srv.shutdown()
    ing.stop(); ing.join(10); store.close()


def request(addr, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection(*addr, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r.status, data


def test_settings_api(live):
    addr, _, _ = live
    status, body = request(addr, "GET", "/api/settings")
    d = json.loads(body)
    by = {f["name"]: f for f in d["editable"]}
    assert status == 200 and by["retention_days"]["locked"] and by["retention_days"]["env"] == "RETENTION_DAYS"
    assert any(r["env"] == "DB_MEMORY_LIMIT" for r in d["restart_only"])
    status, body = request(addr, "POST", "/api/settings", json.dumps({"rollup_retention_days": 30}),
                           {"Content-Type": "application/json"})
    assert status == 200 and json.loads(body)["changed"] == ["rollup_retention_days"]
    status, body = request(addr, "POST", "/api/settings", json.dumps({"retention_days": 1}))
    assert status == 400 and "RETENTION_DAYS" in json.loads(body)["error"]
    assert request(addr, "POST", "/api/settings", "not json")[0] == 400
    assert request(addr, "POST", "/api/nope", "{}")[0] == 404
    status, body = request(addr, "GET", "/api/settings/preview?retention_days=1")
    assert status == 200 and json.loads(body)["requests_deleted"] == 0
    assert request(addr, "GET", "/settings")[0] == 200


class Stream:
    """Reads an SSE response event by event."""

    def __init__(self, addr, path, headers=None):
        self.conn = http.client.HTTPConnection(*addr, timeout=10)
        self.conn.request("GET", path, headers=headers or {})
        self.resp = self.conn.getresponse()

    def next(self):
        # http.client hands the socket to the response for a Connection: close reply; the 10 s timeout set at
        # connect time still applies to every read
        event = {}
        while True:
            line = self.resp.fp.readline().decode("utf-8")
            if not line:
                return None
            line = line.rstrip("\n")
            if not line:
                if "event" in event:
                    return event
                event = {}
                continue
            if line.startswith(":"):
                event.setdefault("comments", 0)
                continue
            key, _, value = line.partition(": ")
            event[key] = value

    def close(self):
        self.resp.close()                                  # the response holds the socket, not the connection
        self.conn.close()


def test_stream_pushes_matching_rows_and_resumes(live):
    addr, log, _ = live
    now = int(time.time() * 1000)
    s = Stream(addr, f"/api/stream?kind=logs&from={now - 3_600_000}&status=5xx")
    assert s.resp.status == 200 and s.resp.getheader("Content-Type").startswith("text/event-stream")
    assert s.next()["event"] == "hello"
    append(log, traefik_line(now), traefik_line(now + 1, DownstreamStatus=502, RequestPath="/broken"))
    ev = s.next()
    page = json.loads(ev["data"])
    assert ev["event"] == "rows" and [r[5] for r in page["rows"]] == ["/broken"]   # the 200 was filtered out
    last_id = ev["id"]
    s.close()
    # reconnect with Last-Event-ID: rows written meanwhile arrive, nothing already seen comes again
    append(log, traefik_line(now + 2, DownstreamStatus=500, RequestPath="/also-broken"))
    time.sleep(1.5)
    s = Stream(addr, f"/api/stream?kind=logs&from={now - 3_600_000}&status=5xx", {"Last-Event-ID": last_id})
    assert s.next()["event"] == "hello"
    ev = s.next()
    assert [r[5] for r in json.loads(ev["data"])["rows"]] == ["/also-broken"]
    s.close()


def test_stream_changes_are_throttled(live):
    addr, log, _ = live
    s = Stream(addr, "/api/stream?kind=changes&min_interval=1")
    assert s.next()["event"] == "hello"
    t0 = time.monotonic()
    for i in range(5):
        append(log, traefik_line())
        time.sleep(0.3)
    first = s.next()
    second = s.next()
    assert first["event"] == second["event"] == "change"
    assert time.monotonic() - t0 >= 1.0               # at most one per second however often rows arrive
    s.close()


def test_stream_cap_falls_back(live, monkeypatch):
    addr, _, app = live
    monkeypatch.setattr(server_mod, "MAX_STREAMS", 1)
    s = Stream(addr, "/api/stream?kind=changes")
    assert s.next()["event"] == "hello"
    assert request(addr, "GET", "/api/stream?kind=changes")[0] == 503
    s.close()
    assert wait_for(lambda: app.streams == 0, 10)  # noticed within STREAM_CHECK
    assert request(addr, "GET", "/api/stream?kind=nope")[0] == 400


def test_one_stream_can_carry_rows_and_changes(live):
    addr, log, _ = live
    now = int(time.time() * 1000)
    s = Stream(addr, f"/api/stream?kind=logs&changes=1&min_interval=0.5&from={now - 3_600_000}")
    assert s.next()["event"] == "hello"
    append(log, traefik_line(now))
    seen = set()
    end = time.monotonic() + 8
    while seen != {"rows", "change"} and time.monotonic() < end:
        seen.add(s.next()["event"])
    assert seen == {"rows", "change"}
    s.close()
