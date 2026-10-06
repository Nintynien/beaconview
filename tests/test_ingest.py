import os
import time

from conftest import make_cfg
from helpers import append, traefik_line, wait_for

from beaconview.ingest import Ingestor
from beaconview.store import Store


def count(store):
    return store.reader().execute("SELECT count(*) FROM requests").fetchone()[0]


def start(cfg):
    store = Store(cfg.db_path, "256MB", 2)
    ing = Ingestor(cfg, store)
    ing.start()
    return store, ing


def stop(store, ing):
    ing.stop()
    ing.join(10)
    store.close()


def test_end_to_end_restart_and_rotation(tmp_path):
    cfg = make_cfg(tmp_path)
    log = tmp_path / "access.log"
    now = int(time.time() * 1000)
    append(log, *[traefik_line(now + i) for i in range(50)], "garbage")

    store, ing = start(cfg)
    assert wait_for(lambda: count(store) == 50)
    src = ing.status()["sources"][0]
    assert src["bad_lines"] == 1 and src["last_bad_line"] == "garbage" and src["rows"] == 50
    assert wait_for(lambda: store.reader().execute("SELECT coalesce(sum(requests), 0) FROM minute_stats").fetchone()[0] == 50)
    stop(store, ing)

    # while stopped: more lines, then a rotation, then lines in the new file
    append(log, *[traefik_line(now + 100 + i) for i in range(10)])
    os.rename(log, tmp_path / "access.log.1")
    append(log, *[traefik_line(now + 200 + i) for i in range(5)])

    store, ing = start(cfg)
    assert wait_for(lambda: count(store) == 65)
    seqs = store.reader().execute("SELECT count(DISTINCT seq), min(seq), max(seq) FROM requests").fetchone()
    assert seqs == (65, 1, 65)                       # no duplicates, no gaps
    assert ing.status()["sources"][0]["rotations"] == 1
    stop(store, ing)


def test_old_lines_are_skipped(tmp_path):
    cfg = make_cfg(tmp_path, RETENTION_DAYS=1)
    log = tmp_path / "access.log"
    now = int(time.time() * 1000)
    append(log, traefik_line(now - 2 * 86_400_000), traefik_line(now))
    store, ing = start(cfg)
    assert wait_for(lambda: count(store) == 1)
    assert ing.status()["sources"][0]["expired"] == 1
    stop(store, ing)


def test_two_instances(tmp_path):
    a, b = tmp_path / "a.log", tmp_path / "b.log"
    cfg = make_cfg(tmp_path, ACCESS_LOGS=f"edge={a},internal=traefik:{b}")
    now = int(time.time() * 1000)
    append(a, traefik_line(now), traefik_line(now + 1))
    append(b, traefik_line(now + 2))
    store, ing = start(cfg)
    assert wait_for(lambda: count(store) == 3)
    got = store.reader().execute("SELECT instance, count(*) FROM requests GROUP BY 1 ORDER BY 1").fetchall()
    assert got == [("edge", 2), ("internal", 1)]
    stop(store, ing)


def test_write_failure_rewinds_and_retries(tmp_path, monkeypatch):
    import beaconview.ingest as ingest_mod
    monkeypatch.setattr(ingest_mod, "RETRY_SECONDS", 0.1)
    cfg = make_cfg(tmp_path)
    log = tmp_path / "access.log"
    append(log, *[traefik_line() for _ in range(5)])
    store = Store(cfg.db_path, "256MB", 2)
    real = store.write_batch
    calls = {"n": 0}

    def flaky(rows, positions):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk hiccup")
        return real(rows, positions)

    store.write_batch = flaky
    ing = Ingestor(cfg, store)
    ing.start()
    assert wait_for(lambda: count(store) == 5)
    assert ing.write_errors == 1 and "disk hiccup" in ing.last_error
    stop(store, ing)


def test_write_rate_covers_the_last_minute(tmp_path):
    cfg = make_cfg(tmp_path)
    store = Store(cfg.db_path, "256MB", 2)
    clock = {"t": 1000.0}
    ing = Ingestor(cfg, store, clock=lambda: clock["t"])
    ing._update_rate(6000, 1000.0)                  # a catch-up burst
    assert ing.rows_per_sec() == 100.0
    clock["t"] = 1030.0
    ing._update_rate(30, 1030.0)
    assert ing.rows_per_sec() == 100.5                # (6000 + 30) / 60
    clock["t"] = 1061.0                             # the burst is over a minute old: gone, not lingering
    assert ing.rows_per_sec() == 0.5
    store.close()
