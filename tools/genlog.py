"""Write synthetic Traefik JSON access log lines, for load tests and demos.

    python tools/genlog.py access.log --rate 300 --seconds 600            # live, paced, constant rate
    python tools/genlog.py access.log --backlog 10000000 --hours 24       # as fast as possible, spread over 24 h
    python tools/genlog.py access.log --rate 300 --seconds 600 --rotate-every 120

    # a demo that looks like a real day: 6 h of history, then live for 2 h
    python tools/genlog.py access.log --realistic --rate 20 --history 6 --seconds 7200

--realistic shapes traffic the way real sites behave: a daily curve (quietest around 04:00, busiest around 16:00),
and events drawn per hour from the seed: one client hammering /api/state for 10-30 minutes, quiet gaps, a service
throwing 5xx with slow responses, the occasional outage. A few of each are also placed in the last hours on purpose,
so a demo always shows them. History and live use the same shape.

Traffic is skewed like the real thing: a few hosts, a few hundred paths, a few thousand client IPs with a heavy
head, a status mix dominated by 200/304, log-normal latency.
"""

import argparse
import json
import math
import os
import random
import time
from datetime import datetime, timezone

HOSTS = ["app.example.com", "api.example.com", "www.example.com", "status.example.com", "logs.example.com"]
ROUTERS = ["app@docker", "api@docker", "www@docker", "status@docker", "logs@docker"]
UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148",
    "curl/8.9.1", "python-requests/2.32.3", "Go-http-client/1.1", "Googlebot/2.1 (+http://www.google.com/bot.html)",
] + [f"Mozilla/5.0 (Linux; Android 14; Model-{i}) AppleWebKit/537.36 Chrome/140.0 Mobile Safari/537.36"
     for i in range(40)]
STATUSES = [(200, 0.78), (304, 0.12), (404, 0.05), (301, 0.02), (403, 0.01), (500, 0.01), (502, 0.005), (499, 0.005)]


def build_paths(rng):
    paths = ["/", "/api/state", "/api/summary", "/api/events", "/api/feed", "/health", "/favicon.ico", "/robots.txt"]
    paths += [f"/static/{n}.{ext}" for n in ("app", "common", "style", "vendor") for ext in ("js", "css")]
    paths += [f"/api/character/{rng.getrandbits(48):012x}" for _ in range(150)]
    paths += [f"/posts/{i}" for i in range(80)]
    return paths


class Generator:
    def __init__(self, seed=1):
        self.rng = random.Random(seed)
        self.paths = build_paths(self.rng)
        self.ips = [f"{self.rng.randint(1, 223)}.{self.rng.randint(0, 255)}.{self.rng.randint(0, 255)}."
                    f"{self.rng.randint(1, 254)}" for _ in range(5000)]
        self.statuses, weights = zip(*STATUSES)
        self.cum = []
        acc = 0.0
        for w in weights:
            acc += w
            self.cum.append(acc)

    def skewed(self, items, power=3.0):
        return items[int(len(items) * self.rng.random() ** power)]

    def status(self):
        x = self.rng.random() * self.cum[-1]
        for s, c in zip(self.statuses, self.cum):
            if x <= c:
                return s
        return 200

    def line(self, ms, ip=None, path=None, ua=None, failing_host=None):
        """One log line. The keyword arguments are how --realistic events shape it: a fixed client (ip, path, ua)
        for a hammering script, failing_host for an incident (that host answers 5xx about half the time, slowly)."""
        rng = self.rng
        h = int(len(HOSTS) * rng.random() ** 2)
        if path is None:
            path = self.skewed(self.paths)
            if rng.random() < 0.1:
                path += f"?page={rng.randint(1, 20)}"
        status = self.status()
        duration = int(rng.lognormvariate(15.5, 1.2))          # ns; median ~5 ms
        if failing_host is not None and h == failing_host:
            duration *= 8
            if rng.random() < 0.5:
                status = 502 if rng.random() < 0.7 else 500
        stamp = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
            f"{ms % 1000:03d}{rng.randint(0, 999999):06d}Z"
        ip = ip or self.skewed(self.ips, 4.0)
        ua = ua or self.skewed(UAS, 2.0)
        return json.dumps({
            "ClientAddr": f"{ip}:{rng.randint(1024, 65535)}", "ClientHost": ip,
            "DownstreamContentSize": 0 if status == 304 else rng.randint(200, 60000),
            "DownstreamStatus": status, "Duration": duration, "EntryPointName": "websecure",
            "OriginStatus": status if status < 499 else 0, "RequestHost": HOSTS[h], "RequestMethod":
            "GET" if rng.random() < 0.93 else "POST", "RequestPath": path, "RequestProtocol": "HTTP/2.0",
            "RouterName": ROUTERS[h], "ServiceName": ROUTERS[h], "StartUTC": stamp, "TLSVersion": "1.3",
            "level": "info", "msg": "", "request_User-Agent": ua,
            "request_Referer": f"https://{HOSTS[h]}/" if rng.random() < 0.3 else None,
            "time": stamp[:19] + "Z",
        }, separators=(",", ":"))


class Shape:
    """How busy the site is at a moment, and what is going on: a daily curve times per-hour events.

    Events come from a per-hour random draw (so the same seed gives the same day) plus a "showcase" placed relative
    to when the script started, so a short demo is guaranteed to contain one of each."""

    HAMMER_UA = "python-requests/2.32.3"

    def __init__(self, seed, started_ms, history_hours):
        self.seed = seed
        self.hours = {}
        self.hammer_ips = ["45.83.66.%d" % (10 + i) for i in range(8)]
        m = 60_000
        self.showcase = []
        if history_hours > 0:
            span = history_hours * 3_600_000
            self.showcase = [
                ("quiet", started_ms - int(span * 0.7), started_ms - int(span * 0.7) + 6 * m, None),
                ("outage", started_ms - int(span * 0.45), started_ms - int(span * 0.45) + 4 * m, None),
                ("incident", started_ms - int(span * 0.25), started_ms - int(span * 0.25) + 7 * m, 1),
                # a client that started hammering shortly before now and keeps going into the live part
                ("hammer", started_ms - 12 * m, started_ms + 25 * m, (self.hammer_ips[0], 0.6)),
            ]

    def _hour(self, h):
        ev = self.hours.get(h)
        if ev is None:
            r = random.Random(f"{self.seed}:{h}")
            base = h * 3_600_000
            ev = []
            if r.random() < 0.30:
                a = base + r.randint(0, 50) * 60_000
                ev.append(("hammer", a, a + r.randint(10, 30) * 60_000, (r.choice(self.hammer_ips), r.uniform(0.3, 0.8))))
            if r.random() < 0.12:
                a = base + r.randint(0, 55) * 60_000
                ev.append(("quiet", a, a + r.randint(2, 8) * 60_000, None))
            if r.random() < 0.12:
                a = base + r.randint(0, 55) * 60_000
                ev.append(("incident", a, a + r.randint(3, 8) * 60_000, r.randrange(len(HOSTS))))
            if r.random() < 0.03:
                a = base + r.randint(0, 55) * 60_000
                ev.append(("outage", a, a + r.randint(3, 6) * 60_000, None))
            self.hours[h] = ev
            if len(self.hours) > 500:
                self.hours.clear()
        return ev

    def at(self, ms):
        """(rate multiplier, hammer (ip, share of base rate) or None, failing host index or None)"""
        local = datetime.fromtimestamp(ms / 1000)
        hour = local.hour + local.minute / 60
        mult = 0.25 + 0.75 * (0.5 - 0.5 * math.cos(2 * math.pi * (hour - 4) / 24))
        mult *= 1 + 0.08 * math.sin(ms / 97_000) + 0.05 * math.sin(ms / 23_000)
        hammer = failing = None
        for kind, a, b, arg in self._hour(ms // 3_600_000) + self.showcase:
            if not a <= ms < b:
                continue
            if kind == "quiet":
                mult *= 0.05
            elif kind == "outage":
                mult = 0.0
            elif kind == "incident":
                failing = arg
            elif kind == "hammer":
                hammer = arg
        return max(0.0, mult), hammer, failing


def realistic_lines(gen, shape, rate, ms_from, ms_to, rng):
    """Lines for [ms_from, ms_to), one second at a time, at rate x the shape's multiplier (Poisson-ish)."""
    out = []
    for sec in range(ms_from // 1000, -(-ms_to // 1000)):
        t = sec * 1000
        mult, hammer, failing = shape.at(t)
        n = _count(rate * mult, rng)
        stamps = sorted(t + rng.randrange(1000) for _ in range(n))
        lines = [(ms, gen.line(ms, failing_host=failing)) for ms in stamps if ms_from <= ms < ms_to]
        if hammer and mult > 0:
            ip, share = hammer
            for _ in range(_count(rate * share, rng)):
                ms = t + rng.randrange(1000)
                if ms_from <= ms < ms_to:
                    lines.append((ms, gen.line(ms, ip=ip, path="/api/state", ua=Shape.HAMMER_UA, failing_host=failing)))
        lines.sort(key=lambda x: x[0])
        out.extend(line for _, line in lines)
    return out


def _count(expected, rng):
    whole = int(expected)
    return whole + (1 if rng.random() < expected - whole else 0)


def rotate(path):
    # Windows refuses to rename a file another process has open at that instant (BeaconView reopens the log on
    # every read), so retry briefly
    for _ in range(50):
        try:
            if os.path.exists(path + ".1"):
                os.remove(path + ".1")
            os.rename(path, path + ".1")
            return
        except PermissionError:
            time.sleep(0.01)
    raise RuntimeError("could not rotate " + path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path")
    ap.add_argument("--rate", type=float, default=300, help="lines per second (live mode)")
    ap.add_argument("--seconds", type=float, default=60, help="how long to run (live mode)")
    ap.add_argument("--rotate-every", type=float, default=0, help="rotate like logrotate every N seconds")
    ap.add_argument("--backlog", type=int, default=0, help="write this many lines as fast as possible")
    ap.add_argument("--hours", type=float, default=1, help="backlog lines are spread over the last N hours")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--realistic", action="store_true", help="daily curve, spikes, gaps, incidents (see above)")
    ap.add_argument("--history", type=float, default=0,
                    help="with --realistic: first write this many hours of history, then go live for --seconds")
    args = ap.parse_args()
    gen = Generator(args.seed)

    if args.realistic:
        run_realistic(args, gen)
        return

    if args.backlog:
        end = int(time.time() * 1000)
        start = end - int(args.hours * 3_600_000)
        step = (end - start) / args.backlog
        with open(args.path, "a", encoding="utf-8", newline="\n") as f:
            buf = []
            for i in range(args.backlog):
                buf.append(gen.line(int(start + i * step)))
                if len(buf) >= 10000:
                    f.write("\n".join(buf) + "\n")
                    buf = []
            if buf:
                f.write("\n".join(buf) + "\n")
        print(f"wrote {args.backlog} lines")
        return

    t0 = time.monotonic()
    last_rotate = t0
    written = 0
    f = open(args.path, "a", encoding="utf-8", newline="\n")
    try:
        while True:
            now = time.monotonic()
            if now - t0 >= args.seconds:
                break
            if args.rotate_every and now - last_rotate >= args.rotate_every:
                f.close()
                rotate(args.path)
                f = open(args.path, "a", encoding="utf-8", newline="\n")
                last_rotate = now
            due = int((now - t0) * args.rate) - written
            if due > 0:
                ms = int(time.time() * 1000)
                f.write("".join(gen.line(ms) + "\n" for _ in range(due)))
                f.flush()
                written += due
            time.sleep(0.02)
    finally:
        f.close()
    print(f"wrote {written} lines in {time.monotonic() - t0:.1f} s")


def run_realistic(args, gen):
    rng = random.Random(args.seed + 7)
    started = int(time.time() * 1000)
    shape = Shape(args.seed, started, args.history)
    written = 0
    if args.history > 0:
        start = started - int(args.history * 3_600_000)
        with open(args.path, "a", encoding="utf-8", newline="\n") as f:
            for chunk_start in range(start, started, 600_000):           # ten minutes at a time
                lines = realistic_lines(gen, shape, args.rate, chunk_start, min(started, chunk_start + 600_000), rng)
                if lines:
                    f.write("\n".join(lines) + "\n")
                written += len(lines)
        print(f"wrote {written} lines of history ({args.history:g} h)", flush=True)
    if args.seconds <= 0:
        return
    t0 = time.monotonic()
    last_rotate = t0
    cursor = started
    live = 0
    f = open(args.path, "a", encoding="utf-8", newline="\n")
    try:
        while time.monotonic() - t0 < args.seconds:
            if args.rotate_every and time.monotonic() - last_rotate >= args.rotate_every:
                f.close()
                rotate(args.path)
                f = open(args.path, "a", encoding="utf-8", newline="\n")
                last_rotate = time.monotonic()
            now = int(time.time() * 1000)
            if now > cursor:
                lines = realistic_lines(gen, shape, args.rate, cursor, now, rng)
                cursor = now
                if lines:
                    f.write("\n".join(lines) + "\n")
                    f.flush()
                    live += len(lines)
            time.sleep(0.05)
    finally:
        f.close()
    print(f"wrote {live} live lines in {time.monotonic() - t0:.1f} s")


if __name__ == "__main__":
    main()
