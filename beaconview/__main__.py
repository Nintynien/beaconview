"""python -m beaconview"""

import logging
import signal
import sys
import threading

from . import __version__, config
from .ingest import Ingestor
from .server import App, make_server
from .store import Store

log = logging.getLogger("beaconview")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stdout)
    try:
        cfg = config.load()
    except config.ConfigError as e:
        log.error("configuration: %s", e)
        return 2
    log.info("BeaconView %s: retention %g days raw, %g days rollups, data in %s",
             __version__, cfg.retention_days, cfg.rollup_retention_days, cfg.db_path)

    store = Store(cfg.db_path, cfg.db_memory_limit, cfg.db_threads)
    ingestor = Ingestor(cfg, store)
    ingestor.start()
    server = make_server(App(cfg, store, ingestor), port=cfg.port)

    def shutdown(signum, frame):
        log.info("signal %s: shutting down", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    log.info("listening on :%d", cfg.port)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        ingestor.stop()
        ingestor.join(timeout=30)
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
