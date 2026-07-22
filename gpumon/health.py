"""Failure / degradation detection.

Strategy
--------
For each GPU we keep a learned baseline of the best clock and power ever
observed while the card was under real load. Early hardware failure shows up
as the card no longer being able to reach that baseline:

* clock shortfall under load .. thermal throttling, degraded silicon, a stuck
  low power state, or a failing VRM
* power shortfall under load ... the core can no longer draw what it used to
* fan stopped while hot ......... a seized / failed fan
* over-temperature ............. cooling loop problem (pump, paste, dust)

Baselines can also be pinned in config ("expected") when you already know the
card's rated numbers, which lets detection work from the very first sample
instead of waiting to learn.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from .backends import GPUSample

# severity ranking for display / filtering
SEVERITY = {"info": 0, "warning": 1, "critical": 2}


@dataclass
class Thresholds:
    temp_warn_c: float = 85.0
    temp_crit_c: float = 95.0
    hotspot_crit_c: float = 105.0
    fan_zero_temp_c: float = 65.0      # a hot card with 0 fan is a failure
    load_active_pct: float = 80.0      # only judge clock/power when busy
    clock_shortfall_pct: float = 25.0  # >this% below baseline -> alert
    power_shortfall_pct: float = 30.0
    min_learn_samples: int = 30        # loaded samples before trusting baseline
    alert_repeat_s: float = 300.0      # re-fire a still-active alert this often


@dataclass
class Baseline:
    max_clock_mhz: float = 0.0
    max_power_w: float = 0.0
    samples_seen: int = 0
    # pinned expectations from config (never overwritten by learning)
    pinned_clock: Optional[float] = None
    pinned_power: Optional[float] = None

    def expected_clock(self) -> Optional[float]:
        if self.pinned_clock:
            return self.pinned_clock
        return self.max_clock_mhz or None

    def expected_power(self) -> Optional[float]:
        if self.pinned_power:
            return self.pinned_power
        return self.max_power_w or None


@dataclass
class Alert:
    ts: float
    key: str
    severity: str
    code: str
    message: str
    value: Optional[float] = None
    expected: Optional[float] = None

    def as_dict(self) -> dict:
        return {
            "ts": self.ts, "key": self.key, "severity": self.severity,
            "code": self.code, "message": self.message,
            "value": self.value, "expected": self.expected,
        }


class HealthEngine:
    def __init__(self, thresholds: Optional[Thresholds] = None,
                 expected: Optional[dict] = None):
        self.t = thresholds or Thresholds()
        self.baselines: dict = {}
        self._last_fired: dict = {}   # (key, code) -> ts
        self.active: dict = {}        # (key, code) -> Alert
        self._expected_cfg = expected or {}

    # -- baseline lifecycle -------------------------------------------
    def load_baselines(self, stored: dict) -> None:
        for key, row in stored.items():
            b = self._baseline(key)
            b.max_clock_mhz = row.get("max_clock_mhz") or 0.0
            b.max_power_w = row.get("max_power_w") or 0.0
            b.samples_seen = row.get("samples_seen") or 0

    def pin_expected(self, key: str, clock=None, power=None) -> None:
        """Pin rated clock/power from an external source (e.g. spec DB).

        Never overrides values already pinned by config, and only fills a slot
        that is currently empty — so precedence stays: config > spec > learned.
        """
        b = self._baseline(key)
        if clock and b.pinned_clock is None:
            b.pinned_clock = float(clock)
        if power and b.pinned_power is None:
            b.pinned_power = float(power)

    def _baseline(self, key: str) -> Baseline:
        b = self.baselines.get(key)
        if b is None:
            b = Baseline()
            cfg = self._expected_cfg.get(key) or self._expected_cfg.get(str(key))
            if cfg:
                b.pinned_clock = cfg.get("max_clock_mhz")
                b.pinned_power = cfg.get("power_w")
            self.baselines[key] = b
        return b

    def _learn(self, s: GPUSample) -> Baseline:
        b = self._baseline(s.key)
        # A card advertises its own max clock (nvidia clocks.max, amd pp_dpm);
        # trust it immediately as an upper reference.
        if s.max_clock_mhz and s.max_clock_mhz > b.max_clock_mhz:
            b.max_clock_mhz = s.max_clock_mhz
        # Learn the real achievable clock/power only from loaded samples.
        if s.load_pct is not None and s.load_pct >= self.t.load_active_pct:
            b.samples_seen += 1
            if s.clock_mhz and s.clock_mhz > b.max_clock_mhz:
                b.max_clock_mhz = s.clock_mhz
            if s.power_w and s.power_w > b.max_power_w:
                b.max_power_w = s.power_w
        return b

    # -- evaluation ----------------------------------------------------
    def evaluate(self, s: GPUSample) -> list:
        """Update baselines and return the list of alerts fired for this sample."""
        b = self._learn(s)
        candidates = []
        t = self.t

        # 1) over-temperature
        if s.temp_c is not None:
            if s.temp_c >= t.temp_crit_c:
                candidates.append(("overtemp", "critical",
                    f"Temperature {s.temp_c:.0f}C >= critical {t.temp_crit_c:.0f}C",
                    s.temp_c, t.temp_crit_c))
            elif s.temp_c >= t.temp_warn_c:
                candidates.append(("overtemp", "warning",
                    f"Temperature {s.temp_c:.0f}C >= warn {t.temp_warn_c:.0f}C",
                    s.temp_c, t.temp_warn_c))
        if s.hotspot_c is not None and s.hotspot_c >= t.hotspot_crit_c:
            candidates.append(("hotspot", "critical",
                f"Hotspot {s.hotspot_c:.0f}C >= {t.hotspot_crit_c:.0f}C",
                s.hotspot_c, t.hotspot_crit_c))

        # 2) fan failure — only meaningful if the card reports a fan at all
        has_fan = s.fan_rpm is not None or s.fan_pct is not None
        fan_stopped = (s.fan_rpm == 0) or (s.fan_rpm is None and s.fan_pct == 0)
        if has_fan and fan_stopped and s.temp_c is not None \
                and s.temp_c >= t.fan_zero_temp_c:
            candidates.append(("fan_stopped", "critical",
                f"Fan not spinning while GPU at {s.temp_c:.0f}C — possible fan failure",
                0.0, None))

        # 3) clock / power shortfall under load (degradation signal)
        loaded = s.load_pct is not None and s.load_pct >= t.load_active_pct
        trusted = b.samples_seen >= t.min_learn_samples or b.pinned_clock \
            or b.pinned_power
        if loaded and trusted:
            exp_clock = b.expected_clock()
            if exp_clock and s.clock_mhz is not None:
                floor = exp_clock * (1 - t.clock_shortfall_pct / 100.0)
                if s.clock_mhz < floor:
                    candidates.append(("clock_shortfall", "warning",
                        f"Core clock {s.clock_mhz:.0f}MHz under load is "
                        f"{100*(1-s.clock_mhz/exp_clock):.0f}% below expected "
                        f"{exp_clock:.0f}MHz — throttling or degradation",
                        s.clock_mhz, exp_clock))

            exp_power = b.expected_power()
            if exp_power and s.power_w is not None:
                floor = exp_power * (1 - t.power_shortfall_pct / 100.0)
                if s.power_w < floor:
                    candidates.append(("power_shortfall", "warning",
                        f"Board power {s.power_w:.0f}W under load is "
                        f"{100*(1-s.power_w/exp_power):.0f}% below expected "
                        f"{exp_power:.0f}W",
                        s.power_w, exp_power))

        return self._reconcile(s.key, candidates, s.ts)

    def _reconcile(self, key: str, candidates: list, ts: float) -> list:
        """Turn raw findings into fired alerts, with repeat suppression, and
        keep ``self.active`` in sync so the API can show current status."""
        seen_codes = set()
        fired = []
        for code, severity, message, value, expected in candidates:
            seen_codes.add(code)
            self.active[(key, code)] = Alert(ts, key, severity, code, message,
                                             value, expected)
            last = self._last_fired.get((key, code), 0.0)
            if ts - last >= self.t.alert_repeat_s:
                self._last_fired[(key, code)] = ts
                fired.append(self.active[(key, code)])
        # clear conditions that no longer hold for this GPU
        for (k, code) in list(self.active):
            if k == key and code not in seen_codes:
                del self.active[(k, code)]
                self._last_fired.pop((k, code), None)
        return fired

    def status(self, key: str) -> dict:
        b = self.baselines.get(key)
        active = [a.as_dict() for (k, _), a in self.active.items() if k == key]
        worst = max((SEVERITY[a["severity"]] for a in active), default=-1)
        state = {2: "critical", 1: "warning", 0: "info", -1: "ok"}[worst]
        return {
            "state": state,
            "active_alerts": active,
            "baseline": {
                "expected_clock_mhz": b.expected_clock() if b else None,
                "expected_power_w": b.expected_power() if b else None,
                "samples_seen": b.samples_seen if b else 0,
            },
        }
