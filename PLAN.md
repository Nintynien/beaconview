# BeaconView — plan

A small, self-hosted request explorer for Traefik. It reads Traefik's JSON access log, keeps a configurable window
of every request in an embedded columnar database, and serves two pages: an overview of the whole proxy and an
explorer for slicing requests by time, host, path, status, IP or user agent (chart on top, log lines below).

Status: milestone 1 (ingest + storage) implemented and load-tested; milestones 2–4 next. Named BeaconView (2026-10-05): independent of the Traefik mark, so it also fits if other proxies
(Caddy, nginx JSON logs) are supported later. The README's first line should say it reads access logs and adds no
tracking, since "web beacon" also means a tracking pixel.

## Goals

- **Answer "who is hitting what, how often, and what did we answer"** for any time range in the retention window:
  unique IPs, top clients, status mix (200 vs 304), latency, per router/service/host/path.
- **Never touch the request path.** BeaconView only reads the log file Traefik already writes. If it is slow,
  stopped or crashed, Traefik and its traffic are unaffected; unread lines wait in the file.
- **Scale without redesign** from a few requests per second to thousands, with retention from days to months.
- **One container, one volume, a few environment variables.** No database server, no build step for the frontend.
- **Open source friendly:** nothing deployment-specific in the code; MIT license.

### Not in v1

Traefik's error log, Traefik's Prometheus metrics, alerting, remote Traefik instances (v2, see Multiple instances;
v1 already stores an `instance` per row and tails several local files), log formats other than Traefik's JSON
(v1.x, see Log formats; the parser seam ships in v1), user accounts beyond optional basic auth.

## Architecture

```
Traefik ──writes──> access.log (JSON) ──tail (read-only mount)──> ingest thread ──batches──> DuckDB file
                                                                                    │
                                     browser <── HTTP server (pages + JSON API) <───┘
                                                         prune thread (hourly) ──────┘
```

One Python process, three kinds of thread: ingest, prune, HTTP (one per connection, stdlib `ThreadingHTTPServer`).
Each uses its own DuckDB cursor on one shared database connection. DuckDB runs queries outside the Python GIL, so a
heavy dashboard query never stalls ingest, and its MVCC lets queries and inserts run concurrently.

### Why DuckDB

| Need | Why DuckDB fits |
|---|---|
| Range analytics over 10⁸–10⁹ rows | Columnar, vectorised, parallel; `count`, `approx_count_distinct`, `approx_quantile`, top-N in seconds |
| Small footprint | Embedded in the process, single file, `memory_limit` and `threads` caps, spills to disk past the cap |
| Storage at high volume | Automatic compression (dictionary, RLE, bit-packing); repeated hosts, paths, user agents cost little |
| Time-range filters without indexes | Min/max zone maps per row group; rows arrive roughly in time order, so ranges skip whole blocks |
| Exactly-once ingest | Transactions: a batch and the file position commit together |

Considered and rejected: **SQLite** (row store; 30-day ranges at 300 req/s mean scanning ~100 GB), **Polars** (a
dataframe library, not a store: no transactions, concurrent writes or bounded-memory queries without building them
ourselves), **ClickHouse** (a server to run and ~1 GB RAM idle; right at much larger scale, wrong for one small
container).

Known DuckDB trade-offs: slow at single-row inserts (we batch), and the file does not shrink after deletes — freed
blocks are reused, so it plateaus at about one retention window of data. If that ever matters, the fallback is
day-partitioned Parquet files (retention = delete a file), behind the same query layer.

### Language

Python 3.13 with one dependency (`duckdb`). Stdlib `json` parses tens of thousands of lines per second, about 100× the
300 req/s target, and the heavy work runs inside DuckDB. Revisit (Go + go-duckdb) only if the load test says so.

## Ingest

- **Source:** `ACCESS_LOG` (default `/var/log/traefik/access.log`), mounted read-only. Several Traefiks on one host:
  `ACCESS_LOGS=edge=/logs/edge/access.log,internal=/logs/internal/access.log` (`instance=path` pairs), one tailer each.
  A pair may name its format, `web=nginx-json:/logs/nginx/access.log`; the default is `LOG_FORMAT` (`traefik`).
- **Tailing:** read in large chunks; parse complete lines; flush a batch every `BATCH_SECONDS` (default 1) or
  `BATCH_ROWS` (default 50,000), whichever comes first. Idle polling at 250 ms when at EOF.
- **Rotation:**
  - *create* (logrotate default, also Traefik's USR1 reopen): the path's inode changes. Keep reading the old file
    descriptor to EOF, wait a short grace period for Traefik's final writes, then switch to the new file at 0.
  - *copytruncate*: the file is smaller than our offset — restart at 0.
- **Exactly once:** `ingest_state(instance, path, inode, offset, last_ts)` is updated in the same transaction as the batch's
  rows, so a crash or restart neither drops nor duplicates a line.
- **Backfill (`BACKFILL=1`, first run only):** read `access.log.N` and `access.log.N.gz` oldest first, skipping rows
  older than the retention window, before starting the live tail.
- **Ordering:** Traefik writes a line when the request *finishes*, keyed by when it *started*, so a long request
  (e.g. a websocket open for hours) arrives late with an old timestamp. Rows are stored by start time; rollups are
  updated incrementally (below), so late rows still count.
- **Bad lines:** counted, and the last one kept for the status page; never logged per line.

### Fields

From Traefik's JSON access log:

| Column | Source | Notes |
|---|---|---|
| `seq` | assigned | BIGINT from a sequence; stable cursor for paging and live mode |
| `instance` | config | which Traefik the line came from (`INSTANCE_NAME`, default the hostname); a filter like any other |
| `ts` | `StartUTC` | TIMESTAMP (ms) |
| `host` | `RequestHost` | |
| `method` | `RequestMethod` | |
| `path` | `RequestPath` | query string split off |
| `query` | `RequestPath` | the part after `?`, nullable |
| `status` | `DownstreamStatus` | what the client got |
| `origin_status` | `OriginStatus` | what the backend answered (0 when Traefik answered itself) |
| `duration_us` | `Duration` | Traefik logs nanoseconds |
| `bytes` | `DownstreamContentSize` | |
| `ip` | `ClientHost` or `CLIENT_IP_HEADER` | see below |
| `router`, `service`, `entrypoint` | `RouterName`, `ServiceName`, `EntryPointName` | |
| `protocol`, `tls` | `RequestProtocol`, `TLSVersion` | |
| `user_agent`, `referer` | `request_User-Agent`, `request_Referer` | only if Traefik keeps those headers |
| `headers` | other kept `request_*` fields | MAP, so any extra header someone keeps is queryable |

- **Real client IP:** `CLIENT_IP_HEADER` (e.g. `CF-Connecting-IP`, `X-Forwarded-For` → first address) is used when
  set and present in the line; otherwise `ClientHost`. Behind Cloudflare or another proxy this is required, or every
  IP is the proxy's.
- **`ANONYMIZE_IPS=1`:** truncate to /24 (IPv4) or /48 (IPv6) before storing.

### Log formats

The table above is BeaconView's own row shape, not Traefik's. A parser turns one log line into that row (or rejects
it); everything after the parser (storage, rollups, queries, pages) knows nothing about where the line came from.
Columns a source cannot fill are NULL, and the pages hide a dimension that is empty for the selected instances.

- **Explicit, not auto-detected:** each log names its format (`LOG_FORMAT`, or per pair in `ACCESS_LOGS`). Sniffing
  the first line is fragile (an nginx JSON `log_format` can look like anything) and a wrong guess silently stores
  garbage. At startup the first few lines are test-parsed and a mismatch is reported on the status page.
- **v1 ships `traefik` only**, behind the parser interface, with fixture tests that any new parser must also pass
  (round-trip of every column, bad lines, NULL handling).
- **Next formats (v1.x), in order of effort:**

| Format | Notes | Columns it cannot fill |
|---|---|---|
| `caddy` | Caddy's native JSON access log; nested fields (`request.host`, `duration` in seconds) | `router`, `entrypoint` (`service` only via a logged upstream) |
| `nginx-json` | nginx `log_format … escape=json`; README ships a ready-made `log_format` using `$request_time`, `$upstream_addr`, `$upstream_status`, `$host`, `$ssl_protocol` | `router`, `entrypoint` (`service` = upstream address) |
| `nginx-combined` | nginx/Apache default text format, parsed by regex | `host`, duration, upstream, TLS: combined has none of them, so latency charts and per-host views stay empty |

  `nginx-combined` is supported for convenience but documented as the weak option: the README steers nginx users to
  the JSON `log_format`, which takes one config line and gives nearly everything Traefik does.
- **Naming:** the UI says "router / service / entrypoint" when the instance is Traefik and the generic "route /
  upstream / listener" otherwise; the columns themselves keep one name.

## Storage

- **`requests`:** one row per request, columns above. No indexes: zone maps on `ts` do the range pruning.
- **`minute_stats`:** one row per `(minute, instance, host, router, service, status)` with `requests`, `bytes`,
  `duration_sum_us`, `duration_max_us` and a **latency histogram** (`d00`…`d18`, 19 log-spaced buckets from 100 µs to
  60 s). Percentiles for any range whose filters are rollup dimensions come from summed buckets
  (`store.hist_quantiles`), accurate to the bucket width; measured 5 ms vs 860 ms (24 h) and 5.4 s (7 d) for
  `approx_quantile` on raw rows. Filters on raw-only fields (path, IP, user agent) use `approx_quantile` on the
  matched rows, whose cost scales with the match count; sampling did not help there (a selective filter is already
  cheaper than a sample scan).
- **Rollups are derived, not accumulated:** each batch marks its minutes in `rollup_dirty` in the same transaction as
  its rows; every 10 s the dirty minutes are recomputed from `requests` (delete + insert per run of minutes). A
  late row (long request logged at its end) just dirties its own minute, and a crash can never leave a rollup short.
  Dirty minutes already past raw retention are skipped so a good rollup is never erased.
- **Bulk load:** a batch is written to a temporary CSV and loaded with `read_csv`. Binding Python values
  (`executemany`, multi-row `VALUES`) ran ~250 rows/s on DuckDB 1.5.6; `read_csv` ~196,000 rows/s.
- **One writer:** ingest, rollups and prune all run on the ingest thread, interleaved in small steps, so writes
  never conflict; HTTP threads only read (one cursor each).
- **`ingest_state`**, **`meta`** (schema version, created at).

### Retention

| Setting | Default | |
|---|---|---|
| `RETENTION_DAYS` | 7 | raw requests |
| `ROLLUP_RETENTION_DAYS` | 90 | minute totals (tiny) |

Hourly: delete expired rows in chunks (by `ts` ranges) so no single delete holds a long transaction, then
`CHECKPOINT`. The README will carry a sizing table measured by the load test, e.g.:

| Traffic | Rows/day | 7 days | 30 days | 60 days |
|---|---|---|---|---|
| 3 req/s | 0.26 M | 1.8 M | 7.8 M | 16 M |
| 300 req/s | 26 M | 180 M | 780 M | 1.6 B |
| 3,000 req/s | 260 M | 1.8 B | 7.8 B | — |

Measured 17–20 bytes/row (synthetic traffic, see Load test), so about **3.1 GB per 7 days at 300 req/s**; by
extrapolation ~14 GB for 30 days and ~28 GB for 60 days (30-day run not completed yet). Real logs with more varied
paths and user agents will compress somewhat worse.

## Pages

No build step: plain HTML, CSS and JS, with [uPlot](https://github.com/leeoniya/uPlot) (MIT, ~50 KB) vendored into
`static/vendor/`. No third-party requests at runtime. Light/dark theme.

Look: dark-first, with one high-visibility accent (signal yellow or electric blue) for live data against muted
chrome; logo a minimal lighthouse casting a geometric beam. Status colours stay conventional (2xx/3xx/4xx/5xx)
and must hold up in both themes.

Shared: a **time-range picker** (15m, 1h, 6h, 24h, 7d, 30d, custom start/end, clamped to the retention window), and the
whole query in the URL so any view can be bookmarked or shared. Drag across a chart to zoom the range.

### Overview (`/`)

- **Tiles:** requests, req/s, unique IPs, error rate (5xx and 4xx separately), p95 latency, bytes out — each with the
  change vs the previous period of the same length.
- **Chart:** requests over time stacked by status class.
- **Tables:** routers/services (req/s, error %, p95), top hosts, top IPs, top user agents, top paths, status codes,
  recent 5xx. Every row links into Explore with that filter applied.
- **Status strip:** ingest lag (now − newest line), lines/s, parse errors, DB size, row count, oldest row.

Counts, bytes and averages come from `minute_stats`; unique IPs and percentiles from `requests`.

### Explore (`/explore`)

- **Filter bar:** host, router, service, method, path (exact or prefix `/api/*`), status (code or class, lists),
  IP (exact or CIDR), user agent (contains), minimum duration, any kept header. Each filter can be negated
  (`status != 304`). Chips show active filters.
- **Chart (top):** requests over time; bucket size picked for ~300 points; optional *split by* status, host, service,
  router, IP or user agent (top 5 + other); latency p50/p95 on a second axis.
- **Summary bar:** matches, unique IPs, p50/p95, bytes.
- **Top values panel:** pick a field, see its top values for the current filters ("which IPs hit `/api/state` in
  this hour" is one click).
- **Log lines (bottom):** newest first, keyset-paged by `seq` ("load more"). Click a row for every field; click an IP,
  path or user agent to add it as a filter.
- **Live mode:** polls `/api/logs?after=<seq>` every 5 s and prepends new matching lines.

## API

All JSON, all bounded by a time range, all results capped.

| Endpoint | Returns |
|---|---|
| `GET /api/overview?from&to` | tiles, previous-period tiles, status series, top tables |
| `GET /api/series?<filters>&bucket&split` | time buckets × series, plus p50/p95 |
| `GET /api/top?field&<filters>&limit` | top values with counts and share |
| `GET /api/logs?<filters>&before=<seq>&after=<seq>&limit` | log lines |
| `GET /api/fields` | filterable fields and their known values (hosts, routers, services) |
| `GET /api/status` | ingest and storage health (also used by the container healthcheck) |

Filters are parsed into parameterised SQL from a whitelist of fields and operators; user input never becomes SQL text.
Unique counts use `approx_count_distinct` (HyperLogLog, ~2% error) and are labelled "≈"; ranges under 24 h use an
exact count.

## Configuration

| Variable | Default | |
|---|---|---|
| `ACCESS_LOG` | `/var/log/traefik/access.log` | |
| `ACCESS_LOGS` | — | several local logs: `name=path,name=path` (overrides `ACCESS_LOG`) |
| `INSTANCE_NAME` | hostname | instance label for `ACCESS_LOG` |
| `LOG_FORMAT` | `traefik` | parser for logs that do not name one (v1: `traefik` only) |
| `DATA_DIR` | `/data` | holds `beaconview.duckdb` |
| `PORT` | `8080` | |
| `RETENTION_DAYS` | `7` | |
| `ROLLUP_RETENTION_DAYS` | `90` | |
| `BACKFILL` | `0` | read rotated files on first run |
| `CLIENT_IP_HEADER` | — | e.g. `CF-Connecting-IP` |
| `ANONYMIZE_IPS` | `0` | |
| `DB_MEMORY_LIMIT` | `512MB` | DuckDB `memory_limit` |
| `DB_THREADS` | `2` | DuckDB `threads` |
| `BATCH_SECONDS` / `BATCH_ROWS` | `1` / `50000` | |
| `BASIC_AUTH_USER` / `BASIC_AUTH_PASSWORD` | — | optional built-in auth |

The README states plainly: this shows visitor IPs and request paths; never expose it without auth (Traefik
middleware or the built-in basic auth).

## Repository layout

```
beaconview/
  beaconview/        __main__.py  config.py  ingest.py  store.py  query.py  server.py
  static/              index.html  explore.html  app.css  common.js  overview.js  explore.js  vendor/uplot.*
  tests/
  tools/               genlog.py (synthetic Traefik log writer)  bench.py (query timings)
  Dockerfile           python:3.13-slim (DuckDB wheels target glibc)
  .github/workflows/   build.yml: tests, then multi-arch image to GHCR
  docker-compose.example.yml
  README.md  LICENSE (MIT)  PLAN.md
```

## Tests

pytest with recorded and generated log lines:

- parsing every field; missing fields; bad lines; `CLIENT_IP_HEADER` and `X-Forwarded-For` chains; anonymisation
- rotation: create + delaycompress, copytruncate, Traefik writing to the old file after rename
- restart mid-file: no lost or duplicate rows
- late rows (old `StartUTC`) land in the right minute of `minute_stats`
- pruning: raw and rollup retention, chunked deletes
- query layer: every filter and negation, CIDR, keyset paging, series bucketing, split-by top-N + other
- HTTP: routes, caps, auth on/off
- a headless browser check of both pages against a generated 7-day log

## Load test (part of milestone 1)

`tools/genlog.py` writes realistic lines (a host/path/IP/user-agent mix with skew, status mix, latency
distribution) at a set rate, rotating like logrotate. `tools/bench.py` times the API queries.

| Scenario | Measure | Target |
|---|---|---|
| Live tail at 300 req/s for 1 h | ingest lag, CPU, RSS | lag < 2 s, < 5% of one core |
| Live tail at 3,000 req/s for 1 h | same | lag < 5 s, under one core |
| Catch-up: 10 M-line backlog | lines/s | > 50,000 lines/s |
| 7 d and 30 d at 300 req/s (bulk-generated straight into DuckDB) | bytes/row, file size; overview, series, top, logs timings | overview < 1 s (7 d); explore series/top < 3 s (30 d); logs page < 200 ms |
| Prune of one day at 300 req/s | duration, effect on ingest lag | no lag spike > 5 s |
| Memory | RSS under all of the above | ≤ `DB_MEMORY_LIMIT` + ~150 MB |

Targets are goals to verify, not promises. If one is missed by a lot, revisit the design before building pages on it.

### Results (2026-10-06, Windows dev box, `DB_THREADS=2`, `DB_MEMORY_LIMIT=512MB`)

| Scenario | Result | Target |
|---|---|---|
| Live tail 300 req/s, 5 min, 2 rotations | worst lag 0.28 s, ~2% of one core, nothing lost | ✓ |
| Live tail 3,000 req/s, 5 min, 2 rotations | 899,951 of 899,951 lines, worst lag 0.27 s, ~11% of one core, 127 MB RSS | ✓ |
| Catch-up, 2 M-line backlog | 42,568 lines/s, 231 MB peak RSS | ✗ (50,000); parse ~109k/s and write ~114k/s run back to back on one thread. Pipelining them (parser thread → queue → writer) would roughly double it; deferred, since a day of backlog at 300 req/s already catches up in ~10 min |
| 7 d at 300 req/s (181 M rows) | 3.11 GB, 17.2 bytes/row | — |
| Overview tiles / series from rollups (24 h, 7 d) | 3–12 ms | ✓ |
| Unique IPs approx / top 10 IPs on a path / top UAs (7 d) | 1.1 s / 1.1 s / 0.6 s | ✓ |
| Explore series with host filter, split by status, raw p95 (7 d) | 2.8 s | ✓ (< 3 s), but p95 dominates: served from the histogram instead |
| p50/p95, raw `approx_quantile` (7 d) | 5.4 s | ✗ → fixed by the rollup histogram (5 ms) |
| Logs page, newest 100 with a filter (7 d) | 11 ms | ✓ |
| Prune one day (26 M rows, 96 chunks) | 3.3 s total | ✓ |
| 30 d at 300 req/s (778 M rows) | not completed: the run was stopped at 300 M rows when the machine ran low on memory (not attributed to the benchmark) | rerun on a quiet host |

Raw-scan query times grow linearly with the range, so 30-day raw queries (unique IPs, top-N on raw fields) should
take ~4–5 s on 2 threads; `DB_THREADS` scales them. The pages should show progress for long ranges.

## Multiple instances (v2)

v1 is built so this is an addition, not a rewrite: every row carries `instance`, ingest takes rows from any source,
and every page treats instance as a filter (an instance picker in the header; "all" is the combined view).

v2 adds remote Traefiks without a second codebase:

- **Ingest endpoint:** `POST /api/ingest` on the central BeaconView, NDJSON body of raw Traefik log lines, gzip
  accepted, `Authorization: Bearer <token>` with one token per instance (`INGEST_TOKENS=name:token,…`); the token
  decides the instance name, so an agent cannot write as another.
- **Agent mode, same image:** `MODE=agent` + `UPSTREAM_URL` + `INGEST_TOKEN` runs the v1 tailer but ships batches
  instead of storing them; no DuckDB, ~30 MB RSS. Each batch carries `(inode, start offset, end offset)`; the server
  stores the last acknowledged offset per instance in the same transaction as the rows, so retries after a timeout
  are dropped as duplicates (exactly once across the network). The agent only advances its position on a 2xx, and
  the log file itself is the buffer while the dashboard is unreachable (bounded by the host's log rotation).
- **Bring your own shipper:** because the endpoint takes raw Traefik JSON lines, Vector or Fluent Bit (HTTP sink,
  NDJSON) can feed it with no BeaconView agent at all; their at-least-once delivery is documented as such.
- **Not planned:** a separate Go/Rust agent. The Python tailer is not the bottleneck (see Load test); one codebase and
  one image is simpler to ship and to trust. Revisit only if agent RSS/CPU on small edge nodes says otherwise.

## Milestones

1. **Ingest + storage:** tailing, rotation, exactly-once positions, schema, rollups, pruning, `/api/status`, tests,
   load test. Deployable on its own so history starts collecting.
2. **Explore + query API:** filters, series, top values, logs paging, live mode, URL state.
3. **Overview:** tiles, comparisons, tables, links into Explore, status strip.
4. **Publish:** backfill, anonymisation, built-in auth, README (sizing table from the load test, screenshots,
   compose example, Traefik config to keep `User-Agent`/`Referer`/`CF-Connecting-IP`), CI, first GHCR release.

### Our own deployment (infra-ops)

`apps/beaconview/docker-compose.yml`: mount `/var/log/traefik` read-only, a `beaconview-data` volume, Traefik
route behind `ADMIN_AUTH` basic auth, Watchtower label. Run alongside `apps/logs` until trusted, then remove
`apps/logs`. Keep `proxy/traefik-logrotate.conf` (raw files become a short safety copy). Optionally keep
`If-None-Match` in Traefik's header config to tell new nopixelv tabs from old ones.

## Open questions

- **Positioning:** "request explorer for Traefik" at launch, "for Traefik, Caddy and nginx" once those parsers ship.

- **Multiple instances:** v2 as above, or sooner if wanted; v1 ships the `instance` column and local multi-file only.

- **Name:** BeaconView. Confirm no conflict on GitHub, Docker Hub, GHCR and a trademark search before publishing
  ("Beacon" is a crowded word; `dunkin6/beacon-viewer` exists on Docker Hub).
- **Cloudflare in front of our Traefik?** If yes, set `CLIENT_IP_HEADER` from day one and keep that header in Traefik's
  access log config.
- **Rollup retention default:** 90 days enough?
- **GeoIP:** country per IP would fit the overview; v1 leaves it out (needs a MaxMind/DB-IP database the user
  supplies). Candidate for v1.1.
