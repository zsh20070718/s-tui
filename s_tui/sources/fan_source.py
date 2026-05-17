#!/usr/bin/env python

# Copyright (C) 2017-2025 Alex Manuskin, Maor Veitsman
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
"""This module implements a fan source"""

from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

import psutil

from s_tui.fan_control_menu import read_inspur_bmc_payload
from s_tui.ipmi import ipmi_access_configured, run_ipmi_sensor
from s_tui.sources.source import Source

IPMI_SENSOR_TIMEOUT = 10.0
MAX_REASONABLE_FAN_RPM = 50000
_FLOAT_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


@dataclass(frozen=True)
class FanReading:
    """A single instantaneous fan speed reading in RPM."""

    label: str
    current: int


def _safe_label(label: str) -> str:
    return " ".join(label.strip().split())


def _dedupe_readings(readings: list[FanReading]) -> list[FanReading]:
    seen: dict[str, int] = {}
    result = []
    for reading in readings:
        count = seen.get(reading.label, 0)
        seen[reading.label] = count + 1
        label = reading.label if count == 0 else f"{reading.label},{count}"
        result.append(FanReading(label, reading.current))
    return result


def _first_float(value: str) -> float | None:
    match = _FLOAT_RE.search(value.strip())
    if match is None:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _read_psutil_fans() -> list[FanReading]:
    try:
        sensors_dict = psutil.sensors_fans()
    except AttributeError:
        logging.debug("Fans sensors is not available from psutil")
        return []
    except (OSError, TypeError):
        # psutil may raise TypeError when sysfs fan sensor files contain None.
        logging.debug("Unable to read fans from psutil", exc_info=True)
        return []
    if not sensors_dict:
        return []

    readings = []
    for key, value in sensors_dict.items():
        sensor_name = key
        for sensor_idx, sensor in enumerate(value):
            sensor_label = sensor.label
            full_name = (
                sensor_label if sensor_label else sensor_name + "," + str(sensor_idx)
            )
            logging.debug("Fan sensor name %s", full_name)
            readings.append(FanReading(_safe_label(full_name), int(sensor.current)))
    return readings


def _read_ipmi_fans(
    ipmitool_exe: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[FanReading]:
    """Read IPMI fan sensors whose unit is RPM."""
    if runner is subprocess.run and not ipmi_access_configured():
        logging.debug("IPMI access is not configured for fan readings")
        return []

    try:
        result = run_ipmi_sensor(
            ipmitool_exe,
            runner=runner,
            timeout=IPMI_SENSOR_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        logging.debug("ipmitool sensor timed out for fan readings", exc_info=True)
        return []
    except (OSError, subprocess.SubprocessError):
        logging.debug("Unable to run ipmitool sensor for fan readings", exc_info=True)
        return []
    if result is None:
        return []
    if result.returncode != 0:
        logging.debug("ipmitool sensor failed for fans: %s", result.stderr.strip())
        return []

    readings = []
    for line in result.stdout.splitlines():
        fields = [item.strip() for item in line.split("|")]
        if len(fields) < 3:
            continue
        name, value, unit = fields[0], fields[1], fields[2]
        if "rpm" not in unit.lower():
            continue
        rpm = _first_float(value)
        if rpm is None:
            continue
        readings.append(FanReading(_safe_label(f"IPMI:{name}"), int(rpm)))
    return readings


def _read_inspur_bmc_fans(
    payload_reader: Callable[[str], object | None] = read_inspur_bmc_payload,
) -> list[FanReading]:
    """Read fan RPM from the Inspur BMC Web API."""
    payload = payload_reader("/api/status/fan_info")
    if not isinstance(payload, dict):
        return []
    fans = payload.get("fans")
    if not isinstance(fans, list):
        return []

    readings = []
    for fan in fans:
        if not isinstance(fan, dict):
            continue
        if fan.get("is_disable") or fan.get("status") == "Absent":
            continue
        rpm = _first_float(str(fan.get("speed_rpm", "")))
        if rpm is None:
            continue
        name = str(fan.get("fan_name") or fan.get("name") or fan.get("id") or "fan")
        readings.append(FanReading(_safe_label(f"BMC:{name}"), int(rpm)))
    return readings


def _probe_fan_readings() -> list[FanReading]:
    readings = _read_inspur_bmc_fans()
    if readings:
        return _dedupe_readings(readings)

    readings = _read_psutil_fans()
    if readings:
        return _dedupe_readings(readings)
    return _dedupe_readings(_read_ipmi_fans())


class FanSource(Source):
    """Source for fan information"""

    def __init__(self):
        Source.__init__(self)

        self.name = "Fan"
        self.measurement_unit = "RPM"
        self.pallet = ("fan light", "fan dark", "fan light smooth", "fan dark smooth")

        initial_readings = _probe_fan_readings()
        if not initial_readings:
            self.is_available = False
            return

        self._sensor_lookup = {}
        for reading in initial_readings:
            self.available_sensors.append(reading.label)
            self._sensor_lookup[reading.label] = len(self.available_sensors) - 1

        self.sensor_available = [True] * len(self.available_sensors)
        self.last_measurement = [reading.current for reading in initial_readings]

    def update(self) -> None:
        sample = _probe_fan_readings()
        if not sample:
            logging.debug("No fan readings available, keeping stale data")
            return

        updated = set()
        for reading in sample:
            idx = self._sensor_lookup.get(reading.label)
            if idx is None:
                continue  # new sensor not in original list
            if reading.current > MAX_REASONABLE_FAN_RPM:
                self.sensor_available[idx] = False
                continue
            self.last_measurement[idx] = reading.current
            self.sensor_available[idx] = True
            updated.add(idx)

        # Mark sensors not seen in this sample as unavailable
        for idx in range(len(self.available_sensors)):
            if idx not in updated:
                self.sensor_available[idx] = False

    def get_edge_triggered(self) -> bool:
        return False

    def get_top(self) -> int:
        return 1
