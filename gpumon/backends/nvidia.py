"""NVIDIA backend.

Prefers the pynvml library when installed (richer + faster), otherwise
shells out to ``nvidia-smi`` so the tool still works with a bare driver
install and no Python dependencies.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Optional

from .base import GPUBackend, GPUSample, now

try:  # optional dependency
    import pynvml  # type: ignore
    _HAVE_PYNVML = True
except Exception:  # pragma: no cover - depends on host
    _HAVE_PYNVML = False


def _f(value) -> Optional[float]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v


QUERY_FIELDS = [
    "index",
    "name",
    "temperature.gpu",
    "power.draw",
    "power.limit",
    "utilization.gpu",
    "utilization.memory",
    "clocks.gr",
    "clocks.max.gr",
    "clocks.mem",
    "fan.speed",
]


class NvidiaBackend(GPUBackend):
    vendor = "nvidia"

    def __init__(self) -> None:
        self._nvml_ready = False
        if _HAVE_PYNVML:
            try:
                pynvml.nvmlInit()
                self._nvml_ready = True
            except Exception:
                self._nvml_ready = False

    @staticmethod
    def available() -> bool:
        if _HAVE_PYNVML:
            try:
                pynvml.nvmlInit()
                pynvml.nvmlShutdown()
                return True
            except Exception:
                pass
        return shutil.which("nvidia-smi") is not None

    # -- discovery -----------------------------------------------------
    def discover(self) -> list:
        if self._nvml_ready:
            try:
                return [
                    (i, pynvml.nvmlDeviceGetHandleByIndex(i))
                    for i in range(pynvml.nvmlDeviceGetCount())
                ]
            except Exception:
                self._nvml_ready = False
        # smi path: a single query returns every GPU, so we sample in read_all
        return [("smi", None)]

    def sample_all(self) -> list:
        if self._nvml_ready:
            return super().sample_all()
        return self._read_smi_all()

    # -- pynvml path ---------------------------------------------------
    def read(self, handle) -> Optional[GPUSample]:
        index, h = handle
        if not self._nvml_ready:
            return None
        g = pynvml

        def q(fn, *a):
            try:
                return fn(*a)
            except Exception:
                return None

        name = q(g.nvmlDeviceGetName, h)
        if isinstance(name, bytes):
            name = name.decode()
        temp = q(g.nvmlDeviceGetTemperature, h, g.NVML_TEMPERATURE_GPU)
        power = q(g.nvmlDeviceGetPowerUsage, h)
        limit = q(g.nvmlDeviceGetEnforcedPowerLimit, h)
        util = q(g.nvmlDeviceGetUtilizationRates, h)
        clock = q(g.nvmlDeviceGetClockInfo, h, g.NVML_CLOCK_GRAPHICS)
        max_clock = q(g.nvmlDeviceGetMaxClockInfo, h, g.NVML_CLOCK_GRAPHICS)
        mem_clock = q(g.nvmlDeviceGetClockInfo, h, g.NVML_CLOCK_MEM)
        fan = q(g.nvmlDeviceGetFanSpeed, h)

        return GPUSample(
            ts=now(),
            vendor=self.vendor,
            index=index,
            key=f"nvidia:{index}",
            name=name or f"nvidia:{index}",
            temp_c=_f(temp),
            power_w=_f(power) / 1000.0 if power is not None else None,
            power_limit_w=_f(limit) / 1000.0 if limit is not None else None,
            load_pct=_f(util.gpu) if util else None,
            mem_load_pct=_f(util.memory) if util else None,
            clock_mhz=_f(clock),
            max_clock_mhz=_f(max_clock),
            mem_clock_mhz=_f(mem_clock),
            fan_pct=_f(fan),
        )

    # -- nvidia-smi path ----------------------------------------------
    def _read_smi_all(self) -> list:
        cmd = [
            "nvidia-smi",
            "--query-gpu=" + ",".join(QUERY_FIELDS),
            "--format=csv,noheader,nounits",
        ]
        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10, check=True
            ).stdout
        except Exception:
            return []

        samples = []
        for line in out.strip().splitlines():
            cols = [c.strip() for c in line.split(",")]
            if len(cols) < len(QUERY_FIELDS):
                continue
            (idx, name, temp, pdraw, plimit, ugpu, umem,
             cgr, cmax, cmem, fan) = cols[: len(QUERY_FIELDS)]
            index = int(_f(idx) or 0)
            samples.append(GPUSample(
                ts=now(),
                vendor=self.vendor,
                index=index,
                key=f"nvidia:{index}",
                name=name or f"nvidia:{index}",
                temp_c=_f(temp),
                power_w=_f(pdraw),
                power_limit_w=_f(plimit),
                load_pct=_f(ugpu),
                mem_load_pct=_f(umem),
                clock_mhz=_f(cgr),
                max_clock_mhz=_f(cmax),
                mem_clock_mhz=_f(cmem),
                fan_pct=_f(fan),
            ))
        return samples
