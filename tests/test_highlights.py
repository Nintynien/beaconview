from helpers import traefik_line

from beaconview.parsers import TraefikParser
from beaconview.query import Query, Runner

DAY = 86_400_000
T = 1_791_201_600_000          # the end of the window being judged
P = TraefikParser()


def build(store, current):
    """Seven quiet days (same clock hour each day), then `current` rows in the last hour.
    Quiet traffic: 120 requests per hour from 4 regular clients, on two routers, 5 ms, all 200."""
    rows, seq = [], 0

    def add(ms, **over):
        nonlocal seq
        seq += 1
        rows.append((seq, "edge") + P.parse(traefik_line(ms, **over)) + (None,))

    for k in range(7, 0, -1):
        start = T - k * DAY - 3_600_000
        for i in range(120):
            add(start + i * 30_000, ClientHost=f"192.0.2.{i % 4 + 1}", RouterName=("app@docker", "api@docker")[i % 2],
                RequestPath=f"/page/{i % 6}", Duration=5_000_000)
    for i, over in enumerate(current):
        add(T - 3_600_000 + i * 3_600_000 // max(1, len(current)), **over)
    store.write_batch(rows, [])
    store.rollup()


def judge(store, oldest=None):
    q = Query(T - 3_600_000, T, [])
    return Runner(store).highlights(q, oldest_ms=oldest if oldest is not None else T - 8 * DAY)


def quiet_hour():
    return [dict(ClientHost=f"192.0.2.{i % 4 + 1}", RouterName=("app@docker", "api@docker")[i % 2],
                 RequestPath=f"/page/{i % 6}", Duration=5_000_000) for i in range(120)]


def test_a_quiet_hour_has_nothing_to_say(store):
    build(store, quiet_hour())
    h = judge(store)
    assert h["state"] == "ready" and h["days"] == 7 and h["items"] == []


def test_each_kind_of_unusual(store):
    current = quiet_hour()
    current += [dict(ClientHost="45.83.66.10", RequestPath="/api/state", RouterName="app@docker", Duration=5_000_000)] * 300
    current += [dict(RouterName="api@docker", DownstreamStatus=502, ClientHost="192.0.2.1", Duration=5_000_000)] * 40
    current += [dict(RouterName="app@docker", ClientHost="192.0.2.2", Duration=900_000_000)] * 60
    build(store, current)
    items = judge(store)["items"]
    kinds = {h["kind"]: h for h in items}
    assert set(kinds) == {"errors", "latency", "client", "path", "traffic"}, items
    assert kinds["errors"]["filters"] == [["router", "api@docker"], ["status", "5xx"]]
    assert kinds["errors"]["severity"] == "critical"                    # 40 of ~100 requests failing
    assert kinds["client"]["title"] == "New client 45.83.66.10" and kinds["client"]["filters"] == [["ip", "45.83.66.10"]]
    assert kinds["path"]["title"] == "New path /api/state"
    assert kinds["latency"]["title"].startswith("app@docker: p95")
    assert kinds["traffic"]["title"].startswith("Traffic 4.3× its usual level")
    severities = [h["severity"] for h in items]
    assert severities == sorted(severities, key=["critical", "warning", "info"].index)


def test_a_drop_in_traffic(store):
    build(store, quiet_hour()[:20])
    items = judge(store)["items"]
    assert [h["title"] for h in items] == ["Traffic 83% below usual"] and items[0]["severity"] == "critical"


def test_a_regular_client_surging(store):
    current = quiet_hour() + [dict(ClientHost="192.0.2.3", RequestPath="/page/1", RouterName="app@docker",
                                   Duration=5_000_000)] * 400
    build(store, current)
    titles = [h["title"] for h in judge(store)["items"]]
    assert "Client 192.0.2.3: 14× its usual requests" in titles


def test_learning_until_there_is_a_day_to_compare(store):
    build(store, quiet_hour())
    h = judge(store, oldest=T - 5 * 3_600_000)              # only five hours of history
    assert h["state"] == "learning" and h["items"] == []
    assert h["ready_ms"] == T - 5 * 3_600_000 + DAY + 3_600_000
