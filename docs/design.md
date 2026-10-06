# BeaconView — design

How BeaconView works and why it is built the way it is: the decisions, the measurements behind them, and what is
planned next. For running it, see the [README](../README.md).

A small, self-hosted request explorer for Traefik. It reads Traefik's JSON access log, keeps a configurable window
of every request in an embedded columnar database, and serves two pages: an overview of the whole proxy and an
explorer for slicing requests by time, host, path, status, IP or user agent (chart on top, log lines below).

Status: v0.1 (milestones 1–3 done, 4 in progress). The name is independent of the Traefik mark, so it also fits if
other proxies (Caddy, nginx JSON logs) are supported later. The README's first line says it reads access logs and
adds no tracking, since "web beacon" also means a tracking pixel.

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
- **Backfill (`BACKFILL=1`, first run only):** before a log's live tail starts, read its rotated siblings
  (`access.log.1`, `access.log.2.gz`, date-stamped names; gzip recognised by content) oldest first by mtime,
  skipping files last written before the retention window and rows older than it. "First run" means the log has no
  saved position (turning BACKFILL on for a log already being tailed does nothing) or its backfill was interrupted.
  Progress is saved per file in the same transaction as its rows, so a stop or crash resumes mid-file without
  duplicates; a file is identified by a hash of its first 64 KB, which survives logrotate renaming and compressing
  it. Files rotated while the backfill runs are picked up before the live tail starts. Measured: 900,000 lines from
  `.3.gz`, `.2.gz`, `.1` in ~16 s (~56,000 lines/s). A banner shows progress ("file 2 of 3").
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
  its rows; every 5 s the dirty minutes are recomputed from `requests` (delete + insert per run of minutes). A
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

No build step and no frontend dependencies: plain HTML, CSS and JS, charts drawn as SVG by `static/chart.js`
(uPlot was the plan; hand-drawn SVG gave exact control of the mark specs for ~350 lines). No third-party requests at
runtime; a strict CSP (`script-src 'self'`, no inline styles). Light/dark theme following the OS, with a toggle.

Chart rules (data-viz method): one y-axis per chart, so response time is its own chart under requests, sharing
the time axis; stacked columns ≤ 24 px with 2 px gaps and a 4 px rounded data end; 2 px lines; legend for 2+
series; hover tooltip listing every series; arrow keys step through buckets; a table view for every chart. Split
colours follow the entity (a key keeps its slot while it stays visible). Status-class colours, validated for both
modes: 2xx blue, 3xx aqua, 4xx yellow, 5xx red (light `#2a78d6 #1baf7a #eda100 #e34948`, dark
`#3987e5 #199e70 #c98500 #d03b3b`).

Look: dark-first, with one high-visibility accent (signal yellow or electric blue) for live data against muted
chrome; logo a minimal lighthouse casting a geometric beam. Status colours stay conventional (2xx/3xx/4xx/5xx)
and must hold up in both themes.

Shared: a **time-range picker** (15m, 1h, 6h, 24h, 7d, 30d, custom start/end, clamped to the retention window), and the
whole query in the URL so any view can be bookmarked or shared. Drag across a chart to zoom the range.

**Which page a feature belongs on:** the Overview tells you, unasked, how the whole proxy is doing (curated, fixed,
compared with the previous period, every value a link into Explore); Explore answers a question you bring (filters,
any split or field, down to single requests, the URL is the question). Needs you to say what you are looking for:
Explore. Worth seeing every time without asking: Overview.

**Ingest health** is not a section of either page: the header dot (green; yellow after 5 minutes without a request,
which may just be a quiet site; red when broken) with details on hover, and a banner on both pages only while
something is wrong (ingest stopped, a log it cannot read, catching up on > 50 MB). The "newest request … ago" text
counts up every second between status checks (every 15 s), measured on the server's clock.

### Overview (`/`)

- **Tiles:** requests, req/s, unique IPs, 5xx rate, 4xx rate, p95, bytes sent — each with the change vs the
  previous period of the same length (rates in percentage points; red/green only where up is bad: error rates, p95).
- **Chart:** requests over time stacked by status class (drag to zoom).
- **Traffic by** router / service / host, sorted busiest, most 5xx or slowest p95 (`/api/breakdown`): req/s,
  requests, 4xx %, 5xx % (each linking to those requests), p50, p95, bytes.
- **Top lists:** status codes, paths, client IPs, user agents, hosts; **recent 5xx** (10 newest).
- Every value links into Explore with that filter and the same range; an instance picker appears when there is
  more than one log.
- **Live** works as on Explore: every preset is live (default range: Last hour), pushed over
  `/api/stream?kind=changes`, at the pace of the chart's bars; what scans raw rows (tiles with unique IPs, top
  paths/IPs/user agents) no more often than every 5 s. Pause / Live as on Explore. Updates land in place, new 5xx
  rows are highlighted, nothing runs while the tab is hidden.

Counts, bytes and percentiles come from `minute_stats` (histograms); unique IPs and raw-field tops from `requests`.

### Explore (`/explore`)

Redesigned 2026-10-06: one switchable panel on top, the matching requests below it, the two linked.

- **Filter bar:** host, router, service, method, path (exact or prefix `/api/*`), status (code or class, lists),
  IP (exact or CIDR), user agent (contains), minimum duration, any kept header. Each filter can be negated
  (`status != 304`). Chips show active filters.
- **Top panel, Timeseries:** one measure over time: requests or bytes (stacked columns, split by status class or
  any field, top 5 + Other), response time (p50/p95/p99 lines, from the rollup histograms when filters allow),
  unique IPs, or error rate (4xx/5xx % lines). Bucket count from the chart width; hover tooltip; drag zooms.
- **Top panel, Breakdown:** one row per value of a field (top 50) with requests, req/s, unique IPs, 4xx %, 5xx %,
  p50, p95 and bytes; click a column header to sort; `=` / `≠` filter to or exclude a value. Grouping by nothing
  gives a single totals row, which is where the old summary tiles went. (The separate top-values panel merged into
  this.)
- **Linked:** hovering a request marks its bucket in the Timeseries (accent band or crosshair) or its group in the
  Breakdown; clicking a bar (or Enter on a focused bucket) jumps the log to that moment ("from 14:05 back", with
  "Back to newest"). From there "Load older" pages down and "Load newer" pages up, keeping the rows being read in
  place; reaching the newest request returns the log to its normal newest-first mode (and Live resumes).
- **Requests (bottom):** newest first with a total match count, keyset-paged ("Load older"). Click a row for every
  field with filter buttons.
- **Live = a preset range.** "Last 5 minutes" … "Last 30 days" are always live; there is no Live toggle. They update
  at the pace of their own bars (`BV.cadence`: one bar's worth, 1 s to 5 min): "Last 5 minutes" (2 s bars) scrolls
  with a new bar every 2 s, "Last hour" (1 min bars) once a minute, longer ranges rarely. The log streams new
  matching requests on every preset (one `/api/stream?kind=logs&changes=1` connection); if you have scrolled down
  to read, your place is kept and an "N new ↑" pill says what arrived above. The newest, still-filling bar is drawn
  lighter and labelled "so far", so it does not read as a drop. **Pause** freezes the window on screen as a fixed
  from/to range (shareable, nothing moves); zooming does the same; **Live** returns to the preset. While the log
  shows an earlier moment (a clicked bar) the chart keeps moving and new rows wait. Falls back to polling if
  streaming fails. (Tried first: a log-only "Follow", which looked broken next to a frozen chart, then a Live
  toggle beside the range, which duplicated what the range already says.)
- URL keeps the view: `view=breakdown`, `measure=`, `split=`, `group=`, `sort=` (old `top=` links map to `group=`).

### Settings (`/settings`)

- **Status:** per log (file being read, state, newest request, lines read, unreadable lines and the last one,
  rotations, backfill progress), database (size, rows, oldest, per-minute totals, last cleanup), process (version,
  uptime, memory, CPU, open live connections). Reached from the nav and by clicking the header's status dot.
- **Editable while running:** request retention, per-minute-total retention, client IP header, IP anonymization
  (`settings.py`). An environment variable, when set, wins and the field is locked ("set by RETENTION_DAYS"); other
  values are saved in `meta` and survive restarts. Lowering a retention first shows what it would delete
  (`/api/settings/preview`) and asks; the prune runs within seconds of saving.
- **Read-only, restart needed:** log paths, backfill, data directory, database limits, port, built-in auth.
- No authentication in the app beyond optional basic auth: the README tells people to put it behind their proxy's
  authentication (Traefik basic auth or forward-auth single sign-on), which does it better than an app can.

## Realtime

Server-Sent Events, not WebSockets or polling: updates only flow server → browser, SSE is plain HTTP through any
proxy, `EventSource` reconnects by itself and resumes with `Last-Event-ID` (the log seq), and it is stdlib on the
server. In-process pub/sub (`events.py`): the ingest thread publishes after each committed batch and each rollup
that changed something; each open stream waits, then queries what is new for it (a slow stream never builds a
backlog). Streams send a heartbeat every 15 s, end after 10 min (the browser reconnects), notice a closed tab within
5 s, and are capped at 50 (then 503 and the page polls). One thread per stream is fine for an internal tool with a
handful of viewers. Per-minute totals rebuild every 2 s; relative ranges end on a whole second; the answer cache
holds 2 s.

## API

All JSON, all bounded by a time range, all results capped.

| Endpoint | Returns |
|---|---|
| `GET /api/breakdown?<filters>&by&sort&limit&unique` | per-group requests, req/s, 4xx/5xx, p50/p95, bytes, unique IPs (`unique=1`); `by=none` for totals; sort by any column |
| `GET /api/summary?<filters>` | requests, req/s, 4xx/5xx, unique IPs, p50/p95, bytes |
| `GET /api/series?<filters>&buckets&measure&split` | time buckets × series; measure = requests, bytes, latency (p50/p95/p99), unique_ips, error_rate |
| `GET /api/top?field&<filters>&limit` | top values with counts and share |
| `GET /api/logs?<filters>&before=<ts:seq>&since=<ts:seq>&after=<seq>&limit` | log lines, newest first: older than `before`, the page just newer than `since`, or ingested after `after`; the first page carries the total match count |
| `GET /api/fields` | filterable fields and their known values (hosts, routers, services) |
| `GET /api/status` | ingest and storage health |
| `GET /api/stream?kind=logs&<filters>` | SSE: new matching requests as they are written (`rows` events, id = seq) |
| `GET /api/stream?kind=changes&min_interval=s` | SSE: a `change` event when the data moved, throttled |
| `GET /api/settings`, `POST /api/settings` | editable settings (with lock state) and the restart-only ones; save as JSON |
| `GET /api/settings/preview?retention_days&rollup_retention_days` | what those retentions would delete now |

**Answer cache:** identical API requests within 5 s share one answer, and concurrent identical requests run the query
once (`AnswerCache`, single flight; follow requests are never cached). Pages end relative ranges on a 5 s boundary
so every tab watching "last hour" asks the identical question: ten tabs cost about what one does.

Filters are parsed into parameterised SQL from a whitelist of fields and operators; user input never becomes SQL text.
URL form, shared by the API and the Explore page (so any view is a link):
`from=<ms>&to=<ms>` (the page also takes `range=15m|1h|6h|24h|7d|30d`), a field repeated to OR its values, `!` to
exclude: `host=a.com`, `path=/api/*`, `path=*state*`, `status=4xx`, `status=!304`, `ip=203.0.113.0/24`,
`ip=2001:db8:*`, `user_agent=bot` (contains, any case), `min_ms=500`, `h.X-Request-Id=abc` (kept headers),
`router=(none)` (missing). Queries whose filters are all rollup dimensions (instance, host, router, service, status)
read `minute_stats`; anything else reads raw rows. Every query runs under a 30 s limit (HTTP 504 past it).
`/api/logs` searches from the newest end in growing windows (15 min ×4 …), so a page from a 30-day range never sorts
30 days of rows.
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
  beaconview/        __main__.py  config.py  settings.py  parsers.py  tailer.py  backfill.py  ingest.py  store.py
                     query.py  events.py  server.py
    static/            overview.*  explore.*  settings.*  common.js  chart.js  app.css  logo.svg
  tests/
  tools/               genlog.py (synthetic Traefik logs, --realistic for demos)  bench.py (query timings)
                       livetest.py (live-tail load test)  smoke.py (checks a running instance; used by CI)
  docs/design.md     this document
  Dockerfile           python:3.13-slim (DuckDB wheels target glibc), runs as uid 10001
  .github/workflows/   build.yml: tests, image smoke test, then multi-arch image to GHCR
                       (main -> :edge, tag vX.Y.Z -> :X.Y.Z, :X.Y, :latest)
  docker-compose.example.yml
  README.md  LICENSE (MIT)
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
- browser tests (`tests/ui`, Playwright + headless Chromium, its own CI job) against a real server on a generated
  log: every page renders in both themes with no console errors and no sideways scroll at phone width; Explore
  filters, hover marking, bar click and "Load newer", breakdown sort and filter, live rows and redraws, Pause, keeping
  your place in the log; Overview tables and links; settings lock, save and the retention confirm

## Load test (part of milestone 1)

`tools/genlog.py` writes realistic lines (a host/path/IP/user-agent mix with skew, status mix, latency
distribution) at a set rate, rotating like logrotate. `--realistic --history H` shapes it like a real day for demos:
a daily curve, per-hour random events (a client hammering `/api/state`, quiet gaps, a service failing with slow 5xx,
rare outages) plus one of each placed in the last hours on purpose. `tools/bench.py` times the API queries.

| Scenario | Measure | Target |
|---|---|---|
| Live tail at 300 req/s for 1 h | ingest lag, CPU, RSS | lag < 2 s, < 5% of one core |
| Live tail at 3,000 req/s for 1 h | same | lag < 5 s, under one core |
| Catch-up: 10 M-line backlog | lines/s | > 50,000 lines/s |
| 7 d and 30 d at 300 req/s (bulk-generated straight into DuckDB) | bytes/row, file size; overview, series, top, logs timings | overview < 1 s (7 d); explore series/top < 3 s (30 d); logs page < 200 ms |
| Prune of one day at 300 req/s | duration, effect on ingest lag | no lag spike > 5 s |
| Memory | RSS under all of the above | ≤ `DB_MEMORY_LIMIT` + ~150 MB |

Targets are goals to verify, not promises. If one is missed by a lot, revisit the design before building pages on it.

### Results (2026-10-06, a desktop machine, `DB_THREADS=2`, `DB_MEMORY_LIMIT=512MB`)

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
2. **Explore + query API:** filters, series, top values, logs paging, live mode, URL state. *Done 2026-10-06:
   48 tests; checked in headless Edge at 1440 px and 390 px, both themes, no console errors or page overflow.*
3. **Overview:** tiles, comparisons, tables, links into Explore, status strip. *Done 2026-10-06: built from the
   existing endpoints plus `/api/breakdown` (no separate `/api/overview`), 49 tests, checked in headless Edge at
   1440 px and 390 px in both themes; the range picker moved into common.js and is shared by both pages.*
4. **Publish:** backfill, anonymisation, built-in auth, settings page, live updates over SSE (*done 2026-10-06*);
   CI with an image smoke test and release tags, README with screenshots (from generated traffic, so no real visitor
   data), sizing table and Traefik config (*done*); first GHCR release (`v0.1.0`).

## Open questions

- **Positioning:** "request explorer for Traefik" at launch, "for Traefik, Caddy and nginx" once those parsers ship.
- **Multiple instances:** v2 as above, or sooner if wanted; v1 ships the `instance` column and local multi-file only.
- **Rollup retention default:** 90 days enough?
- **"Something unusual" on the Overview:** an IP at 10× its usual rate, a 5xx spike, a new top path; the strongest
  candidate for the Overview's "tells you without being asked" role.
- **GeoIP:** country per IP would fit the overview; v1 leaves it out (needs a MaxMind/DB-IP database the user
  supplies). Candidate for v1.1.
