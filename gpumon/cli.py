"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading

from .api import serve
from .health import HealthEngine
from .monitor import Monitor, build_thresholds
from .specs import Enricher
from .storage import Storage


def _load_config(path):
    if not path:
        return {}
    with open(path) as fh:
        return json.load(fh)


def _build_publisher(mqtt_cfg, args):
    host = args.mqtt_host or mqtt_cfg.get("host")
    if not host:
        return None
    try:
        from .mqtt_pub import MqttPublisher
        return MqttPublisher(
            host=host,
            port=args.mqtt_port or mqtt_cfg.get("port", 1883),
            node=args.mqtt_node or mqtt_cfg.get("node"),
            base=mqtt_cfg.get("base", "gpumon"),
            username=mqtt_cfg.get("username"),
            password=mqtt_cfg.get("password"),
            tls=mqtt_cfg.get("tls", False),
            qos=mqtt_cfg.get("qos", 0),
        )
    except Exception as exc:
        # Don't take down local monitoring just because MQTT isn't available.
        logging.getLogger("gpumon").warning(
            "MQTT publishing disabled: %s", exc)
        return None


def _print_once(monitor):
    snap = monitor.snapshot()
    if not snap["gpus"]:
        print("No GPUs detected. Backends:", snap["backends"] or "none")
        return
    for g in snap["gpus"]:
        def fmt(v, unit=""):
            return f"{v:g}{unit}" if isinstance(v, (int, float)) else "n/a"
        spec = g.get("spec", {})
        label = f"[{g['key']}] {g['name']}"
        if spec.get("model"):
            label += f"  ->  {spec['model']}"
            rated = []
            if spec.get("rated_clock_mhz"):
                rated.append(f"{spec['rated_clock_mhz']:g}MHz")
            if spec.get("rated_power_w"):
                rated.append(f"{spec['rated_power_w']:g}W")
            if rated:
                label += f" (rated {'/'.join(rated)}, src={spec.get('source')})"
        print(label)
        print(f"    temp={fmt(g['temp_c'],'C')}  power={fmt(g['power_w'],'W')}"
              f"/{fmt(g['power_limit_w'],'W')}  load={fmt(g['load_pct'],'%')}")
        print(f"    clock={fmt(g['clock_mhz'],'MHz')}/{fmt(g['max_clock_mhz'],'MHz')}"
              f"  mem={fmt(g['mem_clock_mhz'],'MHz')}  fan={fmt(g['fan_rpm'],'rpm')}"
              f"/{fmt(g['fan_pct'],'%')}  volt={fmt(g['voltage_mv'],'mV')}")
        h = g["health"]
        print(f"    health={h['state']}"
              + (f"  alerts={[a['code'] for a in h['active_alerts']]}"
                 if h["active_alerts"] else ""))


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="gpumon",
        description="Cross-vendor (NVIDIA + AMD) GPU monitor with logging, "
                    "HTTP/Prometheus API and early-failure detection.")
    p.add_argument("-c", "--config", help="path to JSON config file")
    p.add_argument("-i", "--interval", type=float, help="poll interval seconds")
    p.add_argument("--db", help="SQLite database path")
    p.add_argument("--host", help="API bind host")
    p.add_argument("--port", type=int, help="API bind port")
    p.add_argument("--no-api", action="store_true", help="disable HTTP API")
    p.add_argument("--once", action="store_true",
                   help="print one reading and exit (no server)")
    p.add_argument("--tui", action="store_true",
                   help="live terminal dashboard (no server)")
    p.add_argument("--specs-file", help="JSON of rated specs keyed by "
                   "'vendor:device' PCI id or model name")
    p.add_argument("--no-enrich", action="store_true",
                   help="disable spec enrichment (pci.ids / dbgpu)")
    p.add_argument("--mqtt-host", help="publish telemetry to this MQTT broker")
    p.add_argument("--mqtt-port", type=int, help="MQTT broker port (default 1883)")
    p.add_argument("--mqtt-node", help="node id used in MQTT topics (default hostname)")
    p.add_argument("--retention-days", type=float, default=30.0)
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    cfg = _load_config(args.config)
    api_cfg = cfg.get("api", {})

    # In TUI mode keep stderr clean so log lines don't corrupt the curses frame.
    log_level = logging.WARNING if args.tui else \
        getattr(logging, args.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    interval = args.interval or cfg.get("interval", 5.0)
    db_path = args.db or cfg.get("db", "gpumon.db")
    host = args.host or api_cfg.get("host", "127.0.0.1")
    port = args.port or api_cfg.get("port", 8642)

    storage = Storage(db_path, retention_days=args.retention_days)
    health = HealthEngine(build_thresholds(cfg.get("thresholds", {})),
                          expected=cfg.get("expected", {}))

    enrich_enabled = not args.no_enrich and cfg.get("enrich", True)
    enricher = None
    if enrich_enabled:
        enricher = Enricher(specs_file=args.specs_file or cfg.get("specs_file"),
                            use_dbgpu=cfg.get("use_dbgpu", True))

    publisher = _build_publisher(cfg.get("mqtt", {}), args)
    monitor = Monitor(storage, health, interval=interval, enricher=enricher,
                      publisher=publisher)

    if args.once:
        monitor.poll_once()
        _print_once(monitor)
        if publisher:
            publisher.close()
        storage.close()
        return 0

    if args.tui:
        from .tui import run as run_tui
        run_tui(monitor, interval=interval)
        if publisher:
            publisher.close()
        storage.close()
        return 0

    monitor.start()

    httpd = None
    if not args.no_api:
        httpd = serve(monitor, host, port)
        threading.Thread(target=httpd.serve_forever, daemon=True,
                         name="gpumon-api").start()

    stop = threading.Event()

    def shutdown(*_):
        logging.getLogger("gpumon").info("shutting down")
        stop.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    print(f"gpumon running (interval={interval}s, db={db_path}"
          + ("" if args.no_api else f", api=http://{host}:{port}") + "). Ctrl-C to stop.")
    stop.wait()

    if httpd:
        httpd.shutdown()
    monitor.stop()
    if publisher:
        publisher.close()
    storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
