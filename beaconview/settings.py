"""The settings that can change while BeaconView runs, editable on the settings page.

Precedence: an environment variable, when set, wins and the field is locked on the page (the compose file stays
the source of truth for what it sets); otherwise a value saved from the page (meta key "settings"); otherwise the
default. Everything else in config.py (log paths, database limits, port, auth) needs a restart and is shown
read-only.
"""

import threading

from .config import Config

# name -> (environment variable, kind, label, note)
EDITABLE = {
    "retention_days": ("RETENTION_DAYS", "number", "Keep every request for (days)",
                       "Lowering it deletes older requests at the next prune, a few seconds after saving."),
    "rollup_retention_days": ("ROLLUP_RETENTION_DAYS", "number", "Keep per-minute totals for (days)",
                              "Totals feed the long-range charts; never shorter than the request retention."),
    "client_ip_header": ("CLIENT_IP_HEADER", "text", "Client IP header",
                         "Behind Cloudflare or another proxy, e.g. CF-Connecting-IP (Traefik must keep the header). "
                         "Applies to new requests only."),
    "anonymize_ips": ("ANONYMIZE_IPS", "bool", "Anonymize IPs",
                      "Store /24 (IPv4) and /48 (IPv6) networks instead of addresses. Applies to new requests only."),
}
META_KEY = "settings"
MIN_DAYS = 0.01


class SettingsError(ValueError):
    pass


def _default(name):
    return Config.__dataclass_fields__[name].default


def _clean(name, value):
    kind = EDITABLE[name][1]
    if kind == "number":
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise SettingsError(f"{EDITABLE[name][2]}: {value!r} is not a number") from None
        if v < MIN_DAYS:
            raise SettingsError(f"{EDITABLE[name][2]}: must be at least {MIN_DAYS}")
        return v
    if kind == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    return str(value or "").strip()


class Settings:
    def __init__(self, cfg, env=None, store=None):
        env = env or {}
        self.store = store
        self.locked = {k: spec[0] for k, spec in EDITABLE.items() if str(env.get(spec[0], "")).strip()}
        self.values = {k: getattr(cfg, k) for k in EDITABLE}
        saved = (store.get_meta(META_KEY) if store is not None else None) or {}
        for k, v in saved.items():
            if k in EDITABLE and k not in self.locked:
                try:
                    self.values[k] = _clean(k, v)
                except SettingsError:
                    pass                    # a bad saved value: keep the default rather than refuse to start
        self.values["rollup_retention_days"] = max(self.values["rollup_retention_days"], self.values["retention_days"])
        self.listeners = []                 # called with the set of changed names, after a save
        self._lock = threading.Lock()

    def __getattr__(self, name):
        values = self.__dict__.get("values")
        if values is not None and name in values:
            return values[name]
        raise AttributeError(name)

    def describe(self):
        return [{
            "name": k, "label": spec[2], "kind": spec[1], "note": spec[3], "value": self.values[k],
            "default": _default(k), "env": spec[0], "locked": k in self.locked,
        } for k, spec in EDITABLE.items()]

    def update(self, changes):
        """Apply {name: value} from the page. Locked or unknown names, and bad values, raise SettingsError and
        change nothing. Returns the set of names that changed."""
        with self._lock:
            new = dict(self.values)
            for k, v in changes.items():
                if k not in EDITABLE:
                    raise SettingsError(f"{k}: not a setting that can be changed here")
                if k in self.locked:
                    raise SettingsError(f"{EDITABLE[k][2]} is set by {self.locked[k]} in the environment")
                new[k] = _clean(k, v)
            if new["rollup_retention_days"] < new["retention_days"]:
                if "rollup_retention_days" in changes:
                    raise SettingsError("per-minute totals cannot be kept for less time than every request")
                new["rollup_retention_days"] = new["retention_days"]
            changed = {k for k in EDITABLE if new[k] != self.values[k]}
            if not changed:
                return changed
            if self.store is not None:
                self.store.save_meta(META_KEY, {k: new[k] for k in EDITABLE
                                                if k not in self.locked and new[k] != _default(k)})
            self.values = new
        for listener in self.listeners:
            listener(changed)
        return changed
