import json
import time
from datetime import datetime, timezone


def iso(ms):
    """epoch ms -> Traefik's StartUTC (nanosecond fraction, Z)."""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}456789Z"


def traefik_line(ms=None, **over):
    ms = int(time.time() * 1000) if ms is None else ms
    entry = {
        "ClientAddr": "203.0.113.7:51234",
        "ClientHost": "203.0.113.7",
        "ClientPort": "51234",
        "DownstreamContentSize": 512,
        "DownstreamStatus": 200,
        "Duration": 1_234_567,
        "EntryPointName": "websecure",
        "OriginContentSize": 512,
        "OriginDuration": 1_100_000,
        "OriginStatus": 200,
        "Overhead": 134_567,
        "RequestAddr": "example.com",
        "RequestContentSize": 0,
        "RequestCount": 42,
        "RequestHost": "example.com",
        "RequestMethod": "GET",
        "RequestPath": "/api/state?x=1",
        "RequestPort": "-",
        "RequestProtocol": "HTTP/2.0",
        "RequestScheme": "https",
        "RetryAttempts": 0,
        "RouterName": "app@docker",
        "ServiceAddr": "172.18.0.5:8080",
        "ServiceName": "app@docker",
        "ServiceURL": "http://172.18.0.5:8080",
        "StartLocal": iso(ms),
        "StartUTC": iso(ms),
        "TLSCipher": "TLS_AES_128_GCM_SHA256",
        "TLSVersion": "1.3",
        "entryPointName": "websecure",
        "level": "info",
        "msg": "",
        "request_User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Firefox/131.0",
        "request_Referer": "https://example.com/",
        "time": iso(ms)[:19] + "Z",
    }
    for key, value in over.items():
        if value is None:
            entry.pop(key, None)
        else:
            entry[key] = value
    return json.dumps(entry)


def append(path, *lines):
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        for line in lines:
            f.write(line + "\n")


def wait_for(predicate, timeout=10.0, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(step)
    return predicate()
