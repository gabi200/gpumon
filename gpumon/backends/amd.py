"""AMD backend built on the amdgpu sysfs / hwmon interface.

No external tooling required (works without rocm-smi). Everything is read
straight from ``/sys/class/drm/cardN/device`` and its ``hwmon`` node, which
means it also covers integrated Radeon GPUs that ship without a fan sensor.
"""

from __future__ import annotations

import glob
import os
import re
from typing import Optional

from .base import GPUBackend, GPUSample, now

DRM_GLOB = "/sys/class/drm/card[0-9]*/device"
AMD_VENDOR = "0x1002"


def _read(path: str) -> Optional[str]:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def _read_int(path: str) -> Optional[int]:
    v = _read(path)
    if v is None:
        return None
    try:
        return int(v)
    except ValueError:
        return None


def _hwmon_dir(device: str) -> Optional[str]:
    matches = glob.glob(os.path.join(device, "hwmon", "hwmon*"))
    return matches[0] if matches else None


def _labelled(hwmon: str, prefix: str) -> dict:
    """Map hwmon label text -> input file path for a sensor family.

    e.g. prefix="temp" -> {"edge": ".../temp1_input", "junction": ...}
    """
    out = {}
    for label_path in glob.glob(os.path.join(hwmon, f"{prefix}*_label")):
        label = _read(label_path)
        if not label:
            continue
        input_path = label_path.replace("_label", "_input")
        if os.path.exists(input_path):
            out[label.lower()] = input_path
    return out


def _parse_pp_dpm(path: str):
    """Parse a pp_dpm_* clock table.

    Lines look like ``1: 400Mhz *`` where ``*`` marks the active state.
    Returns (current_mhz, max_mhz).
    """
    text = _read(path)
    if not text:
        return None, None
    current = None
    values = []
    for line in text.splitlines():
        m = re.search(r"(\d+)\s*mhz", line, re.IGNORECASE)
        if not m:
            continue
        mhz = int(m.group(1))
        values.append(mhz)
        if "*" in line:
            current = mhz
    max_mhz = max(values) if values else None
    return current, max_mhz


class AMDBackend(GPUBackend):
    vendor = "amd"

    @staticmethod
    def available() -> bool:
        for device in glob.glob(DRM_GLOB):
            if _read(os.path.join(device, "vendor")) == AMD_VENDOR:
                return True
        return False

    def discover(self) -> list:
        handles = []
        for device in sorted(glob.glob(DRM_GLOB)):
            if _read(os.path.join(device, "vendor")) != AMD_VENDOR:
                continue
            hwmon = _hwmon_dir(device)
            if hwmon is None:
                continue
            handles.append((device, hwmon))
        # assign stable per-vendor indices
        return list(enumerate(handles))

    def _name(self, device: str, hwmon: str, index: int) -> str:
        did = _read(os.path.join(device, "device")) or "?"
        base = _read(os.path.join(hwmon, "name")) or "amdgpu"
        card = os.path.basename(os.path.dirname(device))
        return f"{base} {did} ({card})"

    def read(self, handle) -> Optional[GPUSample]:
        index, (device, hwmon) = handle

        temps = _labelled(hwmon, "temp")
        # temp1_input is always the edge sensor even without a label
        temp_edge = temps.get("edge") or os.path.join(hwmon, "temp1_input")

        def temp(path):
            v = _read_int(path) if path else None
            return round(v / 1000.0, 1) if v is not None else None

        # power: average preferred, fall back to instantaneous
        power = _read_int(os.path.join(hwmon, "power1_average"))
        if power is None:
            power = _read_int(os.path.join(hwmon, "power1_input"))
        power_w = round(power / 1_000_000.0, 2) if power is not None else None

        cap = _read_int(os.path.join(hwmon, "power1_cap"))
        power_limit_w = round(cap / 1_000_000.0, 2) if cap is not None else None

        sclk_cur, sclk_max = _parse_pp_dpm(os.path.join(device, "pp_dpm_sclk"))
        if sclk_cur is None:
            freq = _read_int(os.path.join(hwmon, "freq1_input"))
            sclk_cur = round(freq / 1_000_000.0) if freq is not None else None
        mclk_cur, _ = _parse_pp_dpm(os.path.join(device, "pp_dpm_mclk"))

        load = _read_int(os.path.join(device, "gpu_busy_percent"))
        mem_load = _read_int(os.path.join(device, "mem_busy_percent"))

        fan_rpm = _read_int(os.path.join(hwmon, "fan1_input"))
        pwm = _read_int(os.path.join(hwmon, "pwm1"))
        fan_pct = round(pwm / 255.0 * 100.0, 1) if pwm is not None else None

        volt = _read_int(os.path.join(hwmon, "in0_input"))

        return GPUSample(
            ts=now(),
            vendor=self.vendor,
            index=index,
            key=f"amd:{index}",
            name=self._name(device, hwmon, index),
            temp_c=temp(temp_edge),
            hotspot_c=temp(temps.get("junction")),
            mem_temp_c=temp(temps.get("mem")),
            power_w=power_w,
            power_limit_w=power_limit_w,
            load_pct=float(load) if load is not None else None,
            mem_load_pct=float(mem_load) if mem_load is not None else None,
            clock_mhz=float(sclk_cur) if sclk_cur is not None else None,
            max_clock_mhz=float(sclk_max) if sclk_max is not None else None,
            mem_clock_mhz=float(mclk_cur) if mclk_cur is not None else None,
            fan_rpm=float(fan_rpm) if fan_rpm is not None else None,
            fan_pct=fan_pct,
            voltage_mv=float(volt) if volt is not None else None,
            extra={"sysfs": device},
        )
