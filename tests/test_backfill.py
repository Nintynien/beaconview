import gzip
import os
import time

from conftest import make_cfg
from helpers import traefik_line, wait_for

import beaconview.ingest as ingest_mod
from beaconview import backfill
from beaconview.ingest import Ingestor
from beaconview.store import Store

NOW = int(time.time() * 1000)


def write(path, tag, n, start_ms, gz=False, mtime_age_s=None):
    """n lines with paths /<tag>/<i>; optionally gzipped and with an mtime that many seconds ago."""
    data = "".join(traefik_line(start_ms + i, RequestPath=f"/{tag}/{i}") + "\n" for i in range(n)).encode()
    if gz:
        with gzip.open(path, "wb") as f:
            f.write(data)
    else:
        with open(path, "wb") as f:
            f.write(data)
    if mtime_age_s is not None:
        t = time.time() - mtime_age_s
        os.utime(path, (t, t))


def day_of_logs(tmp_path):
    """logrotate's layout with delaycompress: .3.gz and .2.gz compressed, .1 plain, the live file."""
    log = tmp_path / "access.log"
    write(tmp_path / "access.log.3.gz", "r3", 30, NOW - 4 * 3_600_000, gz=True, mtime_age_s=3 * 3600)
    write(tmp_path / "access.log.2.gz", "r2", 20, NOW - 3 * 3_600_000, gz=True, mtime_age_s=2 * 3600)
    write(tmp_path / "access.log.1", "r1", 10, NOW - 2 * 3_600_000, mtime_age_s=3600)
    write(log, "live", 5, NOW - 60_000)
    return log


def run_until(cfg, predicate, store=None, timeout=15):
    store = store or Store(cfg.db_path, "256MB", 2)
    ing = Ingestor(cfg, store)
    ing.start()
    ok = wait_for(lambda: predicate(store, ing), timeout)
    ing.stop()
    ing.join(10)
    return store, ing, ok


def paths(store):
    return [r[0] for r in store.reader().execute("SELECT path FROM requests ORDER BY seq").fetchall()]


def done(store, ing):
    s = ing.status()["sources"][0]
    return (s["backfill"] or {}).get("state") == "done" and s["unread_bytes"] == 0 and s["rows"] >= 5


def test_reads_rotated_files_oldest_first_then_the_live_file(tmp_path):
    day_of_logs(tmp_path)
    cfg = make_cfg(tmp_path, BACKFILL=1)
    store, ing, ok = run_until(cfg, lambda st, i: len(paths(st)) == 65)
    assert ok
    got = paths(store)
    tags = [p.split("/")[1] for p in got]
    # oldest file first, each file in order, the live file last: seq follows time
    assert tags == ["r3"] * 30 + ["r2"] * 20 + ["r1"] * 10 + ["live"] * 5
    assert got[:2] == ["/r3/0", "/r3/1"]
    bf = ing.status()["sources"][0]["backfill"]
    assert bf["state"] == "done" and bf["files_done"] == 3 and bf["rows"] == 60
    store.close()

    # a restart reads nothing twice
    store, ing, ok = run_until(cfg, lambda st, i: True)
    assert len(paths(store)) == 65 and ing.status()["sources"][0]["backfill"] is None
    store.close()


def test_off_unless_asked(tmp_path):
    day_of_logs(tmp_path)
    store, ing, ok = run_until(make_cfg(tmp_path), lambda st, i: len(paths(st)) == 5)
    assert ok and {p.split("/")[1] for p in paths(store)} == {"live"}
    store.close()


def test_only_on_first_run(tmp_path):
    day_of_logs(tmp_path)
    store, ing, ok = run_until(make_cfg(tmp_path), lambda st, i: len(paths(st)) == 5)
    store.close()
    # turning BACKFILL on for a log that is already being tailed does not go back in time
    store, ing, ok = run_until(make_cfg(tmp_path, BACKFILL=1), lambda st, i: True)
    assert len(paths(store)) == 5 and ing.status()["sources"][0]["backfill"] is None
    store.close()


def test_resumes_an_interrupted_backfill_without_duplicates(tmp_path, monkeypatch):
    monkeypatch.setattr(backfill, "CHUNK", 1500)        # a couple of lines per read, so it stops inside a file
    day_of_logs(tmp_path)
    cfg = make_cfg(tmp_path, BACKFILL=1, BATCH_ROWS=7)
    store = Store(cfg.db_path, "256MB", 2)
    real = store.write_batch
    calls = {"n": 0}
    ing = Ingestor(cfg, store)

    def stop_after_three(rows, positions, meta=None):
        real(rows, positions, meta)
        calls["n"] += 1
        if calls["n"] == 3:
            ing.stop()                                  # the container stops midway through the first file

    store.write_batch = stop_after_three
    ing.start()
    ing.join(10)
    partial = paths(store)
    assert 0 < len(partial) < 30
    store.close()

    store, ing, ok = run_until(cfg, lambda st, i: len(paths(st)) == 65)
    got = paths(store)
    assert ok and len(got) == len(set(got)) == 65       # every line once
    store.close()


def test_write_failure_is_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest_mod, "RETRY_SECONDS", 0.05)
    day_of_logs(tmp_path)
    cfg = make_cfg(tmp_path, BACKFILL=1, BATCH_ROWS=7)
    store = Store(cfg.db_path, "256MB", 2)
    real = store.write_batch
    calls = {"n": 0}

    def flaky(rows, positions, meta=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk hiccup")
        return real(rows, positions, meta)

    store.write_batch = flaky
    store, ing, ok = run_until(cfg, lambda st, i: len(paths(st)) == 65, store=store)
    got = paths(store)
    assert ok and len(got) == len(set(got)) == 65 and ing.write_errors == 1
    store.close()


def test_a_rotation_during_backfill_is_not_lost(tmp_path):
    log = day_of_logs(tmp_path)
    cfg = make_cfg(tmp_path, BACKFILL=1)
    store = Store(cfg.db_path, "256MB", 2)
    real = store.write_batch
    calls = {"n": 0}

    def rotate_once(rows, positions, meta=None):
        real(rows, positions, meta)
        calls["n"] += 1
        if calls["n"] == 1:
            # while the backfill reads, logrotate moves the live file away and Traefik starts a new one
            os.rename(log, tmp_path / "access.log.0")
            write(log, "after", 3, NOW - 1000)

    store.write_batch = rotate_once
    store, ing, ok = run_until(cfg, lambda st, i: len(paths(st)) == 68, store=store)
    tags = [p.split("/")[1] for p in paths(store)]
    assert ok and tags.count("live") == 5 and tags.count("after") == 3
    store.close()


def test_skips_files_last_written_before_retention(tmp_path):
    log = tmp_path / "access.log"
    write(tmp_path / "access.log.9.gz", "ancient", 10, NOW - 3_600_000, gz=True, mtime_age_s=3 * 86400)
    write(tmp_path / "access.log.1", "r1", 4, NOW - 3_600_000, mtime_age_s=3600)
    write(log, "live", 2, NOW - 1000)
    store, ing, ok = run_until(make_cfg(tmp_path, BACKFILL=1, RETENTION_DAYS=1), lambda st, i: len(paths(st)) == 6)
    assert ok and not any(p.startswith("/ancient/") for p in paths(store))
    store.close()


def test_helpers(tmp_path):
    log = tmp_path / "access.log"
    write(log, "live", 1, NOW)
    write(tmp_path / "access.log.old", "x", 3, NOW, gz=True, mtime_age_s=50)     # gzip without a .gz name
    write(tmp_path / "access.log-20261005", "y", 2, NOW, mtime_age_s=100)
    (tmp_path / "access.log.empty").write_bytes(b"")
    (tmp_path / "other.log").write_bytes(b"{}\n")
    names = [os.path.basename(p) for p, _ in backfill.rotated_files(str(log))]
    assert names == ["access.log-20261005", "access.log.old"]                       # oldest first, no live/empty/other
    lines = [ln for chunk, _ in backfill.read_lines(str(tmp_path / "access.log.old")) for ln in chunk]
    assert len(lines) == 3 and '"/x/2"' in lines[2]
    # the identity survives compression: same content, same id
    plain = tmp_path / "access.log.1"
    write(plain, "z", 5, NOW)
    gz = tmp_path / "access.log.2.gz"
    with open(plain, "rb") as src, gzip.open(gz, "wb") as dst:
        dst.write(src.read())
    assert backfill.identity(str(plain)) == backfill.identity(str(gz)) is not None
    # resume from an offset inside a gzip file
    first_line_len = len(traefik_line(NOW, RequestPath="/z/0")) + 1
    rest = [ln for chunk, _ in backfill.read_lines(str(gz), first_line_len) for ln in chunk]
    assert len(rest) == 4 and '"/z/1"' in rest[0]
