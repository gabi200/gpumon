"""Polling orchestrator: collect -> store -> evaluate health -> expose."""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from .backends import active_backends
from .health import HealthEngine, Thresholds
from .storage import Storage

log = logging.getLogger("gpumon")


class Monitor:
    def __init__(self, storage: Storage, health: HealthEngine,
                 interval: float = 5.0, baseline_save_s: float = 60.0,
                 prune_s: float = 3600.0, enricher=None, publisher=None):
        self.storage = storage
        self.health = health
        self.interval = interval
        self._baseline_save_s = baseline_save_s
        self._prune_s = prune_s
        self._enricher = enricher
        self._publisher = publisher

        self.backends = active_backends()
        self._latest: dict = {}          # key -> sample dict
        self._specs: dict = {}           # key -> resolved spec dict
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.started_at = time.time()

        self.health.load_baselines(self.storage.load_baselines())
        log.info("Detected %d backend(s): %s", len(self.backends),
                 ", ".join(b.vendor for b in self.backends) or "none")

    # -- lifecycle -----------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="gpumon-poller")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval + 2)

    def poll_once(self) -> list:
        samples = []
        for backend in self.backends:
            try:
                samples.extend(backend.sample_all())
            except Exception:
                log.exception("backend %s failed", backend.vendor)
        for s in samples:
            self._maybe_enrich(s)
            self.storage.insert_sample(s)
            for alert in self.health.evaluate(s):
                self.storage.insert_alert(alert.key, alert.severity, alert.code,
                                          alert.message, alert.value,
                                          alert.expected)
                lvl = {"critical": logging.ERROR, "warning": logging.WARNING}.get(
                    alert.severity, logging.INFO)
                log.log(lvl, "[%s] %s: %s", alert.key, alert.code, alert.message)
                if self._publisher is not None:
                    self._publisher.publish_alert(alert)
            with self._lock:
                self._latest[s.key] = s.as_dict()

        if self._publisher is not None:
            for gpu in self.snapshot()["gpus"]:
                self._publisher.publish_telemetry(gpu)
        return samples

    def _maybe_enrich(self, sample) -> None:
        """Resolve rated specs once per GPU and pin them as expected values."""
        if self._enricher is None or sample.key in self._specs:
            return
        try:
            match = self._enricher.enrich(sample)
        except Exception:
            log.exception("spec enrichment failed for %s", sample.key)
            self._specs[sample.key] = {}
            return
        self.health.pin_expected(sample.key, match.max_clock_mhz, match.power_w)
        self._specs[sample.key] = {
            "model": match.model_name,
            "rated_clock_mhz": match.max_clock_mhz,
            "rated_power_w": match.power_w,
            "source": match.source,
        }

    def _run(self) -> None:
        last_save = last_prune = 0.0
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self.poll_once()
            except Exception:
                log.exception("poll failed")

            if t0 - last_save >= self._baseline_save_s:
                self._persist_baselines()
                last_save = t0
            if t0 - last_prune >= self._prune_s:
                try:
                    self.storage.prune()
                except Exception:
                    log.exception("prune failed")
                last_prune = t0

            self._stop.wait(max(0.0, self.interval - (time.time() - t0)))
        self._persist_baselines()

    def _persist_baselines(self) -> None:
        for key, b in self.health.baselines.items():
            try:
                self.storage.save_baseline(key, b.max_clock_mhz, b.max_power_w,
                                           b.samples_seen)
            except Exception:
                log.exception("baseline save failed for %s", key)

    # -- read API used by the HTTP server ------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            latest = dict(self._latest)
        gpus = []
        for key, sample in sorted(latest.items()):
            gpus.append({**sample, "health": self.health.status(key),
                         "spec": self._specs.get(key, {})})
        return {
            "service": "gpumon",
            "uptime_s": round(time.time() - self.started_at, 1),
            "backends": [b.vendor for b in self.backends],
            "gpu_count": len(gpus),
            "gpus": gpus,
        }

    def history(self, key=None, limit=500) -> list:
        return self.storage.history(key, limit)

    def alerts(self, limit=100) -> list:
        return self.storage.recent_alerts(limit)


def build_thresholds(cfg: dict) -> Thresholds:
    t = Thresholds()
    for field_name, value in (cfg or {}).items():
        if hasattr(t, field_name):
            setattr(t, field_name, value)
    return t
