"""The ingest thread: tail every log, parse, batch, write; roll up and prune in between.

Everything that writes to the database happens on this one thread, so writes never conflict with each other, and a
prune or rollup is interleaved with ingest in small steps instead of stalling it.
"""

import logging
import threading
import time

from .parsers import make_parser
from .tailer import Tailer

log = logging.getLogger("beaconview.ingest")

RETRY_SECONDS = 5.0


class Source:
    """One log: its tailer, its parser, and its counters."""

    def __init__(self, spec, cfg, saved):
        self.instance = spec.instance
        self.path = spec.path
        self.format = spec.format
        self.parser = make_parser(spec.format, cfg.client_ip_header, cfg.anonymize_ips)
        inode = pos = None
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

    def status(self):
        t = self.tailer
        return {
            "instance": self.instance, "path": self.path, "format": self.format,
            "reading": t.reading, "inode": t.inode, "pos": t.pos, "unread_bytes": t.unread_bytes(),
            "lines": self.lines, "rows": self.rows, "bad_lines": self.bad, "expired": self.expired,
            "last_bad_line": self.last_bad, "newest_ms": self.newest_ms, "last_line_at": self.last_line_at,
            "rotations": t.rotations, "truncations": t.truncations, "oversized_lines": t.oversized,
            "lost_rotations": t.lost_rotations, "error": t.error,
        }


class Ingestor(threading.Thread):
    def __init__(self, cfg, store, clock=time.time):
        super().__init__(name="ingest", daemon=True)
        self.cfg = cfg
        self.store = store
        self.clock = clock
        saved = store.load_positions()
        self.sources = [Source(spec, cfg, saved.get(spec.instance)) for spec in cfg.sources]
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
        self.rate = 0.0                 # rows/s, smoothed over about a minute
        self._rate_at = None

    def stop(self):
        self.stop_event.set()

    # -- the loop --------------------------------------------------------
    def run(self):
        log.info("ingest started: %s", ", ".join(f"{s.instance}={s.path}" for s in self.sources))
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

    def _update_rate(self, n, now):
        if self._rate_at is None:
            self._rate_at = now
            return
        dt = max(1e-3, now - self._rate_at)
        self._rate_at = now
        alpha = min(1.0, dt / 60.0)
        self.rate += alpha * (n / dt - self.rate)

    def _maintenance(self):
        now = self.clock()
        if now - self.rollup_at >= self.cfg.rollup_seconds:
            self.store.rollup(self._raw_cutoff())
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
        return int((self.clock() - self.cfg.retention_days * 86400) * 1000)

    def _rollup_cutoff(self):
        return int((self.clock() - self.cfg.rollup_retention_days * 86400) * 1000)

    # -- status ----------------------------------------------------------
    def status(self):
        newest = [s.newest_ms for s in self.sources if s.newest_ms is not None]
        return {
            "alive": self.is_alive(),
            "rows_per_sec": round(self.rate, 2),
            "batches": self.batches,
            "last_batch": self.last_batch,
            "write_errors": self.write_errors,
            "last_error": self.last_error,
            "newest_ms": max(newest) if newest else None,
            "pruning": self.pruning,
            "last_prune": self.last_prune,
            "sources": [s.status() for s in self.sources],
        }
