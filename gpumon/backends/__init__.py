"""GPU backend registry."""

from __future__ import annotations

from .base import GPUBackend, GPUSample
from .amd import AMDBackend
from .nvidia import NvidiaBackend

ALL_BACKENDS = [NvidiaBackend, AMDBackend]


def active_backends():
    """Instantiate every backend that reports itself available on this host."""
    backends = []
    for cls in ALL_BACKENDS:
        try:
            if cls.available():
                backends.append(cls())
        except Exception:
            continue
    return backends


__all__ = ["GPUBackend", "GPUSample", "AMDBackend", "NvidiaBackend",
           "ALL_BACKENDS", "active_backends"]
