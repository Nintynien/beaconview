import gzip
import os
import time
from types import SimpleNamespace

import duckdb
from helpers import append, traefik_line, wait_for

from beaconview.geoip import DBIP_FILE, UNKNOWN, GeoIP
from beaconview.ingest import Ingestor
from beaconview.parsers import TraefikParser
from beaconview.query import Runner, parse
from beaconview.store import Store
from conftest import make_cfg

COUNTRIES = {"203.0.113.7": "US", "198.51.100.1": "DE", "203.0.113.0": "US"}


class FakeReader:
    """Stands in for a maxminddb reader: DB-IP's record shape for a few documentation addresses."""

    def __init__(self, path=None):
        self.path = path
        self.closed = False

    def get(self, ip):
        if ip == "not-an-ip":
            raise ValueError("not an IP")
        code = COUNTRIES.get(ip)
        return {"country": {"iso_code": code, "names": {"en": code}}} if code else None

    def metadata(self):
        return SimpleNamespace(build_epoch=1_790_000_000)

    def close(self):
        self.closed = True


def geo_with(tmp_path, **kw):
    g = GeoIP("auto", str(tmp_path), opener=FakeReader, **kw)
    return g


def test_lookup(tmp_path):
    g = geo_with(tmp_path)
    assert g.lookup("203.0.113.7") is None                    # no database yet: nothing to say
    assert g._open("whatever.mmdb", "db-ip")
    assert g.lookup("203.0.113.7") == "US" and g.lookup("198.51.100.1") == "DE"
    assert g.lookup("10.0.0.1") == UNKNOWN and g.lookup("not-an-ip") == UNKNOWN
    assert g.lookup("") is None
    assert g.attribution == {"text": "IP Geolocation by DB-IP", "url": "https://db-ip.com"}
    g._open("mine.mmdb", "file")
    assert g.attribution is None                              # your own file: no DB-IP credit owed


def test_monthly_download_falls_back_and_skips_when_fresh(tmp_path):
    asked = []

    def fetch(url):
        asked.append(url)
        if "2026-10" in url:
            raise OSError("404: not published yet")
        return gzip.compress(b"an mmdb")

    clock = [time.mktime((2026, 10, 2, 12, 0, 0, 0, 0, -1))]
    g = geo_with(tmp_path, fetch=fetch, clock=lambda: clock[0])
    assert g.refresh() is True
    assert [u.rsplit("-lite-", 1)[1] for u in asked] == ["2026-10.mmdb.gz", "2026-09.mmdb.gz"]   # this month, then last
    path = os.path.join(str(tmp_path), "geoip", DBIP_FILE)
    assert open(path, "rb").read() == b"an mmdb" and g.source == "db-ip"
    os.utime(path, (clock[0], clock[0]))
    asked.clear()
    assert g.refresh() is True and asked == []                 # fresh enough: no download
    clock[0] += 40 * 86400                                     # older than a month: fetch again
    g.refresh()
    assert asked


def test_failed_download_keeps_what_works(tmp_path):
    def fetch(url):
        raise OSError("offline")

    g = geo_with(tmp_path, fetch=fetch)
    assert g.refresh() is False and "offline" in g.error and g.reader is None
    g._open("old.mmdb", "db-ip")
    os.makedirs(os.path.join(str(tmp_path), "geoip"), exist_ok=True)
    assert g.refresh() is True and g.lookup("203.0.113.7") == "US"   # still answering from the previous file


def test_off(tmp_path):
    g = GeoIP("off", str(tmp_path), opener=FakeReader)
    g.start()
    assert not g.enabled and g.reader is None and g.lookup("203.0.113.7") is None


def test_ingest_tags_rows_and_fills_older_ones(tmp_path):
    cfg = make_cfg(tmp_path)
    store = Store(cfg.db_path, "256MB", 2)
    log = tmp_path / "access.log"
    # rows stored while there was no database
    append(log, traefik_line(), traefik_line(ClientHost="198.51.100.1"))
    geo = geo_with(tmp_path)
    ing = Ingestor(cfg, store, geo=geo)
    ing.start()
    assert wait_for(lambda: store.reader().execute("SELECT count(*) FROM requests").fetchone()[0] == 2)
    assert store.reader().execute("SELECT count(country) FROM requests").fetchone()[0] == 0
    # the database arrives: new rows get a country, and the older ones are filled in
    geo._open("db.mmdb", "db-ip")
    ing.fill_at = 0.0
    append(log, traefik_line(ClientHost="10.0.0.9"))
    assert wait_for(lambda: store.reader().execute(
        "SELECT count(*) FROM requests WHERE country IS NOT NULL").fetchone()[0] == 3)
    got = dict(store.reader().execute("SELECT ip, country FROM requests").fetchall())
    assert got == {"203.0.113.7": "US", "198.51.100.1": "DE", "10.0.0.9": UNKNOWN}
    q = parse({"from": [str(int(time.time() * 1000) - 3_600_000)], "to": [str(int(time.time() * 1000) + 60_000)],
               "country": ["DE"]})
    assert Runner(store).summary(q)["requests"] == 1
    ing.stop(); ing.join(10); store.close()


def test_older_databases_gain_the_column(tmp_path):
    path = str(tmp_path / "old.duckdb")
    con = duckdb.connect(path)
    con.execute("""CREATE TABLE requests (seq BIGINT NOT NULL, instance VARCHAR NOT NULL, ts TIMESTAMP NOT NULL,
        host VARCHAR, method VARCHAR, path VARCHAR, query VARCHAR, status USMALLINT, origin_status USMALLINT,
        duration_us BIGINT, bytes BIGINT, ip VARCHAR, router VARCHAR, service VARCHAR, entrypoint VARCHAR,
        protocol VARCHAR, tls VARCHAR, user_agent VARCHAR, referer VARCHAR, headers JSON)""")
    con.execute("INSERT INTO requests (seq, instance, ts, ip) VALUES (1, 'edge', now(), '203.0.113.7')")
    con.close()
    store = Store(path)
    cols = [r[0] for r in store.reader().execute("DESCRIBE requests").fetchall()]
    assert "country" in cols
    assert store.missing_country_ips() == ["203.0.113.7"]
    store.write_batch([(2, "edge") + TraefikParser().parse(traefik_line()) + ("US",)], [])
    assert store.reader().execute("SELECT country FROM requests WHERE seq = 2").fetchone()[0] == "US"
    store.close()
