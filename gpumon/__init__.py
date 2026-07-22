"""gpumon — cross-vendor GPU monitoring and early-failure detection."""

from __future__ import annotations

__version__ = "1.0.0"

from .backends import GPUSample, active_backends
from .health import HealthEngine, Thresholds
from .monitor import Monitor
from .storage import Storage

__all__ = ["GPUSample", "active_backends", "HealthEngine", "Thresholds",
           "Monitor", "Storage", "__version__"]
