"""Live-tail load test: run BeaconView against tools/genlog.py and sample /api/status.

    python tools/livetest.py --rate 300 --seconds 300 --rotate-every 120

Reports, every --every seconds: ingest lag (now minus the newest ingested request), unread bytes, rows/s, CPU used
by the BeaconView process as a share of one core, and resident memory.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def status(port):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=10) as r:
        return json.load(r)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rate", type=float, default=300)
    ap.add_argument("--seconds", type=float, default=300)
    ap.add_argument("--rotate-every", type=float, default=0)
    ap.add_argument("--every", type=float, default=15)
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--dir", default=None, help="working directory (default: a new temp dir)")
    args = ap.parse_args()

    work = args.dir or tempfile.mkdtemp(prefix="beaconview-live-")
    log = os.path.join(work, "access.log")
    open(log, "a").close()
    env = dict(os.environ, ACCESS_LOG=log, INSTANCE_NAME="load", DATA_DIR=os.path.join(work, "data"),
               PORT=str(args.port))
    app = subprocess.Popen([sys.executable, "-m", "beaconview"], cwd=ROOT, env=env,
                           stdout=open(os.path.join(work, "beaconview.log"), "w"), stderr=subprocess.STDOUT)
    try:
        for _ in range(100):
            try:
                status(args.port)
                break
            except OSError:
                time.sleep(0.2)
        gen_cmd = [sys.executable, os.path.join(ROOT, "tools", "genlog.py"), log, "--rate", str(args.rate),
                   "--seconds", str(args.seconds)]
        if args.rotate_every:
            gen_cmd += ["--rotate-every", str(args.rotate_every)]
        gen = subprocess.Popen(gen_cmd, stdout=subprocess.PIPE, text=True)

        print(f"{'t':>5} {'rows':>10} {'rows/s':>8} {'lag s':>6} {'unread':>9} {'cpu %':>6} {'rss MB':>7} {'db MB':>7}")
        t0 = time.monotonic()
        prev = status(args.port)
        prev_t = t0
        worst_lag = 0.0
        samples = []
        while True:
            done = gen.poll() is not None
            time.sleep(args.every if not done else 3)
            s = status(args.port)
            now = time.monotonic()
            cpu = (s["process"]["cpu_s"] - prev["process"]["cpu_s"]) / (now - prev_t) * 100
            newest = s["ingest"]["newest_ms"]
            lag = (s["now_ms"] - newest) / 1000 if newest else float("nan")
            src = s["ingest"]["sources"][0]
            rss = (s["process"]["rss_bytes"] or 0) / 1e6
            if not done:
                worst_lag = max(worst_lag, lag)
                samples.append((cpu, rss))
            print(f"{now - t0:5.0f} {src['rows']:10,} {s['ingest']['rows_per_sec']:8.1f} {lag:6.2f} "
                  f"{src['unread_bytes']:9,} {cpu:6.1f} {rss:7.0f} {s['storage']['db_bytes'] / 1e6:7.1f}",
                  flush=True)
            prev, prev_t = s, now
            if done:
                break
        written = int(gen.stdout.read().split()[1])     # genlog prints "wrote N lines in T s"
        src = s["ingest"]["sources"][0]
        print(f"\nwritten {written:,}, ingested {src['rows']:,}, bad {src['bad_lines']}, "
              f"rotations {src['rotations']}, lost rotations {src['lost_rotations']}, "
              f"write errors {s['ingest']['write_errors']}")
        if samples:
            print(f"worst lag {worst_lag:.2f} s, mean cpu {sum(c for c, _ in samples) / len(samples):.1f}% of a core, "
                  f"peak rss {max(r for _, r in samples):.0f} MB")
    finally:
        app.terminate()
        try:
            app.wait(30)
        except subprocess.TimeoutExpired:
            app.kill()


if __name__ == "__main__":
    main()
