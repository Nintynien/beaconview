"""Follow one log file across rotations, by (inode, byte offset).

The file is reopened on every read rather than held open, so the same code covers a rotation seen while running
and one that happened while BeaconView was down: if the inode we were reading is no longer at the path, look for
it among the rotated siblings (`access.log.1`, ...), finish it, then start the new file from byte 0.

Positions only ever point just past a complete line, so a half-written last line is read again next time.
"""

import os
import time

CHUNK = 1 << 20          # bytes per read
MAX_LINE = 1 << 20       # a "line" longer than this is skipped (counted as oversized)


class Tailer:
    def __init__(self, path, inode=None, pos=0, rotate_grace=3.0, clock=time.monotonic):
        self.path = path
        self.inode = inode            # None until the file has been seen
        self.pos = pos
        self.rotate_grace = rotate_grace
        self.clock = clock
        self.rotated_since = None     # when we noticed our inode left the path, reset by each read from it
        self.reading = path           # the file the last read came from (the path, or a rotated sibling)
        self.size = 0                 # size of that file at the last read
        self.rotations = 0
        self.truncations = 0
        self.oversized = 0
        self.lost_rotations = 0       # rotated away and gone before we finished it
        self.error = None

    def state(self):
        return self.inode, self.pos

    def reset(self, inode, pos):
        """Rewind to a committed position (after a failed write)."""
        self.inode, self.pos = inode, pos
        self.rotated_since = None

    def unread_bytes(self):
        return max(0, self.size - self.pos)

    def read(self, max_bytes=CHUNK):
        """Complete lines (str, without newline) from the current position, at most about max_bytes of them."""
        try:
            st = _stat(self.path)
            if self.inode is None:
                if st is None:
                    return []
                self.inode, self.pos = st.st_ino, 0
            if st is not None and st.st_ino == self.inode:
                self.rotated_since = None
                if st.st_size < self.pos:          # copytruncate, or the file was replaced in place
                    self.pos = 0
                    self.truncations += 1
                lines = self._read_from(self.path, max_bytes)
                self.error = None
                return lines

            # our inode is no longer at the path: it was rotated (or deleted)
            now = self.clock()
            if self.rotated_since is None:
                self.rotated_since = now
            old = self._find_rotated()
            if old is not None:
                lines = self._read_from(old, max_bytes)
                if lines:
                    self.rotated_since = now       # still being written (Traefik reopens on its signal)
                    return lines
                if now - self.rotated_since < self.rotate_grace:
                    return []
            elif now - self.rotated_since < self.rotate_grace:
                return []                          # the rename may not be visible yet
            else:
                self.lost_rotations += 1 if self.pos else 0
            if st is None:
                return []                          # the new file is not there yet
            self.inode, self.pos = st.st_ino, 0
            self.rotated_since = None
            self.rotations += 1
            lines = self._read_from(self.path, max_bytes)
            self.error = None
            return lines
        except OSError as e:
            self.error = f"{type(e).__name__}: {e}"
            return []

    def _find_rotated(self):
        folder = os.path.dirname(self.path) or "."
        base = os.path.basename(self.path)
        try:
            names = os.listdir(folder)
        except OSError:
            return None
        for name in sorted(names):
            if name == base or not name.startswith(base) or name.endswith(".gz"):
                continue
            candidate = os.path.join(folder, name)
            st = _stat(candidate)
            if st is not None and st.st_ino == self.inode:
                return candidate
        return None

    def _read_from(self, path, max_bytes):
        with open(path, "rb") as f:
            self.size = os.fstat(f.fileno()).st_size
            self.reading = path
            if self.pos >= self.size:
                return []
            f.seek(self.pos)
            data = f.read(max_bytes)
        end = data.rfind(b"\n")
        if end < 0:
            if len(data) >= MAX_LINE:              # no newline in a whole chunk: skip the monster line
                self.pos += len(data)
                self.oversized += 1
            return []
        chunk = data[:end]
        self.pos += end + 1
        lines = chunk.decode("utf-8", "replace").split("\n")
        return [ln[:-1] if ln.endswith("\r") else ln for ln in lines if ln]


def _stat(path):
    try:
        return os.stat(path)
    except FileNotFoundError:
        return None
