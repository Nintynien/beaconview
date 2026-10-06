"""Browser tests: a real BeaconView on a generated log, driven with Playwright's headless Chromium.

Skipped when Playwright is not installed (pip install -r requirements-dev.txt && python -m playwright install
chromium). One server per test session: two hours of traffic written before it starts, and `append(n)` to write
"live" requests while a test watches.
"""

import os
import sys
import threading
import time

import pytest

pytest.importorskip("playwright")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "tools"))

from helpers import make_cfg  # noqa: E402  (tests/helpers.py)
from genlog import Generator  # noqa: E402

from beaconview.ingest import Ingestor  # noqa: E402
from beaconview.server import App, make_server  # noqa: E402
from beaconview.settings import Settings  # noqa: E402
from beaconview.store import Store  # noqa: E402

HISTORY_LINES = 6000


class Site:
    def __init__(self, base, log, app):
        self.base, self.log, self.app = base, log, app
        self.gen = Generator(99)
        self._lock = threading.Lock()

    def append(self, n, failing=False):
        """Write n requests stamped now (spread over the last second); failing=True makes them 502s."""
        now = int(time.time() * 1000)
        lines = []
        for i in range(n):
            line = self.gen.line(now - 999 + i * 999 // max(1, n))
            if failing:
                line = line.replace('"DownstreamStatus":', '"DownstreamStatus":502,"_was":')
            lines.append(line)
        with self._lock, open(self.log, "a", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")

    def rows(self):
        return self.app.store.reader().execute("SELECT count(*) FROM requests").fetchone()[0]


@pytest.fixture(scope="session")
def site(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ui")
    log = tmp / "access.log"
    gen = Generator(7)
    now = int(time.time() * 1000)
    with open(log, "w", encoding="utf-8", newline="\n") as f:
        for i in range(HISTORY_LINES):                       # the last two hours, evenly
            f.write(gen.line(now - 7_200_000 + i * 7_200_000 // HISTORY_LINES) + "\n")
    cfg = make_cfg(tmp, BATCH_SECONDS=0.2)
    store = Store(cfg.db_path, "256MB", 2)
    # ROLLUP_RETENTION_DAYS "set in the environment", so the settings page shows a locked field
    settings = Settings(cfg, {"ROLLUP_RETENTION_DAYS": "90"}, store)
    ing = Ingestor(cfg, store, settings=settings)
    ing.start()
    app = App(cfg, store, ing)
    server = make_server(app, host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    end = time.monotonic() + 30
    while store.reader().execute("SELECT count(*) FROM requests").fetchone()[0] < HISTORY_LINES:
        if time.monotonic() > end:
            raise RuntimeError("the UI test server did not ingest its history")
        time.sleep(0.1)
    time.sleep(0.5)                                          # let the per-minute totals catch up
    host, port = server.server_address
    yield Site(f"http://{host}:{port}", log, app)
    ing.hub.close()
    server.shutdown()
    ing.stop()
    ing.join(10)
    store.close()


@pytest.fixture
def errors(page):
    """Console errors and uncaught exceptions on the page; a test asserts it stays empty."""
    found = []
    page.on("console", lambda m: found.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: found.append(str(e)))
    page.set_default_timeout(10_000)
    return found
