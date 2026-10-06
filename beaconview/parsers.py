"""Log line -> BeaconView row.

A parser knows one log format and nothing else; everything after it (storage, rollups, queries, pages) sees only
the row below. Columns a format cannot fill are None. `instance` and `seq` are added by the ingester.
"""

import calendar
import ipaddress
import json

# BeaconView's row, in storage order (after seq and instance)
COLUMNS = (
    "ts_ms", "host", "method", "path", "query", "status", "origin_status", "duration_us", "bytes", "ip",
    "router", "service", "entrypoint", "protocol", "tls", "user_agent", "referer", "headers",
)


def anonymize(ip):
    """Keep the network, drop the host: /24 for IPv4, /48 for IPv6. Anything unparseable is dropped."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    prefix = 24 if addr.version == 4 else 48
    return str(ipaddress.ip_network(f"{addr}/{prefix}", strict=False).network_address)


class TraefikParser:
    """Traefik's JSON access log (`accessLog.format: json`).

    Header fields appear as `request_<Name>` only for headers the Traefik config keeps; User-Agent and Referer get
    their own columns and every other kept request header goes into `headers` (JSON), so it stays queryable.
    """

    name = "traefik"

    def __init__(self, client_ip_header="", anonymize_ips=False):
        self.ip_header = client_ip_header.strip().lower()
        self.anonymize_ips = anonymize_ips
        self._second_cache = {}   # "2026-10-05T12:00:00" -> epoch seconds; lines arrive in bursts per second
        self._anon_cache = {}

    def parse(self, line):
        """One line (str) -> tuple in COLUMNS order, or None when it is not a Traefik access log entry."""
        try:
            d = json.loads(line)
        except ValueError:
            return None
        if not isinstance(d, dict):
            return None
        ts_ms = self._ts(d.get("StartUTC")) or self._ts(d.get("time"))
        if ts_ms is None:
            return None

        raw_path = d.get("RequestPath")
        path, query = raw_path, None
        if isinstance(raw_path, str) and "?" in raw_path:
            path, _, query = raw_path.partition("?")

        user_agent = referer = None
        headers = {}
        ip_from_header = None
        for key, value in d.items():
            if not key.startswith("request_"):
                continue
            name = key[8:]
            lname = name.lower()
            if lname == "user-agent":
                user_agent = value
            elif lname == "referer":
                referer = value
            else:
                headers[name] = value
            if self.ip_header and lname == self.ip_header and isinstance(value, str):
                # X-Forwarded-For style chains: the first address is the original client
                ip_from_header = value.split(",", 1)[0].strip() or None

        ip = ip_from_header or d.get("ClientHost")
        if ip is not None and self.anonymize_ips:
            ip = self._anonymize(ip)

        duration = d.get("Duration")
        return (
            ts_ms,
            d.get("RequestHost"),
            d.get("RequestMethod"),
            path,
            query,
            _int(d.get("DownstreamStatus")),
            _int(d.get("OriginStatus")),
            duration // 1000 if isinstance(duration, int) else None,   # Traefik logs nanoseconds
            _int(d.get("DownstreamContentSize")),
            ip,
            d.get("RouterName") or None,
            d.get("ServiceName") or None,
            d.get("EntryPointName") or None,
            d.get("RequestProtocol") or None,
            d.get("TLSVersion") or None,
            user_agent,
            referer,
            json.dumps(headers, separators=(",", ":")) if headers else None,
        )

    def _ts(self, value):
        """RFC 3339 in UTC ("2026-10-05T12:00:00.123456789Z", any fraction length) -> epoch milliseconds."""
        if not isinstance(value, str) or len(value) < 20 or value[10] != "T":
            return None
        second = value[:19]
        epoch = self._second_cache.get(second)
        if epoch is None:
            try:
                epoch = calendar.timegm((int(value[0:4]), int(value[5:7]), int(value[8:10]),
                                         int(value[11:13]), int(value[14:16]), int(value[17:19])))
            except ValueError:
                return None
            if len(self._second_cache) > 4096:
                self._second_cache.clear()
            self._second_cache[second] = epoch
        ms = 0
        rest = value[19:]
        if rest.startswith("."):
            digits = rest[1:4]
            if digits.isdigit():
                ms = int(digits.ljust(3, "0"))
            rest = rest[1:].lstrip("0123456789")
        if rest not in ("Z", "z", "+00:00"):
            # a local-time field with an offset; rare enough to take the slow path
            from datetime import datetime
            try:
                return int(datetime.fromisoformat(_trim_fraction(value)).timestamp() * 1000)
            except ValueError:
                return None
        return epoch * 1000 + ms

    def _anonymize(self, ip):
        hit = self._anon_cache.get(ip)
        if hit is None:
            if len(self._anon_cache) > 65536:
                self._anon_cache.clear()
            hit = self._anon_cache[ip] = anonymize(ip) or ""
        return hit or None


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _trim_fraction(value):
    """fromisoformat takes at most 6 fractional digits; Traefik writes 9."""
    head, dot, tail = value.partition(".")
    if not dot:
        return value
    digits = len(tail) - len(tail.lstrip("0123456789"))
    return head + "." + tail[:min(digits, 6)] + tail[digits:]


PARSERS = {"traefik": TraefikParser}


def make_parser(fmt, client_ip_header="", anonymize_ips=False):
    return PARSERS[fmt](client_ip_header=client_ip_header, anonymize_ips=anonymize_ips)
