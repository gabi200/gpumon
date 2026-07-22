"""Zero-dependency HTTP API + Prometheus exporter.

Endpoints
---------
GET /                 human-readable index of routes
GET /health           liveness + per-GPU health state
GET /gpus             latest reading for every GPU (JSON)
GET /gpus/<key>       latest reading for one GPU (e.g. /gpus/amd:0)
GET /history?gpu=&limit=   recent samples from the database
GET /alerts?limit=    recent alerts from the database
GET /metrics          Prometheus text exposition format
"""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

log = logging.getLogger("gpumon.api")

# Prometheus gauge name -> sample field
_METRICS = {
    "gpu_temperature_celsius": "temp_c",
    "gpu_hotspot_celsius": "hotspot_c",
    "gpu_power_watts": "power_w",
    "gpu_power_limit_watts": "power_limit_w",
    "gpu_utilization_percent": "load_pct",
    "gpu_clock_mhz": "clock_mhz",
    "gpu_max_clock_mhz": "max_clock_mhz",
    "gpu_mem_clock_mhz": "mem_clock_mhz",
    "gpu_fan_rpm": "fan_rpm",
    "gpu_fan_percent": "fan_pct",
    "gpu_voltage_mv": "voltage_mv",
}
_HEALTH_STATE = {"ok": 0, "info": 1, "warning": 2, "critical": 3}


def make_handler(monitor):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quieter than default
            log.debug("%s - %s", self.address_string(), fmt % args)

        # -- helpers ---------------------------------------------------
        def _send(self, code, body: bytes, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, default=str).encode(), "application/json")

        # -- routing ---------------------------------------------------
        def do_GET(self):
            u = urlparse(self.path)
            path = u.path.rstrip("/") or "/"
            q = parse_qs(u.query)
            try:
                if path == "/":
                    return self._json({"endpoints": [
                        "/health", "/gpus", "/gpus/<key>",
                        "/history?gpu=&limit=", "/alerts?limit=", "/metrics"]})
                if path == "/health":
                    snap = monitor.snapshot()
                    worst = max((_HEALTH_STATE[g["health"]["state"]]
                                 for g in snap["gpus"]), default=0)
                    ok = worst < _HEALTH_STATE["critical"]
                    return self._json({"ok": ok, **snap}, 200 if ok else 503)
                if path == "/gpus":
                    return self._json(monitor.snapshot()["gpus"])
                if path.startswith("/gpus/"):
                    key = path[len("/gpus/"):]
                    for g in monitor.snapshot()["gpus"]:
                        if g["key"] == key:
                            return self._json(g)
                    return self._json({"error": "unknown gpu", "key": key}, 404)
                if path == "/history":
                    key = q.get("gpu", [None])[0]
                    limit = int(q.get("limit", ["500"])[0])
                    return self._json(monitor.history(key, min(limit, 10000)))
                if path == "/alerts":
                    limit = int(q.get("limit", ["100"])[0])
                    return self._json(monitor.alerts(min(limit, 10000)))
                if path == "/metrics":
                    return self._send(200, _prometheus(monitor).encode(),
                                      "text/plain; version=0.0.4")
                return self._json({"error": "not found", "path": path}, 404)
            except Exception as exc:  # never leak a stack trace to the client
                log.exception("request failed: %s", self.path)
                return self._json({"error": str(exc)}, 500)

    return Handler


def _prometheus(monitor) -> str:
    snap = monitor.snapshot()
    lines = []
    emitted = set()
    for metric, field in _METRICS.items():
        for g in snap["gpus"]:
            val = g.get(field)
            if val is None:
                continue
            if metric not in emitted:
                lines.append(f"# TYPE {metric} gauge")
                emitted.add(metric)
            labels = f'gpu="{g["key"]}",vendor="{g["vendor"]}",name="{_esc(g["name"])}"'
            lines.append(f"{metric}{{{labels}}} {val}")
    lines.append("# TYPE gpu_health_state gauge")
    lines.append("# 0=ok 1=info 2=warning 3=critical")
    for g in snap["gpus"]:
        labels = f'gpu="{g["key"]}",vendor="{g["vendor"]}"'
        lines.append(f"gpu_health_state{{{labels}}} "
                     f"{_HEALTH_STATE[g['health']['state']]}")
    return "\n".join(lines) + "\n"


def _esc(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def serve(monitor, host="127.0.0.1", port=8642):
    httpd = ThreadingHTTPServer((host, port), make_handler(monitor))
    log.info("API listening on http://%s:%d", host, port)
    return httpd
