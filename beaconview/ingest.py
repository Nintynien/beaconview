"""The ingest thread: tail every log, parse, batch, write; roll up and prune in between.

Everything that writes to the database happens on this one thread, so writes never conflict with each other, and a
prune or rollup is interleaved with ingest in small steps instead of stalling it.

With BACKFILL=1, a log BeaconView has never tailed first has its rotated siblings read (see backfill.py), oldest
first, before the live tail starts at byte 0 of the current file; together they cover everything still on disk.
"""

import collections
import logging
import os
import threading
import time

from . import backfill
from .events import Hub
from .parsers import make_parser
from .settings import Settings
from .tailer import Tailer

log = logging.getLogger("beaconview.ingest")

RETRY_SECONDS = 5.0
RATE_WINDOW = 60.0                      # seconds the reported write rate covers


class Source:
    """One log: its tailer, its parser, and its counters."""

    def __init__(self, spec, cfg, saved, settings):
        self.instance = spec.instance
        self.path = spec.path
        self.format = spec.format
        self.parser = make_parser(spec.format, settings.client_ip_header, settings.anonymize_ips)
        inode = pos = None
        self.fresh = saved is None          # never tailed before: what the backfill means by "first run"
        if saved and saved[0] == spec.path:
            inode, pos = saved[1], saved[2]
        self.tailer = Tailer(spec.path, inode=inode, pos=pos or 0, rotate_grace=cfg.rotate_grace_seconds)
        self.committed = self.tailer.state()
        self.lines = 0
        self.rows = 0
        self.bad = 0
        self.expired = 0
        self.last_bad = None
        self.newest_ms = None
        self.last_line_at = None
        self.backfill = None                # {"state", "files_done", "files_total", "file", "rows", "error"}

    def status(self):
        t = self.tailer
        return {
            "instance": self.instance, "path": self.path, "format": self.format,
            "reading": t.reading, "inode": t.inode, "pos": t.pos, "unread_bytes": t.unread_bytes(),
            "lines": self.lines, "rows": self.rows, "bad_lines": self.bad, "expired": self.expired,
            "last_bad_line": self.last_bad, "newest_ms": self.newest_ms, "last_line_at": self.last_line_at,
            "rotations": t.rotations, "truncations": t.truncations, "oversized_lines": t.oversized,
            "lost_rotations": t.lost_rotations, "error": t.error, "backfill": self.backfill,
        }


class Ingestor(threading.Thread):
    def __init__(self, cfg, store, clock=time.time, settings=None, hub=None):
        super().__init__(name="ingest", daemon=True)
        self.cfg = cfg
        self.store = store
        self.clock = clock
        self.settings = settings or Settings(cfg)
        self.hub = hub or Hub()                 # open streams wait on this (events.py)
        saved = store.load_positions()
        self.sources = [Source(spec, cfg, saved.get(spec.instance), self.settings) for spec in cfg.sources]
        self.settings.listeners.append(self._settings_changed)
        self.seq = store.max_seq()
        self.stop_event = threading.Event()
        self.batches = 0
        self.last_batch = None          # {"at", "rows", "ms"}
        self.write_errors = 0
        self.last_error = None
        self.rollup_at = 0.0
        self.prune_due = 0.0            # first prune right after startup
        self.pruning = False
        self.last_prune = None          # {"at", "ms"}
        self._recent = collections.deque()   # (time, rows) per batch over the last RATE_WINDOW seconds
        self._recent_lock = threading.Lock()  # appended by this thread, read by status requests

    def stop(self):
        self.stop_event.set()

    def _settings_changed(self, changed):
        """Called from the HTTP thread after a save. Parsers are replaced whole (the ingest thread picks the new one
        up on its next line); a retention change prunes soon rather than at the next hourly round."""
        if changed & {"client_ip_header", "anonymize_ips"}:
            for src in self.sources:
                src.parser = make_parser(src.format, self.settings.client_ip_header, self.settings.anonymize_ips)
        if changed & {"retention_days", "rollup_retention_days"}:
            self.prune_due = 0.0

    # -- the loop --------------------------------------------------------
    def run(self):
        log.info("ingest started: %s", ", ".join(f"{s.instance}={s.path}" for s in self.sources))
        if self.cfg.backfill:
            for src in self.sources:
                self._backfill(src)
        batch, started = [], None
        while not self.stop_event.is_set():
            got = False
            try:
                for src in self.sources:
                    lines = src.tailer.read()
                    if lines:
                        got = True
                        self._parse(src, lines, batch)
                # the clock starts at the first line read (even a bad one: its position needs committing too)
                if got and started is None:
                    started = time.monotonic()
                if started is not None and (len(batch) >= self.cfg.batch_rows
                                            or time.monotonic() - started >= self.cfg.batch_seconds):
                    self._flush(batch)
                    batch, started = [], None
                self._maintenance()
            except Exception as e:  # a write failed: rewind to what is committed and try again shortly
                self.write_errors += 1
                self.last_error = f"{type(e).__name__}: {e}"
                log.exception("ingest error, retrying in %.0f s", RETRY_SECONDS)
                for src in self.sources:
                    src.tailer.reset(*src.committed)
                batch, started = [], None
                self.stop_event.wait(RETRY_SECONDS)
                continue
            if not got:
                self.stop_event.wait(self.cfg.poll_seconds)
        try:
            self._flush(batch)
            self.store.rollup(self._raw_cutoff())
        except Exception:
            log.exception("final flush failed")
        log.info("ingest stopped")

    def _parse(self, src, lines, batch):
        cutoff = self._raw_cutoff()
        parse = src.parser.parse
        newest = src.newest_ms
        for line in lines:
            row = parse(line)
            if row is None:
                src.bad += 1
                src.last_bad = line[:500]
                continue
            if row[0] < cutoff:
                src.expired += 1
                continue
            self.seq += 1
            batch.append((self.seq, src.instance) + row)
            if newest is None or row[0] > newest:
                newest = row[0]
        src.lines += len(lines)
        src.newest_ms = newest
        src.last_line_at = self.clock()

    def _flush(self, batch):
        positions = []
        for src in self.sources:
            state = src.tailer.state()
            if state != src.committed and state[0] is not None:
                positions.append((src.instance, src.path, state[0], state[1]))
        if not batch and not positions:
            return
        t0 = time.monotonic()
        self.store.write_batch(batch, positions)
        took = time.monotonic() - t0
        if batch:
            self.hub.publish_rows(batch[-1][0])
        for src in self.sources:
            src.committed = src.tailer.state()
        counts = {}
        for row in batch:
            counts[row[1]] = counts.get(row[1], 0) + 1
        for src in self.sources:
            src.rows += counts.get(src.instance, 0)
        self.batches += 1
        now = self.clock()
        self.last_batch = {"at": now, "rows": len(batch), "ms": round(took * 1000, 1)}
        self._update_rate(len(batch), now)

    # -- backfill --------------------------------------------------------
    def _backfill(self, src):
        """Read src's rotated files into the database before its live tail starts. Runs for a log never tailed
        before, or one whose backfill was interrupted (its progress is in meta under backfill:<instance>);
        retries a failed write like the live loop does."""
        key = "backfill:" + src.instance
        progress = self.store.get_meta(key)
        if (progress or {}).get("complete") or (progress is None and not src.fresh):
            return
        progress = progress or {"complete": False, "files": {}}
        src.backfill = {"state": "running", "files_done": 0, "files_total": 0, "file": None, "rows": 0, "error": None}
        while not self.stop_event.is_set():
            try:
                self._backfill_pass(src, key, progress)
                if self.stop_event.is_set():
                    return
                progress["complete"] = True
                self.store.write_batch([], [], meta={key: progress})
                src.backfill.update(state="done", file=None)
                log.info("backfill of %s done: %d rows from %d files", src.instance, src.backfill["rows"],
                         src.backfill["files_done"])
                return
            except Exception as e:
                self.write_errors += 1
                src.backfill["error"] = self.last_error = f"{type(e).__name__}: {e}"
                log.exception("backfill error, retrying in %.0f s", RETRY_SECONDS)
                progress.update(self.store.get_meta(key) or {"complete": False, "files": {}})
                self.stop_event.wait(RETRY_SECONDS)

    def _backfill_pass(self, src, key, progress):
        # Loop until no rotated file is left to read: one rotated while we were reading the others counts too,
        # since the live tail starts at the current file and would never see it.
        while not self.stop_event.is_set():
            cutoff_s = self._raw_cutoff() / 1000
            todo = []
            for path, mtime in backfill.rotated_files(src.path):
                if mtime < cutoff_s:
                    continue                # last written before the retention window: nothing in it is kept
                fid = backfill.identity(path)
                if fid and not progress["files"].get(fid, {}).get("done"):
                    todo.append((path, fid))
            if not todo:
                return
            done = sum(1 for f in progress["files"].values() if f.get("done"))
            src.backfill.update(files_done=done, files_total=done + len(todo))
            for path, fid in todo:
                if self.stop_event.is_set():
                    return
                self._backfill_file(src, key, progress, path, fid)
                src.backfill["files_done"] += 1

    def _backfill_file(self, src, key, progress, path, fid):
        entry = progress["files"].setdefault(fid, {"name": os.path.basename(path), "pos": 0, "done": False})
        src.backfill["file"] = os.path.basename(path)
        batch = []
        for lines, pos in backfill.read_lines(path, entry["pos"]):
            self._parse(src, lines, batch)
            if len(batch) >= self.cfg.batch_rows:
                entry["pos"] = pos
                self._backfill_commit(src, key, progress, batch)
                batch = []
                if self.stop_event.is_set():
                    return
            else:
                entry["pos"] = pos
        entry["done"] = True
        self._backfill_commit(src, key, progress, batch)

    def _backfill_commit(self, src, key, progress, batch):
        t0 = time.monotonic()
        self.store.write_batch(batch, [], meta={key: progress})
        if batch:
            self.hub.publish_rows(batch[-1][0])
        src.rows += len(batch)
        src.backfill["rows"] += len(batch)
        src.backfill["error"] = None
        self.batches += 1
        now = self.clock()
        self.last_batch = {"at": now, "rows": len(batch), "ms": round((time.monotonic() - t0) * 1000, 1)}
        self._update_rate(len(batch), now)
        self._maintenance()

    def _update_rate(self, n, now):
        with self._recent_lock:
            self._recent.append((now, n))
            self._trim_rate(now)

    def _trim_rate(self, now):
        while self._recent and self._recent[0][0] < now - RATE_WINDOW:
            self._recent.popleft()

    def rows_per_sec(self):
        """Rows written over the last minute: a catch-up burst drops out a minute later instead of lingering."""
        with self._recent_lock:
            self._trim_rate(self.clock())
            return sum(n for _, n in self._recent) / RATE_WINDOW

    def _maintenance(self):
        now = self.clock()
        if now - self.rollup_at >= self.cfg.rollup_seconds:
            if self.store.rollup(self._raw_cutoff()):
                self.hub.publish_rollup()
            self.rollup_at = now
        if self.pruning or now >= self.prune_due:
            if not self.pruning:
                self.pruning = True
                self._prune_t0 = time.monotonic()
            more = self.store.prune_step(self._raw_cutoff(), self._rollup_cutoff())
            if not more:
                self.store.checkpoint()
                self.pruning = False
                self.prune_due = now + self.cfg.prune_every_seconds
                self.last_prune = {"at": now, "ms": round((time.monotonic() - self._prune_t0) * 1000, 1)}

    def _raw_cutoff(self):
        return int((self.clock() - self.settings.retention_days * 86400) * 1000)

    def _rollup_cutoff(self):
        return int((self.clock() - self.settings.rollup_retention_days * 86400) * 1000)

    # -- status ----------------------------------------------------------
    def status(self):
        newest = [s.newest_ms for s in self.sources if s.newest_ms is not None]
        return {
            "alive": self.is_alive(),
            "rows_per_sec": round(self.rows_per_sec(), 2),
            "batches": self.batches,
            "last_batch": self.last_batch,
            "write_errors": self.write_errors,
            "last_error": self.last_error,
            "newest_ms": max(newest) if newest else None,
            "pruning": self.pruning,
            "last_prune": self.last_prune,
            "sources": [s.status() for s in self.sources],
        }
