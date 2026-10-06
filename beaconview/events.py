"""In-process pub/sub between the ingest thread and the open streams (/api/stream).

The ingest thread publishes after each committed write ("rows up to seq N exist") and after each rollup that
changed something; every stream waits on the hub and wakes when either moves. There is no message queue: a stream
remembers the last version it acted on and, when woken, asks the database for what is new. A slow or briefly
disconnected stream therefore never falls behind a backlog; it catches up in one query.
"""

import threading


class Hub:
    def __init__(self):
        self._cond = threading.Condition()
        self.version = 0          # bumps on every publish
        self.max_seq = 0          # the newest committed request seq
        self.rollup_version = 0   # bumps when the per-minute totals change
        self.closed = False

    def publish_rows(self, max_seq):
        with self._cond:
            if max_seq > self.max_seq:
                self.max_seq = max_seq
            self.version += 1
            self._cond.notify_all()

    def publish_rollup(self):
        with self._cond:
            self.rollup_version += 1
            self.version += 1
            self._cond.notify_all()

    def wait(self, seen_version, timeout):
        """Block until something is published after `seen_version` (or timeout, or close). Returns the current
        (version, max_seq, rollup_version)."""
        with self._cond:
            if self.version == seen_version and not self.closed:
                self._cond.wait(timeout)
            return self.version, self.max_seq, self.rollup_version

    def snapshot(self):
        with self._cond:
            return self.version, self.max_seq, self.rollup_version

    def close(self):
        """Wake every stream so it can notice the server is stopping."""
        with self._cond:
            self.closed = True
            self._cond.notify_all()
