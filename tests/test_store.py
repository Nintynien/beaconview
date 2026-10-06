from helpers import traefik_line

from beaconview.parsers import TraefikParser
from beaconview.store import PRUNE_CHUNK_MS

T0 = 1_791_201_600_000   # 2026-10-05T12:00:00Z
P = TraefikParser()


def rows(start_seq, *specs):
    """(ms, overrides) -> batch rows."""
    out = []
    for i, (ms, over) in enumerate(specs):
        out.append((start_seq + i, "edge") + P.parse(traefik_line(ms, **over)))
    return out


def test_write_batch_round_trip(store):
    line = traefik_line(T0 + 5, **{"request_X-Weird": 'quote " comma , newline \n tab \t', "RequestPath": "/a,b?q=\"x\""})
    batch = [(1, "edge") + P.parse(line)]
    store.write_batch(batch, [("edge", "/logs/access.log", 123, 456)])
    r = store.reader()
    got = r.execute("SELECT seq, instance, epoch_ms(ts), path, query, headers->>'X-Weird', router, tls FROM requests").fetchall()
    assert got == [(1, "edge", T0 + 5, "/a,b", 'q="x"', 'quote " comma , newline \n tab \t', "app@docker", "1.3")]
    assert store.load_positions() == {"edge": ("/logs/access.log", 123, 456)}
    assert store.max_seq() == 1


def test_nulls_and_empty_strings_survive(store):
    line = traefik_line(T0, RouterName=None, **{"request_User-Agent": ""})
    store.write_batch([(1, "edge") + P.parse(line)], [])
    router, ua = store.reader().execute("SELECT router, user_agent FROM requests").fetchone()
    assert router is None and ua == ""


def test_positions_without_rows(store):
    store.write_batch([], [("edge", "/x", 1, 10)])
    store.write_batch([], [("edge", "/x", 1, 20)])
    assert store.load_positions() == {"edge": ("/x", 1, 20)}


def test_failed_batch_leaves_nothing(store, monkeypatch):
    store.write_batch(rows(1, (T0, {})), [("edge", "/x", 1, 10)])
    bad = rows(2, (T0, {}))
    bad[0] = bad[0][:7] + ("not-a-status",) + bad[0][8:]   # status column cannot be cast: the load fails
    try:
        store.write_batch(bad, [("edge", "/x", 1, 99)])
    except Exception:
        pass
    else:
        raise AssertionError("expected the load to fail")
    assert store.reader().execute("SELECT count(*) FROM requests").fetchone()[0] == 1
    assert store.load_positions() == {"edge": ("/x", 1, 10)}


def test_rollup_counts_and_late_rows(store):
    store.write_batch(rows(1,
                           (T0 + 1_000, {}),
                           (T0 + 2_000, {"DownstreamStatus": 304}),
                           (T0 + 61_000, {"Duration": 9_000_000})), [])
    assert store.rollup() == 2
    r = store.reader()
    q = "SELECT epoch_ms(minute), status, requests, duration_max_us FROM minute_stats ORDER BY 1, 2"
    assert r.execute(q).fetchall() == [(T0, 200, 1, 1234), (T0, 304, 1, 1234), (T0 + 60_000, 200, 1, 9000)]
    assert store.rollup() == 0                                  # nothing dirty
    # a late row (a websocket that started in the first minute, logged long after) lands in its own minute
    store.write_batch(rows(4, (T0 + 30_000, {})), [])
    assert store.rollup() == 1
    assert r.execute(q).fetchall()[0] == (T0, 200, 2, 1234)
    assert r.execute("SELECT sum(requests) FROM minute_stats").fetchone()[0] == 4


def test_prune_in_chunks_then_rollups(store):
    hour = 3_600_000
    store.write_batch(rows(1, *[(T0 + i * 10 * 60_000, {}) for i in range(12)]), [])   # two hours, 10-min apart
    store.rollup()
    cutoff = T0 + hour
    steps = 0
    while store.prune_step(cutoff, T0 + 30 * 60_000):
        steps += 1
    assert steps == hour // PRUNE_CHUNK_MS
    r = store.reader()
    assert r.execute("SELECT epoch_ms(min(ts)), count(*) FROM requests").fetchone() == (cutoff, 6)
    # rollups keep their own, longer retention
    assert r.execute("SELECT epoch_ms(min(minute)) FROM minute_stats").fetchone()[0] == T0 + 30 * 60_000
    assert store.prune_step(cutoff, T0) is False                # nothing left to do


def test_rollup_skips_minutes_past_raw_retention(store):
    store.write_batch(rows(1, (T0, {})), [])
    store.rollup()
    store.write_batch(rows(2, (T0 + 1, {})), [])                # dirties T0 again
    # pretend the raw rows of that minute are already expired: the existing rollup must survive
    assert store.rollup(raw_cutoff_ms=T0 + 60_000) == 0
    assert store.reader().execute("SELECT requests FROM minute_stats").fetchone()[0] == 1


def test_storage_stats(store):
    store.write_batch(rows(1, (T0, {}), (T0 + 5, {})), [])
    s = store.storage_stats()
    assert s["rows"] == 2 and s["oldest_ms"] == T0 and s["newest_ms"] == T0 + 5 and s["db_bytes"] > 0


def test_rollup_histogram_and_quantiles(store):
    from beaconview.store import HIST_COLUMNS, hist_quantiles
    # 90 fast requests (~1.2 ms) and 10 slow ones (~2 s)
    specs = [(T0 + i, {"Duration": 1_200_000}) for i in range(90)] + \
            [(T0 + 100 + i, {"Duration": 2_000_000_000}) for i in range(10)]
    store.write_batch(rows(1, *specs), [])
    store.rollup()
    buckets = store.reader().execute(
        "SELECT " + ", ".join(f"sum({c})::BIGINT" for c in HIST_COLUMNS) + " FROM minute_stats").fetchone()
    assert sum(buckets) == 100
    p50, p95 = hist_quantiles(list(buckets), [0.5, 0.95])
    assert 1_000 <= p50 < 2_500            # inside the 1-2.5 ms bucket
    assert 1_000_000 <= p95 < 2_500_000    # inside the 1-2.5 s bucket
    assert hist_quantiles([0] * len(HIST_COLUMNS), [0.5]) == [None]
