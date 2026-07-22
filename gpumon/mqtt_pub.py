"""Node-side MQTT publisher.

Each poll, the monitor hands every GPU's reading to this publisher, which
pushes it to an MQTT broker so a central bridge can aggregate the whole fleet.

Topics (base defaults to ``gpumon``):
  <base>/<node>/status          retained "online"/"offline" (LWT-backed)
  <base>/<node>/<gpu_key>        JSON telemetry, one message per GPU per poll
  <base>/<node>/alerts          JSON alert events as they fire

paho-mqtt is an optional dependency (``pip install paho-mqtt``); importing this
module without it raises only when you actually try to construct a publisher.
"""

from __future__ import annotations

import json
import logging
import socket
from typing import Optional

log = logging.getLogger("gpumon.mqtt")

try:
    import paho.mqtt.client as mqtt  # type: ignore
    _HAVE_PAHO = True
except Exception:  # pragma: no cover
    mqtt = None
    _HAVE_PAHO = False


def _new_client(client_id: str):
    """Construct a paho client that works on both 1.x and 2.x APIs."""
    if hasattr(mqtt, "CallbackAPIVersion"):
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                           client_id=client_id, clean_session=True)
    return mqtt.Client(client_id=client_id, clean_session=True)


class MqttPublisher:
    def __init__(self, host: str, port: int = 1883, node: Optional[str] = None,
                 base: str = "gpumon", username: Optional[str] = None,
                 password: Optional[str] = None, tls: bool = False,
                 qos: int = 0, keepalive: int = 60):
        if not _HAVE_PAHO:
            raise RuntimeError("paho-mqtt is required for MQTT publishing "
                               "(pip install paho-mqtt)")
        self.node = node or socket.gethostname()
        self.base = base.rstrip("/")
        self.qos = qos
        self.status_topic = f"{self.base}/{self.node}/status"

        self._client = _new_client(f"gpumon-{self.node}")
        if username:
            self._client.username_pw_set(username, password)
        if tls:
            self._client.tls_set()
        self._client.will_set(self.status_topic, "offline", qos=1, retain=True)
        self._client.on_connect = self._on_connect
        try:
            self._client.connect_async(host, port, keepalive)
            self._client.loop_start()
            log.info("MQTT publisher for node %r -> %s:%d (base %s)",
                     self.node, host, port, self.base)
        except Exception:
            log.exception("MQTT connect failed")

    def _on_connect(self, client, userdata, *args):
        # signature differs between paho 1.x/2.x; publish retained availability
        client.publish(self.status_topic, "online", qos=1, retain=True)
        log.info("MQTT connected; announced %s online", self.node)

    # -- publishing ----------------------------------------------------
    def publish_telemetry(self, gpu: dict) -> None:
        """gpu is a snapshot entry: sample fields + 'health' + 'spec'."""
        health = gpu.get("health", {})
        payload = {
            "node": self.node,
            "ts": gpu.get("ts"),
            "key": gpu.get("key"),
            "vendor": gpu.get("vendor"),
            "name": gpu.get("name"),
            "model": (gpu.get("spec") or {}).get("model"),
            "metrics": {k: gpu.get(k) for k in _METRIC_FIELDS},
            "expected": {
                "clock_mhz": health.get("baseline", {}).get("expected_clock_mhz"),
                "power_w": health.get("baseline", {}).get("expected_power_w"),
            },
            "health_state": health.get("state", "unknown"),
            "active_alerts": [
                {"code": a["code"], "severity": a["severity"]}
                for a in health.get("active_alerts", [])
            ],
        }
        topic = f"{self.base}/{self.node}/{gpu.get('key')}"
        self._safe_publish(topic, payload)

    def publish_alert(self, alert) -> None:
        payload = {
            "node": self.node, "ts": alert.ts, "key": alert.key,
            "severity": alert.severity, "code": alert.code,
            "message": alert.message, "value": alert.value,
            "expected": alert.expected,
        }
        self._safe_publish(f"{self.base}/{self.node}/alerts", payload)

    def _safe_publish(self, topic: str, payload: dict) -> None:
        try:
            self._client.publish(topic, json.dumps(payload, default=str),
                                 qos=self.qos, retain=False)
        except Exception:
            log.exception("MQTT publish to %s failed", topic)

    def close(self) -> None:
        try:
            self._client.publish(self.status_topic, "offline", qos=1, retain=True)
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass


_METRIC_FIELDS = [
    "temp_c", "hotspot_c", "mem_temp_c", "power_w", "power_limit_w",
    "load_pct", "mem_load_pct", "clock_mhz", "max_clock_mhz", "mem_clock_mhz",
    "fan_rpm", "fan_pct", "voltage_mv",
]
