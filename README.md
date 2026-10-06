# BeaconView

A self-hosted request explorer for Traefik. BeaconView reads the JSON access log Traefik already writes and keeps
a searchable history of every request: who is hitting what, how often, and what they got back. It adds no
tracking to your sites and never sits in the request path: if BeaconView is slow or stopped, your traffic is
unaffected.

> **Status: early development (milestone 3 of 4).** Ingest, storage, retention, the Overview and Explore pages work;
> packaging for a first release is next.
> See [PLAN.md](PLAN.md).

## Running it

```yaml
services:
  beaconview:
    image: ghcr.io/nintynien/beaconview:latest
    environment:
      ACCESS_LOG: /var/log/traefik/access.log
      RETENTION_DAYS: "7"
    volumes:
      - /var/log/traefik:/var/log/traefik:ro
      - beaconview-data:/data
volumes:
  beaconview-data:
```

Traefik needs `accessLog.format: json` and a `filePath`; keep the `User-Agent` and `Referer` headers to see them.
A fuller example is in [docker-compose.example.yml](docker-compose.example.yml).

**BeaconView shows visitor IPs and request paths. Never expose it without authentication** (a Traefik basic-auth
middleware, or `BASIC_AUTH_USER` / `BASIC_AUTH_PASSWORD`).

## Authentication

BeaconView has no user accounts. It shows visitor IPs and request paths, and its settings page can change how long
data is kept, so **put it behind authentication before anyone else can reach it**:

- **Traefik basic auth** (simplest): add a `basicauth` middleware to BeaconView's router, as in
  [docker-compose.example.yml](docker-compose.example.yml).
- **Single sign-on**: forward-auth to [Authelia](https://www.authelia.com/), [Authentik](https://goauthentik.io/) or
  [oauth2-proxy](https://oauth2-proxy.github.io/oauth2-proxy/), the same way you protect your other internal apps.
- **No proxy in front?** Set `BASIC_AUTH_USER` and `BASIC_AUTH_PASSWORD` and BeaconView asks for them itself.

`/healthz` always answers without credentials (it says only whether ingest is running), for container health checks.

## Configuration

| Variable | Default | |
|---|---|---|
| `ACCESS_LOG` | `/var/log/traefik/access.log` | the log to follow |
| `ACCESS_LOGS` | | several logs on one host: `edge=/logs/a.log,internal=/logs/b.log` |
| `INSTANCE_NAME` | hostname | label for `ACCESS_LOG` |
| `LOG_FORMAT` | `traefik` | |
| `RETENTION_DAYS` | `7` | every request is kept this long |
| `ROLLUP_RETENTION_DAYS` | `90` | per-minute totals are kept this long |
| `BACKFILL` | `0` | on first start, also read the rotated logs already on disk (`access.log.1`, `.gz` files) |
| `CLIENT_IP_HEADER` | | e.g. `CF-Connecting-IP` behind Cloudflare (Traefik must keep that header) |
| `ANONYMIZE_IPS` | `0` | store /24 (IPv4) and /48 (IPv6) networks instead of addresses |
| `DB_MEMORY_LIMIT` | `512MB` | |
| `DB_THREADS` | `2` | |
| `BASIC_AUTH_USER` / `BASIC_AUTH_PASSWORD` | | optional built-in authentication |
| `DATA_DIR` | `/data` | |
| `PORT` | `8080` | |

Retention, the client IP header and IP anonymization can also be changed on the **Settings** page while
BeaconView runs. A value set by an environment variable wins and is shown locked there, so your compose file stays
the source of truth for whatever it sets; the rest are saved in BeaconView's database.

`GET /api/status` reports ingest progress, storage size and retention; `GET /healthz` is the unauthenticated
liveness probe.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest
ACCESS_LOG=./access.log DATA_DIR=./data .venv/bin/python -m beaconview
python tools/genlog.py ./access.log --rate 300 --seconds 600    # synthetic traffic, constant rate
python tools/genlog.py ./access.log --realistic --rate 20 --history 6 --seconds 3600   # a demo that looks like a real day
python tools/bench.py ./bench.duckdb --days 7 --rate 300        # storage and query timings
```

## License

MIT
