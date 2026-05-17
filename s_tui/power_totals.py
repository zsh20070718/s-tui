#!/usr/bin/env python
"""Aggregate high-level power totals from available s-tui power sources."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class PowerTotals:
    """Current aggregate power readings in watts."""

    machine: float | None
    fan: float | None
    cpu: float | None
    gpu: float | None
    cpu_gpu: float | None


@dataclass(frozen=True)
class _Reading:
    label: str
    watts: float


_NVIDIA_SMI_LABEL_RE = re.compile(r"^gpu\d+:", re.IGNORECASE)
_PSU_LABEL_RE = re.compile(r"\b(psu\d*|ps\d+|power supply)\b", re.IGNORECASE)


def _normalize_label(label: str) -> str:
    return " ".join(label.replace("_", " ").replace("-", " ").lower().split())


def _source_readings(source: object) -> list[_Reading]:
    labels = source.get_sensor_list()
    values = source.get_reading_list()
    sensor_available = getattr(source, "sensor_available", [])

    readings = []
    for idx, label in enumerate(labels):
        if idx >= len(values):
            continue
        if idx < len(sensor_available) and not sensor_available[idx]:
            continue
        try:
            watts = float(values[idx])
        except (TypeError, ValueError):
            continue
        if watts < 0:
            continue
        readings.append(_Reading(str(label), watts))
    return readings


def _is_fan_label(label: str) -> bool:
    return "fan" in _normalize_label(label)


def _is_gpu_label(label: str) -> bool:
    normalized = _normalize_label(label)
    return (
        "gpu" in normalized
        or "nvidia" in normalized
        or "amdgpu" in normalized
        or "amd gpu" in normalized
    )


def _is_cpu_label(label: str) -> bool:
    normalized = _normalize_label(label)
    if _is_gpu_label(label):
        return False
    return any(
        marker in normalized
        for marker in (
            "cpu",
            "processor",
            "package",
            "socket",
            "rapl",
            "core",
        )
    )


def _is_psu_input_label(label: str) -> bool:
    normalized = _normalize_label(label)
    return bool(_PSU_LABEL_RE.search(normalized)) and any(
        marker in normalized for marker in ("input", "inlet", "ac")
    )


def _is_machine_total_label(label: str) -> bool:
    normalized = _normalize_label(label)
    if _is_fan_label(label) or _is_gpu_label(label) or _is_cpu_label(label):
        return False
    if "dcmi" in normalized:
        return True
    return any(
        marker in normalized
        for marker in (
            "pwr consumption",
            "power consumption",
            "power usage",
            "system power",
            "chassis power",
            "node power",
            "total power",
            "total pwr",
        )
    )


def _machine_total(readings: list[_Reading]) -> float | None:
    total_readings = [r for r in readings if _is_machine_total_label(r.label)]
    if total_readings:
        return max(r.watts for r in total_readings)

    dcmi_readings = [r for r in readings if "dcmi" in _normalize_label(r.label)]
    if dcmi_readings:
        return dcmi_readings[0].watts

    psu_input_readings = [r for r in readings if _is_psu_input_label(r.label)]
    if psu_input_readings:
        return sum(r.watts for r in psu_input_readings)

    return None


def _fan_total(readings: list[_Reading]) -> float | None:
    fan_readings = [r for r in readings if _is_fan_label(r.label)]
    if not fan_readings:
        return None
    return sum(r.watts for r in fan_readings)


def _rapl_cpu_total(readings: list[_Reading]) -> float | None:
    package_readings = [
        r
        for r in readings
        if any(marker in _normalize_label(r.label) for marker in ("package", "socket"))
    ]
    if package_readings:
        return sum(r.watts for r in package_readings)
    if readings:
        return sum(r.watts for r in readings)
    return None


def _component_cpu_total(readings: list[_Reading]) -> float | None:
    cpu_readings = [
        r
        for r in readings
        if _is_cpu_label(r.label)
        and not _is_machine_total_label(r.label)
        and not _is_psu_input_label(r.label)
        and not _is_fan_label(r.label)
    ]
    if not cpu_readings:
        return None
    return sum(r.watts for r in cpu_readings)


def _component_gpu_total(readings: list[_Reading]) -> float | None:
    nvidia_smi_readings = [r for r in readings if _NVIDIA_SMI_LABEL_RE.search(r.label)]
    if nvidia_smi_readings:
        total = sum(r.watts for r in nvidia_smi_readings)
        for reading in readings:
            normalized = _normalize_label(reading.label)
            if _NVIDIA_SMI_LABEL_RE.search(reading.label):
                continue
            if "nvidia" in normalized:
                continue
            if _is_gpu_label(reading.label):
                total += reading.watts
        return total

    gpu_readings = [r for r in readings if _is_gpu_label(r.label)]
    if not gpu_readings:
        return None
    return sum(r.watts for r in gpu_readings)


def _cpu_total(
    rapl_readings: list[_Reading], component_readings: list[_Reading]
) -> float | None:
    cpu_total = _rapl_cpu_total(rapl_readings)
    if cpu_total is None:
        cpu_total = _component_cpu_total(component_readings)
    return cpu_total


def _gpu_total(component_readings: list[_Reading]) -> float | None:
    return _component_gpu_total(component_readings)


def _cpu_gpu_total(cpu_total: float | None, gpu_total: float | None) -> float | None:
    if cpu_total is None and gpu_total is None:
        return None
    return (cpu_total or 0.0) + (gpu_total or 0.0)


def collect_power_totals(sources: Iterable[object]) -> PowerTotals:
    """Collect aggregate power totals from current source measurements."""
    rapl_readings: list[_Reading] = []
    component_readings: list[_Reading] = []

    for source in sources:
        source_name = source.get_source_name()
        if source_name == "Power":
            rapl_readings = _source_readings(source)
        elif source_name == "CompPower":
            component_readings = _source_readings(source)

    cpu_total = _cpu_total(rapl_readings, component_readings)
    gpu_total = _gpu_total(component_readings)
    return PowerTotals(
        machine=_machine_total(component_readings),
        fan=_fan_total(component_readings),
        cpu=cpu_total,
        gpu=gpu_total,
        cpu_gpu=_cpu_gpu_total(cpu_total, gpu_total),
    )


def format_power(value: float | None) -> str:
    """Format a power reading for the TUI."""
    if value is None:
        return "N/A"
    return f"{value:.1f} W"
