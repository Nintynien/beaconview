import json
from urllib.parse import parse_qs

import pytest
from helpers import traefik_line

from beaconview import query
from beaconview.parsers import TraefikParser

T0 = 1_791_201_600_000   # 2026-10-05T12:00:00Z
P = TraefikParser()


def q(qs="", **over):
    params = parse_qs(qs)
    params.setdefault("from", [str(T0)])
    params.setdefault("to", [str(T0 + 3_600_000)])
    return query.parse(params)


@pytest.fixture
def runner(store):
    """An hour of hand-made traffic:
    minute 0: 3 x 200 GET /api/state from 203.0.113.7 (ua Firefox), host a.com
    minute 1: 1 x 304 /api/state from 203.0.113.8, 1 x 404 /missing from 198.51.100.1 (ua curl), host b.com
    minute 5: 1 x 500 POST /api/feed from 2001:db8::1, 3 s, header X-Tenant=acme, router none
    """
    specs = [
        (T0 + 1_000, {}), (T0 + 2_000, {}), (T0 + 3_000, {}),
        (T0 + 61_000, {"DownstreamStatus": 304, "ClientHost": "203.0.113.8", "RequestHost": "b.com"}),
        (T0 + 62_000, {"DownstreamStatus": 404, "RequestPath": "/missing", "ClientHost": "198.51.100.1",
                       "RequestHost": "b.com", "request_User-Agent": "curl/8.9.1", "request_Referer": None}),
        (T0 + 300_000, {"DownstreamStatus": 500, "RequestMethod": "POST", "RequestPath": "/api/feed?x=1",
                        "ClientHost": "2001:db8::1", "Duration": 3_000_000_000, "RouterName": "",
                        "request_X-Tenant": "acme"}),
    ]
    rows = []
    for i, (ms, over) in enumerate(specs):
        defaults = {"RequestHost": "a.com"}
        defaults.update(over)
        rows.append((i + 1, "edge") + P.parse(traefik_line(ms, **defaults)))
    store.write_batch(rows, [])
    store.rollup()
    return query.Runner(store)


def count(runner, qs):
    return runner.summary(q(qs))["requests"]


def test_filters(runner):
    assert count(runner, "") == 6
    assert count(runner, "status=200") == 3
    assert count(runner, "status=4xx&status=5xx") == 2
    assert count(runner, "status=!200") == 3
    assert count(runner, "path=/api/*") == 5
    assert count(runner, "path=*state*") == 4
    assert count(runner, "path=*feed") == 1
    assert count(runner, "path=/api/state&path=/missing") == 5
    assert count(runner, "path=!/api/*") == 1
    assert count(runner, "ip=203.0.113.0/24") == 4
    assert count(runner, "ip=2001:db8:*") == 1
    assert count(runner, "ip=!203.0.113.7") == 3
    assert count(runner, "user_agent=CURL") == 1                        # case-insensitive contains
    assert count(runner, "referer=!example") == 1                       # missing referer counts as "not example"
    assert count(runner, "router=(none)") == 1
    assert count(runner, "router=!(none)") == 5
    assert count(runner, "method=POST") == 1
    assert count(runner, "min_ms=1000") == 1
    assert count(runner, "max_ms=2") == 5
    assert count(runner, "h.X-Tenant=acme") == 1
    assert count(runner, "h.X-Tenant=!acme") == 5
    assert count(runner, "query=x=1") == 5                               # the default line is /api/state?x=1
    assert count(runner, "query=y") == 0
    assert count(runner, "host=b.com&status=!304") == 1


def test_rollup_and_raw_agree(runner):
    for qs in ("", "host=b.com", "status=4xx", "router=(none)", "router=!(none)", "status=!200&host=a.com"):
        assert q(qs).rollup_ok(), qs
        rolled = runner.summary(q(qs))
        assert rolled["source"] == "rollup"
        raw = runner.summary(q(qs + "&min_ms=0"))                       # a raw-only filter forces the raw path
        assert raw["source"] == "raw"
        assert rolled["requests"] == raw["requests"], qs
        assert rolled["status_5xx"] == raw["status_5xx"], qs
        assert rolled["bytes"] == raw["bytes"], qs


def test_summary(runner):
    s = runner.summary(q())
    assert s["requests"] == 6 and s["status_4xx"] == 1 and s["status_5xx"] == 1
    assert s["unique_ips"] == 4 and s["unique_ips_approx"] is False
    assert s["rps"] == round(6 / 3600, 3)
    assert 1_000 <= s["p50_us"] < 2_500                                  # 1.2 ms requests, histogram bucket
    long = runner.summary(query.parse({"from": [str(T0 - 2 * 86_400_000)], "to": [str(T0 + 3_600_000)]}))
    assert long["unique_ips_approx"] is True and long["requests"] == 6


def test_series_rollup_and_split(runner):
    s = runner.series(q(), buckets=60)                                   # 1-minute buckets over the hour
    assert s["step"] == 60_000 and s["buckets"] == 60 and s["source"] == "rollup"
    assert s["series"][0]["values"][:6] == [3, 2, 0, 0, 0, 1]
    by_class = runner.series(q(), buckets=60, split="status_class")
    got = {x["key"]: sum(x["values"]) for x in by_class["series"]}
    assert got == {"2xx": 3, "3xx": 1, "4xx": 1, "5xx": 1}
    assert by_class["latency"]["p95_us"][5] >= 2_500_000               # the 3 s request's minute


def test_series_raw_split_top_and_other(runner):
    s = runner.series(q(), buckets=60, split="ip")
    assert s["source"] == "raw"
    keys = [x["key"] for x in s["series"]]
    assert keys[0] == "203.0.113.7" and len(keys) == 4                   # 4 IPs: all fit in the top 5
    assert sum(sum(x["values"]) for x in s["series"]) == 6
    many = runner.series(q(), buckets=60, split="status")
    assert sum(sum(x["values"]) for x in many["series"]) == 6


def test_series_short_range_uses_raw_seconds(runner):
    s = runner.series(q("from=%d&to=%d" % (T0, T0 + 120_000)), buckets=120)
    assert s["step"] == 1000 and s["source"] == "raw"
    assert sum(s["series"][0]["values"]) == 5


def test_top(runner):
    t = runner.top(q(), "path")
    assert t["values"][0] == {"value": "/api/state", "count": 4, "share": round(4 / 6, 4)}
    assert t["total"] == 6 and t["source"] == "raw"
    hosts = runner.top(q(), "host")
    assert hosts["source"] == "rollup" and hosts["values"][0]["value"] == "a.com"
    assert runner.top(q("status=5xx"), "router")["values"] == [{"value": "(none)", "count": 1, "share": 1.0}]
    with pytest.raises(query.BadQuery):
        runner.top(q(), "nope")


def test_logs_paging_and_live(runner):
    page = runner.logs(q(), limit=4)
    assert [r[0] for r in page["rows"]] == [6, 5, 4, 3]
    ts_i = page["columns"].index("ts")
    assert page["rows"][0][ts_i] == T0 + 300_000
    nxt = runner.logs(q(), limit=4, before=tuple(page["next"]))
    assert [r[0] for r in nxt["rows"]] == [2, 1] and nxt["next"] is None
    live = runner.logs(q(), after_seq=4)
    assert [r[0] for r in live["rows"]] == [6, 5] and live["max_seq"] == 6
    filtered = runner.logs(q("status=4xx"))
    assert [r[0] for r in filtered["rows"]] == [5]
    hdr = page["columns"].index("headers")
    assert json.loads(page["rows"][0][hdr]) == {"X-Tenant": "acme"}


def test_logs_window_widens_to_old_rows(runner):
    # a 30-day range whose only rows are at its far end: the search must reach them
    long = query.parse({"from": [str(T0)], "to": [str(T0 + 30 * 86_400_000)]})
    assert len(runner.logs(long, limit=10)["rows"]) == 6


def test_fields(runner):
    f = runner.fields()
    assert f["values"]["host"] == ["a.com", "b.com"] and "status_class" in f["splits"]


def test_bad_queries():
    for qs in ("status=teapot", "ip=2001:db8::/32", "ip=1.2.3.4/99", "from=x"):
        with pytest.raises(query.BadQuery):
            q(qs)
    with pytest.raises(query.BadQuery):
        query.parse({"from": ["5"], "to": ["5"]})


def test_injection_stays_data(runner):
    # quotes and SQL in values, in a header name, and in a glob are just text to match
    assert count(runner, "path=%27%3B DROP TABLE requests%3B --") == 0
    assert count(runner, "h.X%22%29 OR 1%3D1 --=acme") == 0
    assert count(runner, "user_agent=%27 OR %271%27=%271") == 0
    assert count(runner, "") == 6


def test_breakdown(runner):
    b = runner.breakdown(q(), "host")
    assert b["source"] == "rollup"
    assert [(r["value"], r["requests"], r["status_4xx"], r["status_5xx"]) for r in b["rows"]] == [
        ("a.com", 4, 0, 1), ("b.com", 2, 1, 0)]
    assert b["rows"][0]["share"] == round(4 / 6, 4)
    # the slow request's host ranks first by p95, and the raw path (path field) agrees on counts
    assert runner.breakdown(q(), "host", sort="p95")["rows"][0]["value"] == "a.com"
    raw = runner.breakdown(q(), "path", sort="errors")
    assert raw["source"] == "raw" and raw["rows"][0]["value"] in ("/api/feed", "/missing")
    assert raw["rows"][0]["status_5xx"] == 1
    assert runner.breakdown(q(), "router")["rows"][-1]["value"] == "(none)"
    with pytest.raises(query.BadQuery):
        runner.breakdown(q(), "host", sort="vibes")
