"""Bulk-generate days of requests straight into a BeaconView database, then time the queries the pages will run.

    python tools/bench.py bench.duckdb --days 7 --rate 300
    python tools/bench.py bench.duckdb --queries-only          # rerun the timings on an existing file

Uses the real schema (beaconview.store) and the default limits (DB_MEMORY_LIMIT 512MB, DB_THREADS 2) unless told
otherwise, so the numbers describe a small host. The generated traffic has the same shape as tools/genlog.py.
"""

import argparse
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from beaconview.store import _DDL, HIST_COLUMNS, ROLLUP_SELECT, Store, hist_quantiles  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from genlog import HOSTS, ROUTERS, UAS, Generator  # noqa: E402

CHUNK_ROWS = 20_000_000

GEN_SQL = """
INSERT INTO requests
SELECT seq, 'edge', epoch_ms(?::BIGINT + (seq * ?::DOUBLE)::BIGINT),
       hosts[h + 1],
       CASE WHEN r2 < 0.93 THEN 'GET' ELSE 'POST' END,
       paths[floor(pow(r3, 3) * len(paths))::INT + 1],
       CASE WHEN r4 < 0.1 THEN 'page=' || (1 + floor(r4 * 200)::INT) END,
       st, CASE WHEN st < 499 THEN st ELSE 0 END,
       (exp(15.5 + 1.2 * sqrt(-2 * ln(r5 + 1e-12)) * cos(2 * pi() * r6)) / 1000)::BIGINT,
       CASE WHEN st = 304 THEN 0 ELSE 200 + floor(r7 * 60000)::INT END,
       ips[floor(pow(r8, 4) * len(ips))::INT + 1],
       routers[h + 1], routers[h + 1], 'websecure', 'HTTP/2.0', '1.3',
       uas[floor(pow(r9, 2) * len(uas))::INT + 1],
       CASE WHEN r10 < 0.3 THEN 'https://' || hosts[h + 1] || '/' END,
       NULL
FROM (
    SELECT seq, floor(pow(random(), 2) * 5)::INT AS h, random() r2, random() r3, random() r4, random() r5,
           random() r6, random() r7, random() r8, random() r9, random() r10,
           CASE WHEN x < 0.78 THEN 200 WHEN x < 0.90 THEN 304 WHEN x < 0.95 THEN 404 WHEN x < 0.97 THEN 301
                WHEN x < 0.98 THEN 403 WHEN x < 0.99 THEN 500 WHEN x < 0.995 THEN 502 ELSE 499 END AS st,
           ?::VARCHAR[] AS hosts, ?::VARCHAR[] AS routers, ?::VARCHAR[] AS paths, ?::VARCHAR[] AS ips,
           ?::VARCHAR[] AS uas
    FROM (SELECT range AS seq, random() AS x FROM range(?, ?))
)
"""

ROLLUP_SQL = f"INSERT INTO minute_stats {ROLLUP_SELECT} GROUP BY ALL"
HIST_SUMS = ", ".join(f"sum({c})::BIGINT" for c in HIST_COLUMNS)


def rebuild_rollups(store):
    t = time.time()
    store.w.execute("DROP TABLE IF EXISTS minute_stats")
    store.w.execute(_DDL[1])
    store.w.execute(ROLLUP_SQL)
    store.w.execute("CHECKPOINT")
    print(f"rebuilt rollups in {time.time() - t:.0f} s", flush=True)


def generate(store, days, rate):
    gen = Generator(1)
    total = int(days * 86400 * rate)
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(days * 86_400_000)
    step = 1000.0 / rate
    lists = [HOSTS, ROUTERS, gen.paths, gen.ips, UAS]
    t0 = time.time()
    for lo in range(0, total, CHUNK_ROWS):
        hi = min(total, lo + CHUNK_ROWS)
        store.w.execute(GEN_SQL, [start_ms, step] + lists + [lo, hi])
        print(f"  {hi:,}/{total:,} rows, {time.time() - t0:.0f} s", flush=True)
    store.w.execute(ROLLUP_SQL)
    store.w.execute("CHECKPOINT")
    print(f"generated {total:,} rows in {time.time() - t0:.0f} s", flush=True)


def timed(cur, sql, params=(), runs=3):
    times = []
    result = None
    for _ in range(runs):
        t = time.perf_counter()
        result = cur.execute(sql, list(params)).fetchall()
        times.append(time.perf_counter() - t)
    return times[0], statistics.median(times), result


def queries(store):
    cur = store.reader()
    rows, newest = cur.execute("SELECT count(*), epoch_ms(max(ts)) FROM requests").fetchone()
    size = os.path.getsize(store.db_path)
    print(f"\n{rows:,} rows, {size / 1e9:.2f} GB on disk, {size / max(rows, 1):.1f} bytes/row\n")
    day = 86_400_000
    span = [
        ("24h", newest - day), ("7d", newest - 7 * day), ("30d", newest - 30 * day),
    ]
    checks = []
    for label, since in span:
        p = [since]
        checks += [
            (f"overview tiles ({label}, rollups)",
             "SELECT sum(requests), sum(bytes), sum(requests) FILTER (status >= 500) FROM minute_stats "
             "WHERE minute >= epoch_ms(?::BIGINT)", p),
            (f"overview series by status class ({label}, rollups, ~300 buckets)",
             f"SELECT time_bucket(INTERVAL '{max(1, (newest - since) // 300 // 60000)} minutes', minute) b, "
             "status // 100, sum(requests) FROM minute_stats WHERE minute >= epoch_ms(?::BIGINT) GROUP BY ALL", p),
            (f"unique IPs, approx ({label})",
             "SELECT approx_count_distinct(ip) FROM requests WHERE ts >= epoch_ms(?::BIGINT)", p),
            (f"p50/p95 latency, raw approx_quantile ({label})",
             "SELECT approx_quantile(duration_us, [0.5, 0.95]) FROM requests WHERE ts >= epoch_ms(?::BIGINT)", p),
            (f"p50/p95 latency, rollup histogram ({label})",
             f"SELECT {HIST_SUMS}, max(duration_max_us) FROM minute_stats WHERE minute >= epoch_ms(?::BIGINT)", p),
            (f"p50/p95 on /api/state*, raw 5% sample ({label})",
             "SELECT approx_quantile(duration_us, [0.5, 0.95]) FROM requests TABLESAMPLE 5% (system) "
             "WHERE ts >= epoch_ms(?::BIGINT) AND starts_with(path, '/api/state')", p),
            (f"p50/p95 on /api/state*, raw full ({label})",
             "SELECT approx_quantile(duration_us, [0.5, 0.95]) FROM requests "
             "WHERE ts >= epoch_ms(?::BIGINT) AND starts_with(path, '/api/state')", p),
            (f"top 10 IPs on /api/state* ({label})",
             "SELECT ip, count(*) n FROM requests WHERE ts >= epoch_ms(?::BIGINT) AND starts_with(path, '/api/state') "
             "GROUP BY ip ORDER BY n DESC LIMIT 10", p),
            (f"explore series, host filter, split by status ({label})",
             f"SELECT time_bucket(INTERVAL '{max(1, (newest - since) // 300 // 60000)} minutes', ts) b, status, "
             "count(*), approx_quantile(duration_us, 0.95) FROM requests "
             "WHERE ts >= epoch_ms(?::BIGINT) AND host = 'api.example.com' GROUP BY ALL", p),
            (f"top user agents ({label})",
             "SELECT user_agent, count(*) n FROM requests WHERE ts >= epoch_ms(?::BIGINT) "
             "GROUP BY 1 ORDER BY n DESC LIMIT 10", p),
            (f"logs page: newest 100 with status 500 ({label})",
             "SELECT * FROM requests WHERE ts >= epoch_ms(?::BIGINT) AND status = 500 ORDER BY ts DESC, seq DESC "
             "LIMIT 100", p),
        ]
    checks.append(("unique IPs, exact (24h)",
                   "SELECT count(DISTINCT ip) FROM requests WHERE ts >= epoch_ms(?::BIGINT)", [newest - day]))
    checks.append(("logs page: newest 100, no filter (last 15 min)",
                   "SELECT * FROM requests WHERE ts >= epoch_ms(?::BIGINT) ORDER BY ts DESC, seq DESC LIMIT 100",
                   [newest - 15 * 60_000]))
    oldest = cur.execute("SELECT epoch_ms(min(ts)) FROM requests").fetchone()[0]
    print(f"{'query':70} {'cold':>8} {'warm':>8}")
    for label, sql, params in checks:
        if params and params[0] < oldest - 60_000:
            continue          # the dataset does not reach that far back
        cold, warm, result = timed(cur, sql, params)
        note = ""
        if "histogram" in label:
            note = "  p50/p95 us " + str(hist_quantiles(list(result[0][:-1]), [0.5, 0.95], result[0][-1]))
        elif "p50/p95" in label:
            note = "  p50/p95 us " + str([round(v) for v in result[0][0]])
        print(f"{label:70} {cold:8.3f} {warm:8.3f}{note}", flush=True)


def prune_day(store):
    oldest = store.w.execute("SELECT epoch_ms(min(ts)) FROM requests").fetchone()[0]
    t = time.perf_counter()
    store._oldest_ms = oldest
    steps = 0
    while store.prune_step(oldest + 86_400_000, 0):
        steps += 1
    deleted = time.perf_counter() - t
    t = time.perf_counter()
    store.checkpoint()
    print(f"\nprune one day: {steps} chunks in {deleted:.1f} s, checkpoint {time.perf_counter() - t:.1f} s, "
          f"file {os.path.getsize(store.db_path) / 1e9:.2f} GB", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db")
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--rate", type=float, default=300)
    ap.add_argument("--memory", default="512MB")
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--queries-only", action="store_true")
    ap.add_argument("--prune", action="store_true", help="also time pruning the oldest day")
    ap.add_argument("--rebuild-rollups", action="store_true", help="recreate minute_stats (after a schema change)")
    args = ap.parse_args()
    store = Store(args.db, args.memory, args.threads)
    try:
        if not args.queries_only:
            if store.w.execute("SELECT count(*) FROM requests").fetchone()[0]:
                sys.exit(f"{args.db} already has rows; use --queries-only or a new file")
            print(f"generating {args.days} days at {args.rate} req/s ...", flush=True)
            generate(store, args.days, args.rate)
        elif args.rebuild_rollups:
            rebuild_rollups(store)
        queries(store)
        if args.prune:
            prune_day(store)
    finally:
        store.close()


if __name__ == "__main__":
    main()
