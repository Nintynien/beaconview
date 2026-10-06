import json

from helpers import traefik_line

from beaconview.parsers import COLUMNS, TraefikParser, anonymize

MS = 1_791_201_600_123   # 2026-10-05T12:00:00.123Z


def row(line, **kw):
    out = TraefikParser(**kw).parse(line)
    return None if out is None else dict(zip(COLUMNS, out))


def test_every_column():
    r = row(traefik_line(MS, request_X_Custom=None, **{"request_X-Forwarded-For": "198.51.100.1, 10.0.0.1"}))
    assert r == {
        "ts_ms": MS, "host": "example.com", "method": "GET", "path": "/api/state", "query": "x=1",
        "status": 200, "origin_status": 200, "duration_us": 1234, "bytes": 512, "ip": "203.0.113.7",
        "router": "app@docker", "service": "app@docker", "entrypoint": "websecure", "protocol": "HTTP/2.0",
        "tls": "1.3", "user_agent": "Mozilla/5.0 (X11; Linux x86_64) Firefox/131.0",
        "referer": "https://example.com/", "headers": json.dumps({"X-Forwarded-For": "198.51.100.1, 10.0.0.1"},
                                                                 separators=(",", ":")),
    }


def test_missing_fields_are_none():
    r = row(traefik_line(MS, RouterName=None, ServiceName=None, TLSVersion=None, Duration=None,
                         **{"request_User-Agent": None, "request_Referer": None, "RequestPath": "/plain"}))
    assert r["router"] is r["service"] is r["tls"] is r["duration_us"] is None
    assert r["user_agent"] is r["referer"] is r["headers"] is r["query"] is None
    assert r["path"] == "/plain"


def test_traefik_answered_itself():
    # a 404 from Traefik (no router matched): no router, no service, OriginStatus 0
    r = row(traefik_line(MS, RouterName="", ServiceName="", OriginStatus=0, DownstreamStatus=404))
    assert r["router"] is None and r["service"] is None and r["origin_status"] == 0 and r["status"] == 404


def test_bad_lines():
    p = TraefikParser()
    for line in ("", "not json", "[1,2]", '"text"', json.dumps({"msg": "no time"}),
                 json.dumps({"StartUTC": "yesterday"})):
        assert p.parse(line) is None, line


def test_time_formats():
    p = TraefikParser()
    assert p.parse(traefik_line(MS))[0] == MS
    # no fraction, and fallback to "time" when StartUTC is absent
    assert p.parse(json.dumps({"StartUTC": "2026-10-05T12:00:00Z"}))[0] == MS - 123
    assert p.parse(json.dumps({"time": "2026-10-05T12:00:00Z"}))[0] == MS - 123
    # a local time with an offset takes the slow path and still lands on UTC
    assert p.parse(json.dumps({"StartUTC": "2026-10-05T14:00:00.123456789+02:00"}))[0] == MS


def test_client_ip_header_case_and_chain():
    line = traefik_line(MS, **{"request_Cf-Connecting-Ip": "198.51.100.9", "request_X-Forwarded-For": "192.0.2.1, 10.0.0.1"})
    assert row(line, client_ip_header="CF-Connecting-IP")["ip"] == "198.51.100.9"
    assert row(line, client_ip_header="x-forwarded-for")["ip"] == "192.0.2.1"
    # configured but absent in this line: fall back to the TCP peer
    assert row(traefik_line(MS), client_ip_header="CF-Connecting-IP")["ip"] == "203.0.113.7"


def test_anonymize():
    assert anonymize("203.0.113.77") == "203.0.113.0"
    assert anonymize("2001:db8:abcd:12::1") == "2001:db8:abcd::"
    assert anonymize("not-an-ip") is None
    assert row(traefik_line(MS), anonymize_ips=True)["ip"] == "203.0.113.0"
