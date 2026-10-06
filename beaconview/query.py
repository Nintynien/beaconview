"""Filters -> SQL, and the queries behind /api/summary, /api/series, /api/top, /api/logs and /api/fields.

A query is a time range plus filters taken from the URL. Every field and operator comes from the whitelist below and
every value is a bound parameter, so nothing a visitor types becomes SQL text.

URL form (repeat a field to OR its values; prefix a value with ! to exclude it):
    from=<epoch ms>&to=<epoch ms>
    host=example.com  path=/api/*  path=*state*  status=4xx  status=!304  ip=203.0.113.0/24  ip=2001:db8:*
    user_agent=bot  referer=google  min_ms=500  max_ms=2000  h.X-Request-Id=abc  instance=edge

When every filter is on a rollup dimension (instance, host, router, service, status), counts, bytes and percentiles
come from minute_stats, which answers any range in milliseconds; anything else reads the raw rows.
"""

import ipaddress
import threading
import time

import duckdb

from .store import DURATION_EDGES_US, HIST_COLUMNS, hist_quantiles

NONE = "(none)"                       # the value that matches a missing field
QUERY_TIMEOUT_SECONDS = 30
MAX_BUCKETS = 400
SPLIT_TOP = 5
OTHER = "Other"
STEPS_S = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400)
EXACT_UNIQUE_MAX_MS = 86_400_000      # longer ranges count unique IPs approximately (HyperLogLog)

# name -> (kind, raw column, rollup column or None, label)
FIELDS = {
    "instance": ("str", "instance", "instance", "Instance"),
    "host": ("str", "host", "host", "Host"),
    "router": ("str", "router", "router", "Router"),
    "service": ("str", "service", "service", "Service"),
    "entrypoint": ("str", "entrypoint", None, "Entrypoint"),
    "method": ("str", "method", None, "Method"),
    "path": ("glob", "path", None, "Path"),
    "query": ("contains", "query", None, "Query string"),
    "status": ("status", "status", "status", "Status"),
    "origin_status": ("status", "origin_status", None, "Origin status"),
    "ip": ("ip", "ip", None, "Client IP"),
    "user_agent": ("contains", "user_agent", None, "User agent"),
    "referer": ("contains", "referer", None, "Referer"),
    "protocol": ("str", "protocol", None, "Protocol"),
    "tls": ("str", "tls", None, "TLS"),
}
# fields that can split the chart / be ranked by /api/top; "status_class" is derived
SPLITS = ("status_class",) + tuple(FIELDS)
LOG_COLUMNS = ("seq", "instance", "ts", "host", "method", "path", "query", "status", "origin_status", "duration_us",
               "bytes", "ip", "router", "service", "entrypoint", "protocol", "tls", "user_agent", "referer",
               "headers")


class BadQuery(ValueError):
    """The request asked for something that is not allowed or does not parse (HTTP 400)."""


class QueryTimeout(RuntimeError):
    """A query ran past QUERY_TIMEOUT_SECONDS and was interrupted (HTTP 504)."""


# -- parsing ---------------------------------------------------------------

class Query:
    def __init__(self, from_ms, to_ms, conds, min_us=None, max_us=None, headers=()):
        self.from_ms, self.to_ms = from_ms, to_ms
        self.conds = conds              # [(field, [positive values], [negative values])]
        self.min_us, self.max_us = min_us, max_us
        self.headers = headers          # [(name, [pos], [neg])]

    @property
    def span_ms(self):
        return self.to_ms - self.from_ms

    def rollup_ok(self, extra_fields=()):
        """True when minute_stats can answer: rollup-dimension filters only, and the extra fields too."""
        if self.min_us is not None or self.max_us is not None or self.headers:
            return False
        fields = [f for f, _, _ in self.conds] + list(extra_fields)
        return all(f == "status_class" or FIELDS[f][2] is not None for f in fields)


def parse(params, now_ms=None):
    """parse_qs-style dict -> Query. Raises BadQuery."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    to_ms = _int(params, "to", now_ms)
    from_ms = _int(params, "from", to_ms - 3_600_000)
    if from_ms >= to_ms:
        raise BadQuery("from must be before to")
    conds, headers = [], []
    for key, values in params.items():
        if key in FIELDS:
            pos = [v for v in values if not v.startswith("!")]
            neg = [v[1:] for v in values if v.startswith("!")]
            for v in pos + neg:
                _check_value(key, v)
            conds.append((key, pos, neg))
        elif key.startswith("h.") and len(key) > 2:
            headers.append((key[2:], [v for v in values if not v.startswith("!")],
                            [v[1:] for v in values if v.startswith("!")]))
    min_us = _int(params, "min_ms", None)
    max_us = _int(params, "max_ms", None)
    return Query(from_ms, to_ms, conds,
                 None if min_us is None else min_us * 1000,
                 None if max_us is None else max_us * 1000,
                 headers)


def _int(params, name, default):
    raw = params.get(name, [None])[0]
    if raw in (None, ""):
        return default
    try:
        return int(float(raw))
    except ValueError:
        raise BadQuery(f"{name} must be a number") from None


def _check_value(field, value):
    kind = FIELDS[field][0]
    if kind == "status" and value != NONE:
        if not (value.isdigit() or (len(value) == 3 and value[0].isdigit() and value[1:].lower() == "xx")):
            raise BadQuery(f"{field}: {value!r} is not a status code (404) or class (4xx)")
    if kind == "ip" and "/" in value:
        try:
            net = ipaddress.ip_network(value, strict=False)
        except ValueError:
            raise BadQuery(f"ip: {value!r} is not an address or range") from None
        if net.version != 4:
            raise BadQuery("ip: IPv6 ranges are not supported; use a prefix like 2001:db8:*")


# -- SQL building ----------------------------------------------------------

def _match(kind, col, value, params, rollup):
    """SQL for `col matches value`, appending bound parameters."""
    if value == NONE:
        return f"{col} = ''" if rollup else f"{col} IS NULL"
    if kind == "status":
        if value.lower().endswith("xx"):
            lo = int(value[0]) * 100
            params += [lo, lo + 99]
            return f"{col} BETWEEN ? AND ?"
        params.append(int(value))
        return f"{col} = ?"
    if kind == "contains":
        params.append(value.lower())
        return f"contains(lower({col}), ?)"
    if kind == "ip" and "/" in value:
        net = ipaddress.ip_network(value, strict=False)
        params += [int(net.network_address), int(net.broadcast_address)]
        return f"ipv4_num({col}) BETWEEN ? AND ?"
    if kind in ("glob", "ip", "header") and "*" in value:
        core = value.strip("*")
        if value.startswith("*") and value.endswith("*") and len(value) > 1:
            params.append(core)
            return f"contains({col}, ?)"
        if value.endswith("*"):
            params.append(value[:-1])
            return f"starts_with({col}, ?)"
        if value.startswith("*"):
            params.append(value[1:])
            return f"ends_with({col}, ?)"
    params.append(value)
    return f"{col} = ?"


def _group(kind, col, pos, neg, params, rollup):
    parts = []
    if pos:
        parts.append("(" + " OR ".join(_match(kind, col, v, params, rollup) for v in pos) + ")")
    if neg:
        # coalesce: a missing value is "not this value", so it stays in
        parts.append("NOT coalesce((" + " OR ".join(_match(kind, col, v, params, rollup) for v in neg) + "), false)")
    return parts


def where(q, rollup=False):
    """(SQL WHERE body, params) for the query's range and filters on requests or, with rollup, minute_stats."""
    params = []
    if rollup:
        clauses = ["minute >= epoch_ms(?::BIGINT)", "minute < epoch_ms(?::BIGINT)"]
        params += [q.from_ms // 60000 * 60000, q.to_ms]
    else:
        clauses = ["ts >= epoch_ms(?::BIGINT)", "ts < epoch_ms(?::BIGINT)"]
        params += [q.from_ms, q.to_ms]
    for field, pos, neg in q.conds:
        kind, raw_col, roll_col, _ = FIELDS[field]
        clauses += _group(kind, roll_col if rollup else raw_col, pos, neg, params, rollup)
    if q.min_us is not None:
        clauses.append("duration_us >= ?")
        params.append(q.min_us)
    if q.max_us is not None:
        clauses.append("duration_us <= ?")
        params.append(q.max_us)
    for name, pos, neg in q.headers:
        clauses += _header_group(name, pos, neg, params)
    return " AND ".join(clauses), params


def _header_group(name, pos, neg, params):
    """A kept request header (stored in the headers JSON). The header name is a bound parameter too: the JSON
    path is assembled in SQL, with quotes stripped so it cannot leave the key."""
    col = "json_extract_string(headers, '$.\"' || replace(?, '\"', '') || '\"')"
    parts = []
    for values, negate in ((pos, False), (neg, True)):
        if not values:
            continue
        ors = []
        for v in values:
            params.append(name)
            ors.append(_match("header", col, v, params, False))
        body = "(" + " OR ".join(ors) + ")"
        parts.append(f"NOT coalesce({body}, false)" if negate else body)
    return parts


def _key_expr(split, rollup):
    """SQL for the split/top key, as text, '(none)' for missing."""
    if split == "status_class":
        return "CASE WHEN status IS NULL THEN '(none)' ELSE (status // 100)::VARCHAR || 'xx' END"
    kind, raw_col, roll_col, _ = FIELDS[split]
    col = roll_col if rollup else raw_col
    if rollup and FIELDS[split][0] == "str":
        return f"CASE WHEN {col} = '' THEN '(none)' ELSE {col} END"
    return f"coalesce({col}::VARCHAR, '(none)')"


# -- running ---------------------------------------------------------------

class Runner:
    """Runs queries on a fresh cursor with a time limit."""

    def __init__(self, store, timeout=QUERY_TIMEOUT_SECONDS):
        self.store = store
        self.timeout = timeout

    def run(self, sql, params):
        cur = self.store.reader()
        timer = threading.Timer(self.timeout, cur.interrupt)
        timer.start()
        try:
            return cur.execute(sql, params).fetchall()
        except duckdb.InterruptException:
            raise QueryTimeout(f"the query took longer than {self.timeout} s; narrow the time range or filters")
        finally:
            timer.cancel()
            cur.close()

    # -- /api/summary ----------------------------------------------------
    def summary(self, q):
        rollup = q.rollup_ok()
        if rollup:
            w, p = where(q, rollup=True)
            row = self.run(
                f"""SELECT coalesce(sum(requests), 0)::BIGINT, sum(bytes)::BIGINT,
                           coalesce(sum(requests) FILTER (status BETWEEN 400 AND 499), 0)::BIGINT,
                           coalesce(sum(requests) FILTER (status >= 500), 0)::BIGINT,
                           max(duration_max_us), {", ".join(f"coalesce(sum({c}), 0)::BIGINT" for c in HIST_COLUMNS)}
                    FROM minute_stats WHERE {w}""", p)[0]
            count, nbytes, c4, c5, dmax = row[:5]
            p50, p95 = hist_quantiles(list(row[5:]), [0.5, 0.95], dmax)
        else:
            w, p = where(q)
            count, nbytes, c4, c5, quants = self.run(
                f"""SELECT count(*), sum(bytes)::BIGINT, count(*) FILTER (status BETWEEN 400 AND 499),
                           count(*) FILTER (status >= 500), approx_quantile(duration_us, [0.5, 0.95])
                    FROM requests WHERE {w}""", p)[0]
            p50, p95 = quants if quants else (None, None)
        approx = q.span_ms > EXACT_UNIQUE_MAX_MS
        w, p = where(q)
        unique = self.run(
            f"SELECT {'approx_count_distinct(ip)' if approx else 'count(DISTINCT ip)'} FROM requests WHERE {w}", p)[0][0]
        return {
            "from": q.from_ms, "to": q.to_ms, "requests": count, "bytes": nbytes or 0,
            "rps": round(count / (q.span_ms / 1000), 3), "status_4xx": c4, "status_5xx": c5,
            "unique_ips": unique, "unique_ips_approx": approx,
            "p50_us": _round(p50), "p95_us": _round(p95), "source": "rollup" if rollup else "raw",
        }

    # -- /api/series -----------------------------------------------------
    def series(self, q, buckets=120, split=None):
        if split is not None and split not in SPLITS:
            raise BadQuery(f"split: unknown field {split!r}")
        buckets = max(10, min(MAX_BUCKETS, buckets))
        step_s = next((s for s in STEPS_S if s * buckets * 1000 >= q.span_ms), STEPS_S[-1])
        step = step_s * 1000
        rollup = step >= 60_000 and q.rollup_ok([split] if split else [])
        start = q.from_ms // step * step
        n = -(-(q.to_ms - start) // step)
        ts_expr = "epoch_ms(minute)" if rollup else "epoch_ms(ts)"
        bucket = f"({ts_expr} // {step}) * {step}"
        count_expr = "sum(requests)" if rollup else "count(*)"
        table = "minute_stats" if rollup else "requests"
        w, p = where(q, rollup=rollup)

        keys = []
        if split:
            key = _key_expr(split, rollup)
            top = self.run(f"SELECT {key} k, {count_expr} n FROM {table} WHERE {w} GROUP BY k ORDER BY n DESC, k "
                           f"LIMIT {SPLIT_TOP + 1}", p)
            keys = [r[0] for r in top[:SPLIT_TOP]]
            has_other = len(top) > SPLIT_TOP
            if keys:
                in_list = ", ".join("?" for _ in keys)
                key_sql = f"CASE WHEN {key} IN ({in_list}) THEN {key} ELSE '{OTHER}' END"
                params = list(keys) + p
            else:
                key_sql, params = "'all'", p
            rows = self.run(f"SELECT {bucket} b, {key_sql} k, {count_expr} FROM {table} WHERE {w} GROUP BY ALL",
                            params)
            names = keys + ([OTHER] if has_other else [])
        else:
            rows = [(b, "all", c) for b, c in self.run(
                f"SELECT {bucket} b, {count_expr} FROM {table} WHERE {w} GROUP BY ALL", p)]
            names = ["all"]
        index = {name: i for i, name in enumerate(names)}
        values = [[0] * n for _ in names]
        for b, k, c in rows:
            i = (b - start) // step
            if 0 <= i < n and k in index:
                values[index[k]][i] = int(c)

        p50 = [None] * n
        p95 = [None] * n
        if rollup:
            hist = self.run(f"SELECT {bucket} b, max(duration_max_us), "
                            f"{', '.join(f'sum({c})::BIGINT' for c in HIST_COLUMNS)} "
                            f"FROM minute_stats WHERE {w} GROUP BY ALL", p)
            for row in hist:
                i = (row[0] - start) // step
                if 0 <= i < n:
                    p50[i], p95[i] = hist_quantiles(list(row[2:]), [0.5, 0.95], row[1])
        else:
            for b, quants in self.run(f"SELECT {bucket} b, approx_quantile(duration_us, [0.5, 0.95]) "
                                      f"FROM requests WHERE {w} GROUP BY ALL", p):
                i = (b - start) // step
                if 0 <= i < n and quants:
                    p50[i], p95[i] = quants
        return {
            "from": q.from_ms, "to": q.to_ms, "start": start, "step": step, "buckets": n, "split": split,
            "series": [{"key": name, "values": values[i]} for i, name in enumerate(names)],
            "latency": {"p50_us": [_round(v) for v in p50], "p95_us": [_round(v) for v in p95]},
            "source": "rollup" if rollup else "raw",
        }

    # -- /api/top --------------------------------------------------------
    def top(self, q, field, limit=10):
        if field not in SPLITS:
            raise BadQuery(f"field: unknown field {field!r}")
        limit = max(1, min(50, limit))
        rollup = q.rollup_ok([field])
        table = "minute_stats" if rollup else "requests"
        count_expr = "sum(requests)" if rollup else "count(*)"
        key = _key_expr(field, rollup)
        w, p = where(q, rollup=rollup)
        rows = self.run(f"SELECT {key} k, {count_expr} n FROM {table} WHERE {w} GROUP BY k ORDER BY n DESC, k "
                        f"LIMIT {limit}", p)
        total, distinct = self.run(
            f"SELECT coalesce({count_expr}, 0), approx_count_distinct({key}) FROM {table} WHERE {w}", p)[0]
        return {
            "field": field, "total": int(total), "distinct": int(distinct or 0), "distinct_approx": True,
            "values": [{"value": k, "count": int(c), "share": round(c / total, 4) if total else 0} for k, c in rows],
            "source": "rollup" if rollup else "raw",
        }

    # -- /api/logs -------------------------------------------------------
    def logs(self, q, limit=100, before=None, after_seq=None):
        """Newest first. `before` is a (ts_ms, seq) cursor from the previous page; `after_seq` returns only rows
        ingested after that seq (live mode). Searches outward from the newest end of the range in growing windows,
        so a page from a 30-day range does not sort 30 days of rows."""
        limit = max(1, min(500, limit))
        cols = ", ".join("epoch_ms(ts)" if c == "ts" else c for c in LOG_COLUMNS)
        hi = q.to_ms if before is None else min(q.to_ms, before[0] + 1)
        window = 15 * 60_000
        rows = []
        while True:
            lo = max(q.from_ms, hi - window)
            sub = Query(lo, hi, q.conds, q.min_us, q.max_us, q.headers)
            w, p = where(sub)
            extra = ""
            if before is not None:
                extra += " AND (epoch_ms(ts) < ? OR (epoch_ms(ts) = ? AND seq < ?))"
                p += [before[0], before[0], before[1]]
            if after_seq is not None:
                extra += " AND seq > ?"
                p.append(after_seq)
            rows = self.run(f"SELECT {cols} FROM requests WHERE {w}{extra} ORDER BY ts DESC, seq DESC LIMIT {limit}",
                            p)
            if len(rows) >= limit or lo <= q.from_ms:
                break
            window *= 4                      # widen and search the same newest end again
        more = len(rows) == limit
        return {
            "columns": list(LOG_COLUMNS), "rows": [list(r) for r in rows],
            "next": [rows[-1][2], rows[-1][0]] if more else None,
            "max_seq": max((r[0] for r in rows), default=after_seq),
        }

    # -- /api/fields -----------------------------------------------------
    def fields(self):
        values = {}
        for name in ("instance", "host", "router", "service"):
            rows = self.run(f"SELECT {name}, sum(requests) n FROM minute_stats WHERE {name} <> '' "
                            f"GROUP BY 1 ORDER BY n DESC LIMIT 200", [])
            values[name] = [r[0] for r in rows]
        return {
            "fields": [{"name": k, "kind": v[0], "label": v[3], "rollup": v[2] is not None}
                       for k, v in FIELDS.items()],
            "splits": list(SPLITS),
            "values": values,
            "duration_edges_us": list(DURATION_EDGES_US),
        }


def _round(v):
    return None if v is None else int(round(v))
