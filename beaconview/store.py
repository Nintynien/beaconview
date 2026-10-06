"""DuckDB storage: raw requests, per-minute rollups, ingest positions, pruning.

One writer (the ingest thread) owns `self.w`; readers take their own cursor from `reader()`. DuckDB's MVCC lets
them run at the same time.

Rows go in by bulk-loading a temporary CSV file: binding Python values row by row (`executemany`, multi-row
VALUES) managed ~250 rows/s against DuckDB 1.5, while `read_csv` loads ~200,000/s.
"""

import csv
import os
import threading
import time

import duckdb

from .parsers import COLUMNS

SCHEMA_VERSION = 1
NULL = "\\N"
PRUNE_CHUNK_MS = 15 * 60 * 1000     # delete expired rows a quarter hour at a time

# the CSV layout of a batch: seq, instance, then the parser's columns
_CSV_TYPES = {
    "seq": "BIGINT", "instance": "VARCHAR", "ts_ms": "BIGINT", "host": "VARCHAR", "method": "VARCHAR",
    "path": "VARCHAR", "query": "VARCHAR", "status": "USMALLINT", "origin_status": "USMALLINT",
    "duration_us": "BIGINT", "bytes": "BIGINT", "ip": "VARCHAR", "router": "VARCHAR", "service": "VARCHAR",
    "entrypoint": "VARCHAR", "protocol": "VARCHAR", "tls": "VARCHAR", "user_agent": "VARCHAR",
    "referer": "VARCHAR", "headers": "VARCHAR",
}
_CSV_COLUMNS = ("seq", "instance") + COLUMNS
assert set(_CSV_COLUMNS) == set(_CSV_TYPES)
_CSV_SPEC = "{" + ", ".join(f"'{c}': '{_CSV_TYPES[c]}'" for c in _CSV_COLUMNS) + "}"

# Latency histogram kept per rollup row, so percentiles over any range come from the rollups instead of a scan of
# every raw row (approx_quantile over 7 days at 300 req/s took 5.4 s on 2 threads; the histogram answers in ms).
# Upper bounds in microseconds; the last bucket is open-ended. Changing them needs a rollup rebuild.
DURATION_EDGES_US = (100, 250, 500, 1_000, 2_500, 5_000, 10_000, 25_000, 50_000, 100_000, 250_000, 500_000,
                     1_000_000, 2_500_000, 5_000_000, 10_000_000, 30_000_000, 60_000_000)
HIST_COLUMNS = tuple(f"d{i:02d}" for i in range(len(DURATION_EDGES_US) + 1))


def _hist_select():
    parts, lower = [], None
    for col, upper in zip(HIST_COLUMNS, DURATION_EDGES_US + (None,)):
        cond = []
        if lower is not None:
            cond.append(f"duration_us >= {lower}")
        if upper is not None:
            cond.append(f"duration_us < {upper}")
        parts.append(f"count(*) FILTER ({' AND '.join(cond)})")
        lower = upper
    return ", ".join(parts)


def hist_quantiles(buckets, quantiles, max_us=None):
    """Quantiles (microseconds) from summed histogram buckets, interpolating linearly inside a bucket. The open
    last bucket ends at max_us when known. Accurate to the bucket width (e.g. p95 between 25 and 50 ms)."""
    total = sum(buckets)
    if not total:
        return [None for _ in quantiles]
    out = []
    for q in quantiles:
        target = q * total
        seen, lower = 0, 0
        for count, upper in zip(buckets, DURATION_EDGES_US + (None,)):
            if upper is None:
                upper = max(max_us or lower, lower)
            if count and seen + count >= target:
                out.append(round(lower + (upper - lower) * (target - seen) / count))
                break
            seen += count
            lower = upper
        else:
            out.append(lower)
    return out


# one rollup row per (minute, instance, host, router, service, status); callers add WHERE ... GROUP BY ALL
ROLLUP_SELECT = f"""
    SELECT time_bucket(INTERVAL 1 MINUTE, ts), instance, coalesce(host, ''), coalesce(router, ''),
           coalesce(service, ''), status, count(*), sum(bytes), sum(duration_us), max(duration_us), {_hist_select()}
    FROM requests"""

_DDL = [
    """CREATE TABLE IF NOT EXISTS requests (
        seq BIGINT NOT NULL,
        instance VARCHAR NOT NULL,
        ts TIMESTAMP NOT NULL,
        host VARCHAR, method VARCHAR, path VARCHAR, query VARCHAR,
        status USMALLINT, origin_status USMALLINT, duration_us BIGINT, bytes BIGINT,
        ip VARCHAR, router VARCHAR, service VARCHAR, entrypoint VARCHAR,
        protocol VARCHAR, tls VARCHAR, user_agent VARCHAR, referer VARCHAR,
        headers JSON)""",
    # Derived from requests, recomputed per dirty minute (see rollup()); '' stands for "none" so rows group.
    """CREATE TABLE IF NOT EXISTS minute_stats (
        minute TIMESTAMP NOT NULL, instance VARCHAR NOT NULL, host VARCHAR NOT NULL, router VARCHAR NOT NULL,
        service VARCHAR NOT NULL, status USMALLINT,
        requests BIGINT NOT NULL, bytes BIGINT, duration_sum_us BIGINT, duration_max_us BIGINT,
        """ + ", ".join(f"{c} UINTEGER" for c in HIST_COLUMNS) + ")",
    # minutes whose rollup is out of date; written in the same transaction as the rows that dirtied them
    "CREATE TABLE IF NOT EXISTS rollup_dirty (minute TIMESTAMP NOT NULL)",
    """CREATE TABLE IF NOT EXISTS ingest_state (
        instance VARCHAR PRIMARY KEY, path VARCHAR, inode UBIGINT, pos BIGINT, updated TIMESTAMP)""",
    "CREATE TABLE IF NOT EXISTS meta (key VARCHAR PRIMARY KEY, value VARCHAR)",
    # dotted IPv4 -> integer, NULL for anything else (IPv6, junk); used by ip=a.b.c.d/n filters
    r"""CREATE OR REPLACE MACRO ipv4_num(s) AS
        CASE WHEN regexp_full_match(s, '\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}')
             THEN (split_part(s, '.', 1)::UBIGINT << 24) + (split_part(s, '.', 2)::UBIGINT << 16)
                + (split_part(s, '.', 3)::UBIGINT << 8) + split_part(s, '.', 4)::UBIGINT END""",
]


class Store:
    def __init__(self, db_path, memory_limit="512MB", threads=2):
        self.db_path = db_path
        folder = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(folder, exist_ok=True)
        self.tmp_dir = os.path.join(folder, "tmp")
        os.makedirs(self.tmp_dir, exist_ok=True)
        self.con = duckdb.connect(db_path, config={
            "memory_limit": memory_limit,
            "threads": threads,
            "temp_directory": os.path.join(self.tmp_dir, "spill"),
        })
        self._lock = threading.Lock()
        self.w = self.con.cursor()
        for ddl in _DDL:
            self.w.execute(ddl)
        self.w.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?), ('created', ?)",
                       [str(SCHEMA_VERSION), str(int(time.time()))])
        self._oldest_ms = None      # cached min(ts) for pruning; None = unknown

    def close(self):
        try:
            self.w.execute("CHECKPOINT")
        finally:
            self.con.close()

    def reader(self):
        """A cursor for one reading thread (the connection object itself is not thread-safe)."""
        with self._lock:
            return self.con.cursor()

    # -- positions -------------------------------------------------------
    def load_positions(self):
        rows = self.w.execute("SELECT instance, path, inode, pos FROM ingest_state").fetchall()
        return {r[0]: (r[1], r[2], r[3]) for r in rows}

    def max_seq(self):
        return self.w.execute("SELECT coalesce(max(seq), 0) FROM requests").fetchone()[0]

    # -- writing ---------------------------------------------------------
    def write_batch(self, rows, positions):
        """Insert rows (tuples in _CSV_COLUMNS order) and record each tailer's position, atomically.

        positions: [(instance, path, inode, pos)]. The rows' minutes are marked dirty for rollup() in the same
        transaction, so a crash between the two never leaves a minute uncounted.
        """
        if rows:
            tmp = os.path.join(self.tmp_dir, "batch.csv")
            with open(tmp, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerows([NULL if v is None else v for v in row] for row in rows)
            minutes = sorted({row[2] // 60000 * 60000 for row in rows})
        self.w.execute("BEGIN")
        try:
            if rows:
                self.w.execute(
                    f"""INSERT INTO requests
                        SELECT seq, instance, epoch_ms(ts_ms), host, method, path, query, status, origin_status,
                               duration_us, bytes, ip, router, service, entrypoint, protocol, tls, user_agent,
                               referer, headers::JSON
                        FROM read_csv(?, header = false, delim = ',', quote = '"', escape = '"',
                                      nullstr = ?, columns = {_CSV_SPEC})""",
                    [tmp, NULL])
                self.w.execute("INSERT INTO rollup_dirty SELECT epoch_ms(unnest(?::BIGINT[]))", [minutes])
            for instance, path, inode, pos in positions:
                self.w.execute("INSERT OR REPLACE INTO ingest_state VALUES (?, ?, ?, ?, now())",
                               [instance, path, inode, pos])
            self.w.execute("COMMIT")
        except Exception:
            self.w.execute("ROLLBACK")
            raise
        if rows and self._oldest_ms is not None:
            self._oldest_ms = min(self._oldest_ms, minutes[0])

    def rollup(self, raw_cutoff_ms=None):
        """Recompute minute_stats for every dirty minute from the raw rows. Returns the number of minutes redone.

        Recomputing (rather than adding a batch's counts) makes the rollup exactly as right as the raw table,
        and a late row (a long request logged when it finished, keyed by when it started) dirties only its own
        minute. Minutes older than the raw retention are dropped from the dirty list: their raw rows are gone or
        going, and recomputing would erase a good rollup.
        """
        dirty = [r[0] for r in self.w.execute(
            "SELECT DISTINCT epoch_ms(minute) FROM rollup_dirty ORDER BY 1").fetchall()]
        if not dirty:
            return 0
        if raw_cutoff_ms is not None:
            dirty = [m for m in dirty if m >= raw_cutoff_ms]
        runs = []
        for m in dirty:
            if runs and m == runs[-1][1] + 60000:
                runs[-1][1] = m
            else:
                runs.append([m, m])
        self.w.execute("BEGIN")
        try:
            for start, end in runs:
                self.w.execute(
                    "DELETE FROM minute_stats WHERE minute BETWEEN epoch_ms(?::BIGINT) AND epoch_ms(?::BIGINT)",
                    [start, end])
                self.w.execute(
                    f"""INSERT INTO minute_stats {ROLLUP_SELECT}
                        WHERE ts >= epoch_ms(?::BIGINT) AND ts < epoch_ms(?::BIGINT)
                        GROUP BY ALL""",
                    [start, end + 60000])
            self.w.execute("DELETE FROM rollup_dirty")
            self.w.execute("COMMIT")
        except Exception:
            self.w.execute("ROLLBACK")
            raise
        return len(dirty)

    # -- retention -------------------------------------------------------
    def prune_step(self, raw_cutoff_ms, rollup_cutoff_ms):
        """Delete one chunk of expired raw rows. Returns True while more remain, so the caller can interleave
        ingest between chunks; the rollups (small) go in one statement on the last step."""
        if self._oldest_ms is None:
            first = self.w.execute("SELECT epoch_ms(min(ts)) FROM requests").fetchone()[0]
            self._oldest_ms = first if first is not None else raw_cutoff_ms
        if self._oldest_ms < raw_cutoff_ms:
            end = min(raw_cutoff_ms, self._oldest_ms + PRUNE_CHUNK_MS)
            self.w.execute("DELETE FROM requests WHERE ts < epoch_ms(?::BIGINT)", [end])
            self._oldest_ms = end
            return True             # the next step does the next chunk, or the rollups
        self.w.execute("DELETE FROM minute_stats WHERE minute < epoch_ms(?::BIGINT)", [rollup_cutoff_ms])
        self.w.execute("DELETE FROM rollup_dirty WHERE minute < epoch_ms(?::BIGINT)", [raw_cutoff_ms])
        return False

    def checkpoint(self):
        self.w.execute("CHECKPOINT")

    # -- reading ---------------------------------------------------------
    def storage_stats(self, cur=None):
        cur = cur or self.reader()
        rows, oldest, newest = cur.execute(
            "SELECT count(*), epoch_ms(min(ts)), epoch_ms(max(ts)) FROM requests").fetchone()
        rollup_rows = cur.execute("SELECT count(*) FROM minute_stats").fetchone()[0]
        size = 0
        for suffix in ("", ".wal"):
            try:
                size += os.path.getsize(self.db_path + suffix)
            except OSError:
                pass
        return {"rows": rows, "oldest_ms": oldest, "newest_ms": newest, "rollup_rows": rollup_rows,
                "db_bytes": size}
