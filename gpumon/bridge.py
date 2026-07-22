"""Central MQTT -> Prometheus bridge for a fleet of gpumon nodes.

Subscribes to every node's telemetry, keeps the latest reading per GPU, and
exposes it all on a single ``/metrics`` endpoint that Prometheus scrapes and
Grafana graphs. Also serves ``/fleet`` (JSON snapshot) and ``/alerts``.

Run:  python3 -m gpumon.bridge --mqtt-host BROKER --port 9109
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("gpumon.bridge")

try:
    import paho.mqtt.client as mqtt  # type: ignore
except Exception:  # pragma: no cover
    mqtt = None

# Prometheus gauge name -> telemetry metric field
GPU_METRICS = {
    "gpumon_gpu_temperature_celsius": "temp_c",
    "gpumon_gpu_hotspot_celsius": "hotspot_c",
    "gpumon_gpu_memory_temperature_celsius": "mem_temp_c",
    "gpumon_gpu_power_watts": "power_w",
    "gpumon_gpu_power_limit_watts": "power_limit_w",
    "gpumon_gpu_utilization_percent": "load_pct",
    "gpumon_gpu_memory_utilization_percent": "mem_load_pct",
    "gpumon_gpu_clock_mhz": "clock_mhz",
    "gpumon_gpu_max_clock_mhz": "max_clock_mhz",
    "gpumon_gpu_memory_clock_mhz": "mem_clock_mhz",
    "gpumon_gpu_fan_rpm": "fan_rpm",
    "gpumon_gpu_fan_percent": "fan_pct",
    "gpumon_gpu_voltage_mv": "voltage_mv",
}
HEALTH_STATE = {"ok": 0, "info": 1, "warning": 2, "critical": 3, "unknown": -1}


class FleetState:
    """Thread-safe store of the latest data received from all nodes."""

    def __init__(self, stale_after: float = 30.0):
        self.stale_after = stale_after
        self._lock = threading.Lock()
        self._gpus: dict = {}     # (node, key) -> {"payload":..., "recv":ts}
        self._nodes: dict = {}    # node -> {"status":str, "recv":ts}
        self._alerts: deque = deque(maxlen=500)

    def on_telemetry(self, node: str, key: str, payload: dict) -> None:
        now = time.time()
        with self._lock:
            self._gpus[(node, key)] = {"payload": payload, "recv": now}
            n = self._nodes.setdefault(node, {"status": "online", "recv": now})
            n["recv"] = now

    def on_status(self, node: str, status: str) -> None:
        with self._lock:
            n = self._nodes.setdefault(node, {"status": status, "recv": 0.0})
            n["status"] = status
            n["recv"] = time.time()

    def on_alert(self, alert: dict) -> None:
        with self._lock:
            self._alerts.appendleft(alert)

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            gpus = []
            for (node, key), e in sorted(self._gpus.items()):
                p = e["payload"]
                gpus.append({**p, "node": node, "age_s": round(now - e["recv"], 1),
                             "stale": (now - e["recv"]) > self.stale_after})
            nodes = []
            for node, n in sorted(self._nodes.items()):
                fresh = (now - n["recv"]) <= self.stale_after
                nodes.append({
                    "node": node, "status": n["status"],
                    "age_s": round(now - n["recv"], 1),
                    "up": n["status"] == "online" and fresh,
                })
            alerts = list(self._alerts)
        return {"nodes": nodes, "gpus": gpus, "alerts": alerts}


def _esc(text) -> str:
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def render_prometheus(state: FleetState) -> str:
    snap = state.snapshot()
    lines = []
    emitted = set()

    def emit_type(metric, kind="gauge"):
        if metric not in emitted:
            lines.append(f"# TYPE {metric} {kind}")
            emitted.add(metric)

    for g in snap["gpus"]:
        labels = (f'node="{_esc(g["node"])}",gpu="{_esc(g.get("key"))}",'
                  f'vendor="{_esc(g.get("vendor"))}",name="{_esc(g.get("name"))}"')
        metrics = g.get("metrics", {})
        for metric, field in GPU_METRICS.items():
            val = metrics.get(field)
            if val is None:
                continue
            emit_type(metric)
            lines.append(f"{metric}{{{labels}}} {val}")

        exp = g.get("expected", {})
        if exp.get("clock_mhz") is not None:
            emit_type("gpumon_gpu_expected_clock_mhz")
            lines.append(f"gpumon_gpu_expected_clock_mhz{{{labels}}} {exp['clock_mhz']}")
        if exp.get("power_w") is not None:
            emit_type("gpumon_gpu_expected_power_watts")
            lines.append(f"gpumon_gpu_expected_power_watts{{{labels}}} {exp['power_w']}")

        emit_type("gpumon_gpu_health_state")
        lines.append(f"gpumon_gpu_health_state{{{labels}}} "
                     f"{HEALTH_STATE.get(g.get('health_state', 'unknown'), -1)}")

        emit_type("gpumon_gpu_last_seen_seconds")
        lines.append(f"gpumon_gpu_last_seen_seconds{{{labels}}} {g['age_s']}")

        emit_type("gpumon_gpu_alert_active")
        active = g.get("active_alerts", [])
        seen_codes = set()
        for a in active:
            al = labels + f',code="{_esc(a["code"])}",severity="{_esc(a["severity"])}"'
            lines.append(f"gpumon_gpu_alert_active{{{al}}} 1")
            seen_codes.add(a["code"])

    emit_type("gpumon_node_up")
    for n in snap["nodes"]:
        lines.append(f'gpumon_node_up{{node="{_esc(n["node"])}"}} {1 if n["up"] else 0}')
    emit_type("gpumon_node_last_seen_seconds")
    for n in snap["nodes"]:
        lines.append(f'gpumon_node_last_seen_seconds{{node="{_esc(n["node"])}"}} {n["age_s"]}')

    return "\n".join(lines) + "\n"


def make_handler(state: FleetState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            log.debug("%s", a)

        def _send(self, code, body: bytes, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0].rstrip("/") or "/"
            if path == "/metrics":
                return self._send(200, render_prometheus(state).encode(),
                                  "text/plain; version=0.0.4")
            if path == "/fleet":
                return self._send(200, json.dumps(state.snapshot(),
                                  default=str).encode(), "application/json")
            if path == "/alerts":
                return self._send(200, json.dumps(state.snapshot()["alerts"],
                                  default=str).encode(), "application/json")
            if path == "/":
                return self._send(200, json.dumps(
                    {"endpoints": ["/metrics", "/fleet", "/alerts"]}).encode(),
                    "application/json")
            return self._send(404, b'{"error":"not found"}', "application/json")

    return Handler


class Bridge:
    def __init__(self, mqtt_host, mqtt_port=1883, base="gpumon",
                 stale_after=30.0, username=None, password=None, tls=False):
        if mqtt is None:
            raise RuntimeError("paho-mqtt is required (pip install paho-mqtt)")
        self.base = base.rstrip("/")
        self.state = FleetState(stale_after)

        if hasattr(mqtt, "CallbackAPIVersion"):
            self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                       client_id="gpumon-bridge")
        else:
            self._client = mqtt.Client(client_id="gpumon-bridge")
        if username:
            self._client.username_pw_set(username, password)
        if tls:
            self._client.tls_set()
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.connect_async(mqtt_host, mqtt_port, 60)

    def _on_connect(self, client, userdata, *args):
        client.subscribe(f"{self.base}/#", qos=1)
        log.info("bridge subscribed to %s/#", self.base)

    def _on_message(self, client, userdata, msg):
        parts = msg.topic.split("/")
        if len(parts) < 3 or parts[0] != self.base:
            return
        node, leaf = parts[1], parts[2]
        try:
            if leaf == "status":
                self.state.on_status(node, msg.payload.decode().strip())
                return
            payload = json.loads(msg.payload.decode())
            if leaf == "alerts":
                self.state.on_alert(payload)
            else:
                self.state.on_telemetry(node, leaf, payload)
        except Exception:
            log.exception("failed to handle message on %s", msg.topic)

    def start(self):
        self._client.loop_start()

    def stop(self):
        self._client.loop_stop()
        self._client.disconnect()


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="gpumon.bridge",
        description="Central MQTT->Prometheus bridge for a gpumon fleet.")
    p.add_argument("--mqtt-host", default="127.0.0.1")
    p.add_argument("--mqtt-port", type=int, default=1883)
    p.add_argument("--base", default="gpumon", help="MQTT topic base")
    p.add_argument("--host", default="0.0.0.0", help="HTTP bind host")
    p.add_argument("--port", type=int, default=9109, help="HTTP /metrics port")
    p.add_argument("--stale-after", type=float, default=30.0,
                   help="seconds without data before a node/GPU is 'down'")
    p.add_argument("--username")
    p.add_argument("--password")
    p.add_argument("--tls", action="store_true")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    bridge = Bridge(args.mqtt_host, args.mqtt_port, base=args.base,
                    stale_after=args.stale_after, username=args.username,
                    password=args.password, tls=args.tls)
    bridge.start()

    httpd = ThreadingHTTPServer((args.host, args.port),
                                make_handler(bridge.state))
    log.info("bridge metrics on http://%s:%d/metrics (mqtt %s:%d)",
             args.host, args.port, args.mqtt_host, args.mqtt_port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        bridge.stop()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
