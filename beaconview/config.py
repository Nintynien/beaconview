"""Settings, all from environment variables (see the Configuration table in PLAN.md / README)."""

import os
import socket
from dataclasses import dataclass, field

FORMATS = ("traefik",)


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class LogSource:
    instance: str
    path: str
    format: str = "traefik"


@dataclass(frozen=True)
class Config:
    sources: tuple
    data_dir: str = "/data"
    port: int = 8080
    retention_days: float = 7
    rollup_retention_days: float = 90
    client_ip_header: str = ""
    anonymize_ips: bool = False
    db_memory_limit: str = "512MB"
    db_threads: int = 2
    batch_seconds: float = 1.0
    batch_rows: int = 50_000
    poll_seconds: float = 0.25
    rotate_grace_seconds: float = 3.0
    rollup_seconds: float = 5.0
    prune_every_seconds: float = 3600.0
    basic_auth_user: str = ""
    basic_auth_password: str = field(default="", repr=False)

    @property
    def db_path(self):
        return os.path.join(self.data_dir, "beaconview.duckdb")


def parse_sources(access_logs, access_log, instance_name, log_format):
    """`ACCESS_LOGS=name=path,name=format:path` wins over the single `ACCESS_LOG` + `INSTANCE_NAME`."""
    if log_format not in FORMATS:
        raise ConfigError(f"LOG_FORMAT={log_format!r}: supported formats are {', '.join(FORMATS)}")
    if not access_logs.strip():
        return (LogSource(instance_name, access_log, log_format),)
    out, seen = [], set()
    for item in access_logs.split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, rest = item.partition("=")
        name, rest = name.strip(), rest.strip()
        if not sep or not name or not rest:
            raise ConfigError(f"ACCESS_LOGS entry {item!r}: expected name=path or name=format:path")
        fmt, colon, path = rest.partition(":")
        # a Windows drive ("C:\\logs") or any unknown prefix is part of the path, not a format
        if not colon or fmt not in FORMATS:
            fmt, path = log_format, rest
        if name in seen:
            raise ConfigError(f"ACCESS_LOGS: instance {name!r} appears twice")
        seen.add(name)
        out.append(LogSource(name, path, fmt))
    if not out:
        raise ConfigError("ACCESS_LOGS is set but names no logs")
    return tuple(out)


def _num(env, name, default, kind=float, minimum=None):
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = kind(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not a number") from None
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name}={raw!r} must be at least {minimum}")
    return value


def _flag(env, name):
    return env.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def load(env=None):
    env = os.environ if env is None else env
    sources = parse_sources(
        env.get("ACCESS_LOGS", ""),
        env.get("ACCESS_LOG", "").strip() or "/var/log/traefik/access.log",
        env.get("INSTANCE_NAME", "").strip() or socket.gethostname(),
        env.get("LOG_FORMAT", "").strip() or "traefik",
    )
    user = env.get("BASIC_AUTH_USER", "")
    password = env.get("BASIC_AUTH_PASSWORD", "")
    if bool(user) != bool(password):
        raise ConfigError("set both BASIC_AUTH_USER and BASIC_AUTH_PASSWORD, or neither")
    retention = _num(env, "RETENTION_DAYS", 7.0, minimum=0.01)
    return Config(
        sources=sources,
        data_dir=env.get("DATA_DIR", "").strip() or "/data",
        port=_num(env, "PORT", 8080, int, 1),
        retention_days=retention,
        rollup_retention_days=max(retention, _num(env, "ROLLUP_RETENTION_DAYS", 90.0, minimum=0.01)),
        client_ip_header=env.get("CLIENT_IP_HEADER", "").strip(),
        anonymize_ips=_flag(env, "ANONYMIZE_IPS"),
        db_memory_limit=env.get("DB_MEMORY_LIMIT", "").strip() or "512MB",
        db_threads=_num(env, "DB_THREADS", 2, int, 1),
        batch_seconds=_num(env, "BATCH_SECONDS", 1.0, minimum=0.05),
        batch_rows=_num(env, "BATCH_ROWS", 50_000, int, 1),
        basic_auth_user=user,
        basic_auth_password=password,
    )
