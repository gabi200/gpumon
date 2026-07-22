"""Spec enrichment: resolve each GPU to rated clock / power figures so the
health engine has an "expected" reference from the first sample, without
waiting to learn a baseline.

Resolution chain, highest confidence first:

1. ``expected`` in config .................. handled by health.py (wins)
2. ``specs_file`` JSON override ............ this module, keyed by PCI id or name
3. dbgpu package (optional, bundled data) .. this module, looked up by model name
4. driver self-reported max clock / limit .. already in the sample
5. learned baseline ........................ health.py

Name resolution: NVIDIA samples already carry the marketing name; AMD is
resolved from the PCI vendor:device id via the system ``pci.ids`` file (or
``lspci`` as a fallback).

Everything is best-effort and offline. A missing pci.ids, missing dbgpu, or an
unmatched model simply yields fewer pinned values — never an error.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional

from .backends import GPUSample

log = logging.getLogger("gpumon.specs")

_PCIIDS_PATHS = [
    "/usr/share/hwdata/pci.ids",
    "/usr/share/misc/pci.ids",
    "/usr/share/pci.ids",
]


@dataclass
class SpecMatch:
    model_name: Optional[str] = None
    max_clock_mhz: Optional[float] = None
    power_w: Optional[float] = None
    source: Optional[str] = None      # "specs_file" | "dbgpu" | "pci.ids"

    def is_empty(self) -> bool:
        return not (self.model_name or self.max_clock_mhz or self.power_w)


def _num(value) -> Optional[float]:
    """Coerce '1,733 MHz' / '180 W' / 180 -> float."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        m = re.search(r"[-+]?\d[\d,]*\.?\d*", value)
        if m:
            try:
                return float(m.group(0).replace(",", ""))
            except ValueError:
                return None
    return None


# ----------------------------------------------------------------------
# PCI id -> model name
# ----------------------------------------------------------------------
def _pciids_path() -> Optional[str]:
    for p in _PCIIDS_PATHS:
        if os.path.isfile(p):
            return p
    return None


def _lookup_pciids(vendor: str, device: str, path: str) -> Optional[str]:
    """Scan pci.ids for a device *within* the matching vendor block.

    Device ids are not globally unique (e.g. 15e7 exists under both AMD and
    Intel), so we must stay inside the right vendor section.
    """
    vendor, device = vendor.lower(), device.lower()
    in_vendor = False
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip() or line.startswith("#"):
                    continue
                if line[0] not in " \t":                 # vendor line
                    if in_vendor:                        # left our block
                        break
                    in_vendor = line[:4].lower() == vendor
                    continue
                if in_vendor and line.startswith("\t") and line[1] != "\t":
                    if line[1:5].lower() == device:      # device line
                        return line[5:].strip()
    except OSError:
        return None
    return None


def _lookup_lspci(vendor: str, device: str) -> Optional[str]:
    if not shutil.which("lspci"):
        return None
    try:
        out = subprocess.run(
            ["lspci", "-d", f"{vendor}:{device}", "-mm"],
            capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    import shlex
    for line in out.splitlines():
        parts = shlex.split(line)
        if len(parts) >= 4:      # slot, class, vendor, device
            return parts[3]
    return None


# ----------------------------------------------------------------------
# model name -> specs (dbgpu, optional)
# ----------------------------------------------------------------------
def _load_dbgpu():
    try:
        from dbgpu import GPUDatabase   # type: ignore
        return GPUDatabase.default()
    except Exception:
        return None


def _spec_to_dict(spec) -> dict:
    for attr in ("_asdict", "as_dict", "to_dict"):
        fn = getattr(spec, attr, None)
        if callable(fn):
            try:
                return dict(fn())
            except Exception:
                pass
    if hasattr(spec, "__dict__"):
        return dict(vars(spec))
    return {}


def _extract_specs(spec) -> tuple:
    """Pull (max_clock_mhz, power_w) out of a dbgpu record, tolerating whatever
    the field names happen to be."""
    data = _spec_to_dict(spec)
    boost = base = tdp = None
    for key, val in data.items():
        k = re.sub(r"[^a-z0-9]", "", key.lower())   # "Thermal Design Power" -> "thermaldesignpower"
        if boost is None and "boost" in k and "clock" in k:
            boost = _num(val)
        elif base is None and "base" in k and "clock" in k:
            base = _num(val)
        elif tdp is None and ("tdp" in k or "thermaldesignpower" in k
                              or k in ("power", "powerw", "powerwatts")):
            tdp = _num(val)
    return (boost or base), tdp


# ----------------------------------------------------------------------
# Enricher
# ----------------------------------------------------------------------
class Enricher:
    def __init__(self, specs_file: Optional[str] = None, use_dbgpu: bool = True):
        self._cache: dict = {}
        self._pciids = _pciids_path()
        self._overrides = self._load_overrides(specs_file)
        self._db = _load_dbgpu() if use_dbgpu else None
        if self._db is None and use_dbgpu:
            log.debug("dbgpu not available; spec lookup limited to pci.ids/override")

    @staticmethod
    def _load_overrides(path: Optional[str]) -> dict:
        if not path:
            return {}
        try:
            with open(path) as fh:
                return json.load(fh)
        except Exception as exc:
            log.warning("could not read specs_file %s: %s", path, exc)
            return {}

    def _pci_id(self, sample: GPUSample) -> Optional[tuple]:
        sysfs = (sample.extra or {}).get("sysfs")
        if not sysfs:
            return None

        def rd(name):
            try:
                with open(os.path.join(sysfs, name)) as fh:
                    return fh.read().strip().replace("0x", "").lower()
            except OSError:
                return None

        v, d = rd("vendor"), rd("device")
        return (v, d) if v and d else None

    def _resolve_name(self, sample: GPUSample, pci) -> Optional[str]:
        if sample.vendor == "nvidia" and sample.name:
            return sample.name           # already the marketing name
        if pci:
            v, d = pci
            if self._pciids:
                name = _lookup_pciids(v, d, self._pciids)
                if name:
                    return name
            return _lookup_lspci(v, d)
        return None

    def enrich(self, sample: GPUSample) -> SpecMatch:
        if sample.key in self._cache:
            return self._cache[sample.key]

        pci = self._pci_id(sample)
        name = self._resolve_name(sample, pci)
        match = SpecMatch(model_name=name)

        # 1) user override, keyed by "vendor:device" or by model name
        override = None
        if pci:
            override = self._overrides.get(f"{pci[0]}:{pci[1]}")
        if override is None and name:
            override = self._overrides.get(name)
        if override:
            match.max_clock_mhz = _num(override.get("max_clock_mhz"))
            match.power_w = _num(override.get("power_w"))
            match.source = "specs_file"

        # 2) dbgpu lookup by name
        elif name and self._db is not None:
            clock, power = self._db_lookup(name)
            if clock or power:
                match.max_clock_mhz, match.power_w = clock, power
                match.source = "dbgpu"

        if match.source is None and name:
            match.source = "pci.ids"     # name only, no rated numbers

        self._cache[sample.key] = match
        if not match.is_empty():
            log.info("spec for %s -> %s (clock=%s power=%s src=%s)", sample.key,
                     match.model_name, match.max_clock_mhz, match.power_w,
                     match.source)
        return match

    def _db_lookup(self, name: str) -> tuple:
        db = self._db
        spec = None
        try:
            spec = db[name]
        except Exception:
            search = getattr(db, "search", None)
            if callable(search):
                try:
                    hits = search(name)
                    spec = hits[0] if hits else None
                except Exception:
                    spec = None
        if spec is None:
            return (None, None)
        try:
            return _extract_specs(spec)
        except Exception:
            return (None, None)
