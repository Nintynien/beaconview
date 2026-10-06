"""Write synthetic Traefik JSON access log lines, for load tests and demos.

    python tools/genlog.py access.log --rate 300 --seconds 600            # live, paced
    python tools/genlog.py access.log --backlog 10000000 --hours 24       # as fast as possible, spread over 24 h
    python tools/genlog.py access.log --rate 300 --seconds 600 --rotate-every 120

Traffic is skewed like the real thing: a few hosts, a few hundred paths, a few thousand client IPs with a heavy
head, a status mix dominated by 200/304, log-normal latency.
"""

import argparse
import json
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

    def line(self, ms):
        rng = self.rng
        h = int(len(HOSTS) * rng.random() ** 2)
        path = self.skewed(self.paths)
        if rng.random() < 0.1:
            path += f"?page={rng.randint(1, 20)}"
        status = self.status()
        duration = int(rng.lognormvariate(15.5, 1.2))          # ns; median ~5 ms
        stamp = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
            f"{ms % 1000:03d}{rng.randint(0, 999999):06d}Z"
        ip = self.skewed(self.ips, 4.0)
        return json.dumps({
            "ClientAddr": f"{ip}:{rng.randint(1024, 65535)}", "ClientHost": ip,
            "DownstreamContentSize": 0 if status == 304 else rng.randint(200, 60000),
            "DownstreamStatus": status, "Duration": duration, "EntryPointName": "websecure",
            "OriginStatus": status if status < 499 else 0, "RequestHost": HOSTS[h], "RequestMethod":
            "GET" if rng.random() < 0.93 else "POST", "RequestPath": path, "RequestProtocol": "HTTP/2.0",
            "RouterName": ROUTERS[h], "ServiceName": ROUTERS[h], "StartUTC": stamp, "TLSVersion": "1.3",
            "level": "info", "msg": "", "request_User-Agent": self.skewed(UAS, 2.0),
            "request_Referer": f"https://{HOSTS[h]}/" if rng.random() < 0.3 else None,
            "time": stamp[:19] + "Z",
        }, separators=(",", ":"))


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
    args = ap.parse_args()
    gen = Generator(args.seed)

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


if __name__ == "__main__":
    main()
