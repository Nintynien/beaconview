"""Country per client IP, from an MMDB database.

GEOIP=auto (the default) downloads DB-IP's free "IP to Country Lite" database (CC BY 4.0: pages showing its results
link "IP Geolocation by DB-IP") into DATA_DIR/geoip and refreshes it monthly; GEOIP=/path/to/file.mmdb uses a database
you supply (MaxMind's GeoLite2-Country works); GEOIP=off turns it off. Lookups happen on this machine: no visitor IP
ever leaves it, the only network traffic is the monthly download.

The download runs on its own thread so ingest never waits for it; a failed download keeps the previous file (or no
countries at all) and is retried every few hours. Lookups go through a cache: real traffic repeats IPs.
"""

import gzip
import logging
import os
import threading
import time
import urllib.request
from datetime import datetime, timezone

import maxminddb

log = logging.getLogger("beaconview.geoip")

DBIP_URL = "https://download.db-ip.com/free/dbip-country-lite-{month}.mmdb.gz"
DBIP_FILE = "dbip-country-lite.mmdb"
MAX_AGE_DAYS = 32          # DB-IP publishes monthly; older than this, fetch the new month
RETRY_SECONDS = 6 * 3600   # after a failed download
CHECK_SECONDS = 24 * 3600  # how often a working install checks whether it is due
UNKNOWN = "ZZ"             # looked up, not found (private ranges, unassigned): ISO 3166's "unknown"
CACHE_MAX = 200_000


class GeoIP:
    def __init__(self, mode="auto", data_dir="/data", fetch=None, clock=time.time, opener=None):
        self.mode = (mode or "auto").strip()
        self.dir = os.path.join(data_dir, "geoip")
        self.fetch = fetch or _fetch
        self.opener = opener or maxminddb.open_database      # tests swap in a fake database
        self.clock = clock
        self.reader = None
        self.source = None          # "db-ip" or "file"
        self.path = None
        self.built = None           # the database's build time (epoch s)
        self.error = None
        self.checked = None
        self.version = 0            # bumps when a new database is opened (stored rows may want refilling)
        self._cache = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    @property
    def enabled(self):
        return self.mode.lower() != "off"

    @property
    def attribution(self):
        """The credit pages must show while DB-IP's data is in use (CC BY 4.0)."""
        return {"text": "IP Geolocation by DB-IP", "url": "https://db-ip.com"} if self.source == "db-ip" else None

    # -- lifecycle -------------------------------------------------------
    def start(self):
        if not self.enabled:
            return
        if self.mode.lower() != "auto":
            self._open(self.mode, "file")          # a supplied file: open it now, nothing to download
            return
        existing = os.path.join(self.dir, DBIP_FILE)
        if os.path.exists(existing):
            self._open(existing, "db-ip")          # usable straight away; the thread refreshes it when due
        self._thread = threading.Thread(target=self._run, name="geoip", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            ok = self.refresh()
            self._stop.wait(CHECK_SECONDS if ok else RETRY_SECONDS)

    def refresh(self):
        """Download this month's DB-IP database if ours is missing or older than MAX_AGE_DAYS. True when a usable
        database is open afterwards."""
        self.checked = self.clock()
        path = os.path.join(self.dir, DBIP_FILE)
        if os.path.exists(path) and self.clock() - os.path.getmtime(path) < MAX_AGE_DAYS * 86400:
            return self.reader is not None or self._open(path, "db-ip")
        os.makedirs(self.dir, exist_ok=True)
        now = datetime.fromtimestamp(self.clock(), tz=timezone.utc)
        months = [now.strftime("%Y-%m"), _previous_month(now)]     # early in a month, the new file may not be out
        for month in months:
            try:
                data = gzip.decompress(self.fetch(DBIP_URL.format(month=month)))
                tmp = path + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(data)
                self.opener(tmp).close()                           # refuse a file that does not open
                os.replace(tmp, path)
                log.info("geoip: downloaded DB-IP country database %s (%d KB)", month, len(data) // 1024)
                return self._open(path, "db-ip")
            except Exception as e:  # noqa: BLE001 - any failure: try the other month, then keep what we have
                self.error = f"download {month}: {type(e).__name__}: {e}"
        log.warning("geoip: %s; retrying in %d h", self.error, RETRY_SECONDS // 3600)
        return self.reader is not None

    def _open(self, path, source):
        try:
            reader = self.opener(path)
        except Exception as e:  # noqa: BLE001
            self.error = f"{path}: {type(e).__name__}: {e}"
            log.warning("geoip: cannot open %s", self.error)
            return False
        with self._lock:
            old, self.reader = self.reader, reader
            self._cache = {}
            self.source, self.path, self.error = source, path, None
            self.built = reader.metadata().build_epoch
            self.version += 1
        if old is not None:
            old.close()
        return True

    # -- lookups ---------------------------------------------------------
    def lookup(self, ip):
        """ISO country code for an IP, UNKNOWN when the database has no answer, None when there is no database."""
        reader = self.reader
        if reader is None or not ip:
            return None
        hit = self._cache.get(ip)
        if hit is not None:
            return hit
        try:
            record = reader.get(ip)
        except ValueError:                         # not an IP address (a junk ClientHost)
            record = None
        code = UNKNOWN
        if isinstance(record, dict):
            country = record.get("country") or record.get("registered_country") or {}
            code = (country.get("iso_code") or UNKNOWN).upper()
        if len(self._cache) >= CACHE_MAX:
            self._cache = {}
        self._cache[ip] = code
        return code

    def status(self):
        return {
            "mode": self.mode, "source": self.source, "path": self.path, "error": self.error,
            "built_ms": int(self.built * 1000) if self.built else None,
            "checked_ms": int(self.checked * 1000) if self.checked else None,
            "attribution": self.attribution,
        }


def _previous_month(now):
    return f"{now.year - 1}-12" if now.month == 1 else f"{now.year}-{now.month - 1:02d}"


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "beaconview (+https://github.com/Nintynien/beaconview)"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()
