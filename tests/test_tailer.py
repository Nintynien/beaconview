import os

from helpers import append

from beaconview.tailer import Tailer


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_reads_complete_lines_only(tmp_path):
    log = tmp_path / "access.log"
    append(log, "a", "b")
    with open(log, "a") as f:
        f.write("half")
    t = Tailer(str(log))
    assert t.read() == ["a", "b"]
    assert t.read() == []                       # the half line waits
    with open(log, "a") as f:
        f.write("-done\n")
    assert t.read() == ["half-done"]


def test_missing_file_then_appears(tmp_path):
    log = tmp_path / "access.log"
    t = Tailer(str(log))
    assert t.read() == [] and t.inode is None
    append(log, "x")
    assert t.read() == ["x"]


def test_rotation_create(tmp_path):
    """logrotate `create`: rename to .1, new empty file; Traefik keeps writing the old one until its signal."""
    log, clock = tmp_path / "access.log", Clock()
    append(log, "1", "2")
    t = Tailer(str(log), rotate_grace=3.0, clock=clock)
    assert t.read() == ["1", "2"]
    append(log, "3")
    os.rename(log, tmp_path / "access.log.1")
    append(log, "new-1")                        # the new file already has a line
    assert t.read() == ["3"]                    # the old file is finished first
    append(tmp_path / "access.log.1", "4")      # Traefik writes the old file before it reopens
    clock.t += 1
    assert t.read() == ["4"]
    clock.t += 1
    assert t.read() == []                       # inside the grace period: still watching the old file
    clock.t += 3.1
    assert t.read() == ["new-1"]
    assert t.rotations == 1 and t.reading == str(log)


def test_rotation_with_delaycompress_and_gz_siblings(tmp_path):
    log, clock = tmp_path / "access.log", Clock()
    (tmp_path / "access.log.2.gz").write_bytes(b"\x1f\x8b junk")
    append(log, "a")
    t = Tailer(str(log), rotate_grace=1.0, clock=clock)
    assert t.read() == ["a"]
    append(log, "b")
    os.rename(log, tmp_path / "access.log.1")
    append(log, "c")
    assert t.read() == ["b"]
    clock.t += 2
    assert t.read() == ["c"]


def test_copytruncate(tmp_path):
    log = tmp_path / "access.log"
    append(log, "one", "two")
    t = Tailer(str(log))
    assert t.read() == ["one", "two"]
    with open(log, "w") as f:
        f.write("three\n")
    assert t.read() == ["three"]
    assert t.truncations == 1


def test_resume_from_saved_position(tmp_path):
    log = tmp_path / "access.log"
    append(log, "a", "b")
    t = Tailer(str(log))
    t.read()
    inode, pos = t.state()
    append(log, "c")
    t2 = Tailer(str(log), inode=inode, pos=pos)
    assert t2.read() == ["c"]


def test_rotated_while_down(tmp_path):
    """Stopped mid-file, the log rotated meanwhile: finish the old file from the saved offset, then the new one."""
    log, clock = tmp_path / "access.log", Clock()
    append(log, "a", "b")
    t = Tailer(str(log))
    assert t.read() == ["a", "b"]
    inode, pos = t.state()
    append(log, "c")
    os.rename(log, tmp_path / "access.log.1")
    append(log, "d")
    t2 = Tailer(str(log), inode=inode, pos=pos, rotate_grace=1.0, clock=clock)
    assert t2.read() == ["c"]
    clock.t += 2
    assert t2.read() == ["d"]


def test_rotated_away_and_gone(tmp_path):
    log, clock = tmp_path / "access.log", Clock()
    append(log, "a")
    t = Tailer(str(log), rotate_grace=1.0, clock=clock)
    t.read()
    append(log, "unread")
    os.rename(log, tmp_path / "elsewhere")      # not a sibling we can find
    append(log, "new")
    assert t.read() == []
    clock.t += 2
    assert t.read() == ["new"]
    assert t.lost_rotations == 1


def test_crlf_and_blank_lines(tmp_path):
    log = tmp_path / "access.log"
    log.write_bytes(b"a\r\n\nb\n")
    assert Tailer(str(log)).read() == ["a", "b"]


def test_max_bytes_resumes_mid_file(tmp_path):
    log = tmp_path / "access.log"
    append(log, *[f"line-{i}" for i in range(100)])
    t = Tailer(str(log))
    got = []
    while True:
        lines = t.read(max_bytes=64)
        if not lines:
            break
        got += lines
    assert got == [f"line-{i}" for i in range(100)]
