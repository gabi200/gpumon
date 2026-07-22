"""Backend abstractions shared by the NVIDIA and AMD collectors."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict, field
from typing import Optional


@dataclass
class GPUSample:
    """A single point-in-time reading for one GPU.

    Every numeric field is Optional: a sensor that a given card does not
    expose (e.g. an integrated GPU without a fan) is reported as ``None``
    rather than a fake ``0`` so health logic can tell "absent" from "zero".
    """

    ts: float
    vendor: str                 # "nvidia" | "amd"
    index: int                  # per-vendor ordinal, stable within a boot
    key: str                    # stable identity, e.g. "amd:0"
    name: str

    temp_c: Optional[float] = None          # edge/core temperature
    hotspot_c: Optional[float] = None        # junction/hotspot temperature
    mem_temp_c: Optional[float] = None
    power_w: Optional[float] = None          # board power draw
    power_limit_w: Optional[float] = None    # cap / TDP
    load_pct: Optional[float] = None         # core utilisation
    mem_load_pct: Optional[float] = None
    clock_mhz: Optional[float] = None        # current core/sclk
    max_clock_mhz: Optional[float] = None    # advertised max core clock
    mem_clock_mhz: Optional[float] = None
    fan_rpm: Optional[float] = None
    fan_pct: Optional[float] = None
    voltage_mv: Optional[float] = None

    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


class GPUBackend(ABC):
    """One backend manages every GPU it can see for a single vendor."""

    vendor: str = "unknown"

    @staticmethod
    @abstractmethod
    def available() -> bool:
        """Return True if this backend can run on the current machine."""

    @abstractmethod
    def discover(self) -> list:
        """Return a list of opaque device handles (one per GPU)."""

    @abstractmethod
    def read(self, handle) -> Optional[GPUSample]:
        """Read one sample for a handle, or None if it could not be read."""

    def sample_all(self) -> list:
        out = []
        for handle in self.discover():
            try:
                s = self.read(handle)
            except Exception:
                s = None
            if s is not None:
                out.append(s)
        return out


def now() -> float:
    return time.time()
