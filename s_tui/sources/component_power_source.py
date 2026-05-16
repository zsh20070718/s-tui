#!/usr/bin/env python

# Copyright (C) 2017-2026 Alex Manuskin, Gil Tsuker
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA
"""Component power source.

This source complements the RAPL CPU package power source with power readings
from server and accelerator interfaces that commonly exist on lab machines:

* Linux hwmon power*_input sensors
* NVIDIA GPUs via nvidia-smi
* IPMI power sensors and DCMI chassis power readings
"""

from __future__ import annotations

import csv
import glob
import logging
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from io import StringIO

from s_tui.helper_functions import cat, which
from s_tui.sources.source import Source

PROBE_TIMEOUT = 2.0
_POWER_INPUT_RE = re.compile(r"power(\d+)_input$")
_FLOAT_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


@dataclass(frozen=True)
class PowerReading:
    """A single instantaneous power reading in watts."""

    label: str
    current: float


def _safe_label(label: str) -> str:
    """Normalize provider labels for compact TUI display."""
    return " ".join(label.strip().split())


def _dedupe_readings(readings: list[PowerReading]) -> list[PowerReading]:
    """Keep duplicate provider labels stable by adding ,N suffixes."""
    seen: dict[str, int] = {}
    result = []
    for reading in readings:
        count = seen.get(reading.label, 0)
        seen[reading.label] = count + 1
        label = reading.label if count == 0 else f"{reading.label},{count}"
        result.append(PowerReading(label, reading.current))
    return result


def _first_float(value: str) -> float | None:
    match = _FLOAT_RE.search(value.strip())
    if match is None:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _read_text(path: str, fallback: str = "") -> str:
    try:
        return str(cat(path, fallback=fallback, binary=False)).strip()
    except OSError:
        return fallback


def _read_hwmon_power(base_path: str = "/sys/class/hwmon") -> list[PowerReading]:
    """Read Linux hwmon power*_input sensors.

    The kernel hwmon ABI reports power*_input in microwatts.
    """
    readings: list[PowerReading] = []
    for input_path in sorted(
        glob.glob(os.path.join(base_path, "hwmon*", "power*_input"))
    ):
        match = _POWER_INPUT_RE.search(os.path.basename(input_path))
        if match is None:
            continue

        raw_value = _read_text(input_path)
        watts = _first_float(raw_value)
        if watts is None:
            continue

        hwmon_dir = os.path.dirname(input_path)
        sensor_id = match.group(1)
        chip_name = _read_text(
            os.path.join(hwmon_dir, "name"), os.path.basename(hwmon_dir)
        )
        power_label = _read_text(
            os.path.join(hwmon_dir, f"power{sensor_id}_label"),
            f"power{sensor_id}",
        )
        label = _safe_label(f"hwmon:{chip_name}:{power_label}")
        readings.append(PowerReading(label, watts / 1_000_000.0))
    return readings


def _read_nvidia_power(
    nvidia_smi_exe: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[PowerReading]:
    """Read NVIDIA GPU power.draw via nvidia-smi."""
    exe = nvidia_smi_exe or which("nvidia-smi")
    if exe is None:
        return []

    cmd = [
        exe,
        "--query-gpu=index,name,power.draw",
        "--format=csv,noheader,nounits",
    ]
    try:
        result = runner(cmd, capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        logging.debug("Unable to run nvidia-smi for component power", exc_info=True)
        return []
    if result.returncode != 0:
        logging.debug("nvidia-smi failed: %s", result.stderr.strip())
        return []

    readings = []
    for row in csv.reader(StringIO(result.stdout)):
        if len(row) < 3:
            continue
        gpu_index = row[0].strip()
        gpu_name = _safe_label(row[1])
        watts = _first_float(row[2])
        if watts is None:
            continue
        readings.append(PowerReading(f"GPU{gpu_index}:{gpu_name}", watts))
    return readings


def _read_ipmi_sensor_power(
    ipmitool_exe: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[PowerReading]:
    """Read IPMI sensor rows whose unit is watts."""
    exe = ipmitool_exe or which("ipmitool")
    if exe is None:
        return []

    try:
        result = runner(
            [exe, "sensor"],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        logging.debug("Unable to run ipmitool sensor for power", exc_info=True)
        return []
    if result.returncode != 0:
        logging.debug("ipmitool sensor failed: %s", result.stderr.strip())
        return []

    readings = []
    for line in result.stdout.splitlines():
        fields = [item.strip() for item in line.split("|")]
        if len(fields) < 3:
            continue
        name, value, unit = fields[0], fields[1], fields[2]
        if "watt" not in unit.lower() and unit.strip().lower() != "w":
            continue
        watts = _first_float(value)
        if watts is None:
            continue
        readings.append(PowerReading(_safe_label(f"IPMI:{name}"), watts))
    return readings


def _read_ipmi_dcmi_power(
    ipmitool_exe: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[PowerReading]:
    """Read the DCMI instantaneous chassis power reading."""
    exe = ipmitool_exe or which("ipmitool")
    if exe is None:
        return []

    try:
        result = runner(
            [exe, "dcmi", "power", "reading"],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        logging.debug("Unable to run ipmitool dcmi power reading", exc_info=True)
        return []
    if result.returncode != 0:
        logging.debug("ipmitool dcmi power reading failed: %s", result.stderr.strip())
        return []

    for line in result.stdout.splitlines():
        if "instantaneous power reading" not in line.lower():
            continue
        watts = _first_float(line)
        if watts is not None:
            return [PowerReading("IPMI:DCMI", watts)]
    return []


def _probe_power_readings() -> list[PowerReading]:
    """Return component power readings from all supported providers."""
    readings = []
    readings.extend(_read_hwmon_power())
    readings.extend(_read_nvidia_power())
    readings.extend(_read_ipmi_sensor_power())
    readings.extend(_read_ipmi_dcmi_power())
    return _dedupe_readings(readings)


class ComponentPowerSource(Source):
    """Source for non-RAPL component power information."""

    def __init__(self) -> None:
        Source.__init__(self)

        self.name = "CompPower"
        self.measurement_unit = "W"
        self.pallet = (
            "power light",
            "power dark",
            "power light smooth",
            "power dark smooth",
        )
        self.max_power = 1.0

        initial_readings = _probe_power_readings()
        if not initial_readings:
            self.is_available = False
            logging.debug("Component power reading is not available")
            return

        self.available_sensors = [reading.label for reading in initial_readings]
        self._sensor_lookup = {
            reading.label: idx for idx, reading in enumerate(initial_readings)
        }
        self.sensor_available = [True] * len(self.available_sensors)
        self.last_measurement = [reading.current for reading in initial_readings]
        self.max_power = max([self.max_power, *self.last_measurement])

    def update(self) -> None:
        if not self.is_available:
            return

        readings = _probe_power_readings()
        updated = set()
        for reading in readings:
            idx = self._sensor_lookup.get(reading.label)
            if idx is None:
                continue
            self.last_measurement[idx] = reading.current
            self.sensor_available[idx] = True
            self.max_power = max(self.max_power, reading.current)
            updated.add(idx)

        for idx in range(len(self.available_sensors)):
            if idx not in updated:
                self.sensor_available[idx] = False

    def get_edge_triggered(self) -> bool:
        return False

    def get_maximum(self) -> float:
        return self.max_power

    def reset(self) -> None:
        self.max_power = max([1.0, *self.last_measurement])

    def get_top(self) -> int:
        return max(1, int(self.max_power))
