#!/usr/bin/env python
"""Shared nvidia-smi reader used by the GPU sources.

A single subprocess is invoked per refresh cycle and the parsed result is
cached for a short window so the three GPU sources (util / temp / power)
that share this module only spawn one ``nvidia-smi`` per s-tui tick.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
from threading import Lock


_NVIDIA_SMI = shutil.which("nvidia-smi")
_QUERY_FIELDS = (
    "index",
    "name",
    "utilization.gpu",
    "memory.used",
    "memory.total",
    "temperature.gpu",
    "power.draw",
    "power.limit",
)
_QUERY_ARG = ",".join(_QUERY_FIELDS)
_CACHE_TTL_SECONDS = 0.4


@dataclass
class GpuSample:
    index: int
    name: str
    util_percent: float
    mem_used_mib: float
    mem_total_mib: float
    temperature_c: float
    power_w: float
    power_limit_w: float


class NvidiaGpuReader:
    """Rate-limited wrapper around ``nvidia-smi --query-gpu``."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._last_sample_time = 0.0
        self._last_samples: list[GpuSample] = []
        self._available = _NVIDIA_SMI is not None
        self._gpu_count = 0

        if self._available:
            initial = self._query()
            if initial is None:
                self._available = False
            else:
                self._last_samples = initial
                self._last_sample_time = time.monotonic()
                self._gpu_count = len(initial)

    @property
    def is_available(self) -> bool:
        return self._available and self._gpu_count > 0

    @property
    def gpu_count(self) -> int:
        return self._gpu_count

    def get_names(self) -> list[str]:
        return [s.name for s in self._last_samples]

    def sample(self) -> list[GpuSample]:
        """Return the most recent samples, refreshing if cache expired."""
        if not self._available:
            return []
        now = time.monotonic()
        with self._lock:
            if now - self._last_sample_time >= _CACHE_TTL_SECONDS:
                fresh = self._query()
                if fresh is not None:
                    self._last_samples = fresh
                    self._last_sample_time = now
            return list(self._last_samples)

    def _query(self) -> list[GpuSample] | None:
        if _NVIDIA_SMI is None:
            return None
        try:
            proc = subprocess.run(
                [
                    _NVIDIA_SMI,
                    f"--query-gpu={_QUERY_ARG}",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as err:
            logging.debug("nvidia-smi query failed: %s", err)
            return None

        if proc.returncode != 0:
            logging.debug("nvidia-smi returned %d: %s", proc.returncode, proc.stderr)
            return None

        samples: list[GpuSample] = []
        for line in proc.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < len(_QUERY_FIELDS):
                continue
            try:
                samples.append(
                    GpuSample(
                        index=int(parts[0]),
                        name=parts[1],
                        util_percent=_to_float(parts[2]),
                        mem_used_mib=_to_float(parts[3]),
                        mem_total_mib=_to_float(parts[4]),
                        temperature_c=_to_float(parts[5]),
                        power_w=_to_float(parts[6]),
                        power_limit_w=_to_float(parts[7]),
                    )
                )
            except (ValueError, IndexError) as err:
                logging.debug("Failed to parse nvidia-smi row %r: %s", line, err)
        return samples


def _to_float(token: str) -> float:
    # nvidia-smi can emit "[N/A]" or "[Not Supported]" for some fields
    if not token or token.startswith("[") or "not supported" in token.lower():
        return 0.0
    try:
        return float(token)
    except ValueError:
        return 0.0


_shared_reader: NvidiaGpuReader | None = None


def get_shared_reader() -> NvidiaGpuReader:
    """Return the process-wide reader so all GPU sources share one query."""
    global _shared_reader
    if _shared_reader is None:
        _shared_reader = NvidiaGpuReader()
    return _shared_reader
