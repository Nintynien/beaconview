"""Finding and reading the rotated siblings of a log for the first-run backfill (BACKFILL=1).

The ingest thread drives it (Ingestor._backfill): oldest file first, progress saved per file in the same transaction
as its rows. A file is known by a hash of its first 64 KB of content, not its name or inode, because logrotate
renames files (access.log.1 -> access.log.2) and compresses them (-> .2.gz, a new inode) between runs; the content
is what stays the same.
"""

import gzip
import hashlib
import os

IDENTITY_BYTES = 64 * 1024
CHUNK = 1 << 20


def rotated_files(path):
    """[(path, mtime)] of the files next to `path` whose names start with its name (access.log.1, access.log.2.gz,
    access.log-20261005, ...), oldest first. The live file itself is left out, by name and by inode."""
    folder = os.path.dirname(path) or "."
    base = os.path.basename(path)
    try:
        live_inode = os.stat(path).st_ino
    except FileNotFoundError:
        live_inode = None
    out = []
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    for name in names:
        if name == base or not name.startswith(base):
            continue
        full = os.path.join(folder, name)
        try:
            st = os.stat(full)
        except OSError:
            continue
        if not os.path.isfile(full) or st.st_ino == live_inode or st.st_size == 0:
            continue
        out.append((full, st.st_mtime))
    out.sort(key=lambda x: (x[1], x[0]))
    return out


def open_bytes(path):
    """The file's uncompressed bytes: gzip is recognised by its magic number, not its name."""
    with open(path, "rb") as f:
        magic = f.read(2)
    return gzip.open(path, "rb") if magic == b"\x1f\x8b" else open(path, "rb")


def identity(path):
    """A hash of the file's first 64 KB of content, or None for an empty or unreadable file."""
    try:
        with open_bytes(path) as f:
            head = f.read(IDENTITY_BYTES)
    except (OSError, EOFError):
        return None
    return hashlib.sha1(head).hexdigest()[:20] if head else None


def read_lines(path, start=0):
    """Yield (lines, end offset) chunks of complete lines from uncompressed offset `start`. The last line of a
    rotated file counts even without a newline: nothing will be appended to it."""
    with open_bytes(path) as f:
        if start:
            f.seek(start)                         # gzip: decompresses and discards up to there
        pos, rest = start, b""
        while True:
            chunk = f.read(CHUNK)
            data = rest + chunk
            if not chunk:
                if data.strip():
                    pos += len(data)
                    yield [_decode(data)], pos
                return
            cut = data.rfind(b"\n")
            if cut < 0:
                rest = data
                continue
            body, rest = data[:cut], data[cut + 1:]
            pos += cut + 1
            lines = [ln for ln in (_decode(x) for x in body.split(b"\n")) if ln]
            yield lines, pos


def _decode(raw):
    line = raw.decode("utf-8", "replace")
    return line[:-1] if line.endswith("\r") else line
