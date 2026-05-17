#!/usr/bin/env python
"""NVIDIA GPU sources for s-tui.

Provides three sources backed by ``nvidia-smi`` (via the shared reader):

* :class:`GpuUtilSource`  - per-GPU utilization, plus an Avg sensor.
* :class:`GpuTempSource`  - per-GPU temperature.
* :class:`GpuPowerSource` - per-GPU power draw in watts.
"""

from __future__ import annotations

import logging

from s_tui.sources.nvidia_gpu_reader import get_shared_reader
from s_tui.sources.source import Source


class _GpuSourceBase(Source):
    """Common bookkeeping for NVIDIA-backed sources."""

    def __init__(self) -> None:
        Source.__init__(self)
        self.reader = get_shared_reader()
        if not self.reader.is_available:
            self.is_available = False
            logging.debug("NVIDIA GPU source unavailable (no nvidia-smi or no GPUs)")
            return
        self._gpu_count = self.reader.gpu_count

    def _label_for(self, idx: int) -> str:
        return f"GPU{idx}"


class GpuUtilSource(_GpuSourceBase):
    """Per-GPU utilization, plus an Avg sensor when more than one GPU exists."""

    def __init__(self) -> None:
        super().__init__()
        if not self.is_available:
            return

        self.name = "GPU Util"
        self.measurement_unit = "%"
        self.pallet = (
            "util light",
            "util dark",
            "util light smooth",
            "util dark smooth",
        )

        self.available_sensors = []
        if self._gpu_count > 1:
            self.available_sensors.append("Avg")
        for idx in range(self._gpu_count):
            self.available_sensors.append(self._label_for(idx))
        self.last_measurement = [0.0] * len(self.available_sensors)
        self.sensor_available = [True] * len(self.available_sensors)

    def update(self) -> None:
        if not self.is_available:
            return
        samples = self.reader.sample()
        if not samples:
            return
        values = [s.util_percent for s in samples]
        if self._gpu_count > 1:
            self.last_measurement[0] = sum(values) / len(values)
            for i, v in enumerate(values):
                self.last_measurement[i + 1] = v
        else:
            self.last_measurement[0] = values[0]

    def get_top(self) -> int:
        return 100

    def reset(self) -> None:  # used by graphs Reset button
        pass

    def get_edge_triggered(self) -> bool:
        return False


class GpuTempSource(_GpuSourceBase):
    """Per-GPU temperature with an 85C default soft threshold."""

    THRESHOLD_TEMP = 85

    def __init__(self) -> None:
        super().__init__()
        if not self.is_available:
            return

        self.name = "GPU Temp"
        self.measurement_unit = "C"
        self.pallet = (
            "temp light",
            "temp dark",
            "temp light smooth",
            "temp dark smooth",
        )
        self.alert_pallet = (
            "high temp light",
            "high temp dark",
            "high temp light smooth",
            "high temp dark smooth",
        )

        self.available_sensors = [self._label_for(i) for i in range(self._gpu_count)]
        self.last_measurement = [0.0] * len(self.available_sensors)
        self.sensor_available = [True] * len(self.available_sensors)
        self.last_thresholds = [self.THRESHOLD_TEMP] * len(self.available_sensors)
        self._max_temp = 0.0

    def update(self) -> None:
        if not self.is_available:
            return
        samples = self.reader.sample()
        if not samples:
            return
        for i, s in enumerate(samples):
            if i >= len(self.last_measurement):
                break
            self.last_measurement[i] = s.temperature_c
        self._max_temp = max(self.last_measurement) if self.last_measurement else 0.0
        Source.update(self)

    def get_top(self) -> int:
        return 100

    def reset(self) -> None:
        self._max_temp = 0.0

    def get_edge_triggered(self) -> bool:
        return self._max_temp > self.THRESHOLD_TEMP

    def get_sensor_alerts(self) -> list[str | None]:
        alerts: list[str | None] = [None] * len(self.available_sensors)
        for idx, value in enumerate(self.last_measurement):
            if value > self.THRESHOLD_TEMP:
                alerts[idx] = "high temp txt"
        return alerts


class GpuPowerSource(_GpuSourceBase):
    """Per-GPU power draw in watts."""

    def __init__(self) -> None:
        super().__init__()
        if not self.is_available:
            return

        self.name = "GPU Power"
        self.measurement_unit = "W"
        self.pallet = (
            "power light",
            "power dark",
            "power light smooth",
            "power dark smooth",
        )

        self.available_sensors = [self._label_for(i) for i in range(self._gpu_count)]
        self.last_measurement = [0.0] * len(self.available_sensors)
        self.sensor_available = [True] * len(self.available_sensors)
        # Cache the per-GPU power limit so get_top() can return a stable max.
        self._power_limits = [0.0] * len(self.available_sensors)

    def update(self) -> None:
        if not self.is_available:
            return
        samples = self.reader.sample()
        if not samples:
            return
        for i, s in enumerate(samples):
            if i >= len(self.last_measurement):
                break
            self.last_measurement[i] = s.power_w
            if s.power_limit_w > self._power_limits[i]:
                self._power_limits[i] = s.power_limit_w

    def get_top(self) -> int:
        if any(self._power_limits):
            return int(max(self._power_limits))
        return 1

    def reset(self) -> None:
        pass

    def get_edge_triggered(self) -> bool:
        return False
