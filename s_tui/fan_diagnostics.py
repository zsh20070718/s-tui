#!/usr/bin/env python
"""Read-only diagnostics for fan, GPU power, and IPMI availability."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable

from s_tui.fan_control_menu import discover_fan_control_targets
from s_tui.ipmi import (
    IPMITOOL_ARGS_ENV,
    LOCAL_IPMI_DEVICE_PATHS,
    build_ipmitool_command,
    get_ipmitool_exe,
    ipmi_access_configured,
    local_ipmi_accessible_devices,
    local_ipmi_devices,
)
from s_tui.power_totals import collect_power_totals, format_power
from s_tui.sources.component_power_source import ComponentPowerSource
from s_tui.sources.fan_source import FanSource

DIAGNOSTIC_TIMEOUT = 5.0
_PSU_LABEL_RE = re.compile(r"\b(psu\d*|ps\d+|power supply)\b", re.IGNORECASE)


def _is_likely_psu_fan(label: str) -> bool:
    normalized = " ".join(label.replace("_", " ").replace("-", " ").split())
    return bool(_PSU_LABEL_RE.search(normalized))


def _read_source(source: object) -> list[tuple[str, object]]:
    if not source.get_is_available():
        return []
    source.update()
    labels = source.get_sensor_list()
    values = source.get_reading_list()
    available = getattr(source, "sensor_available", [True] * len(labels))
    readings = []
    for idx, label in enumerate(labels):
        if idx >= len(values):
            continue
        if idx < len(available) and not available[idx]:
            continue
        readings.append((str(label), values[idx]))
    return readings


def _ipmi_status_lines(
    ipmitool_exe: str | None,
    ipmi_vendor: str,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> list[str]:
    lines = ["IPMI:"]
    exe = get_ipmitool_exe(ipmitool_exe)
    devices = local_ipmi_devices()
    accessible_devices = set(local_ipmi_accessible_devices())
    configured_args = os.environ.get(IPMITOOL_ARGS_ENV, "").strip()

    lines.append(f"  ipmitool: {exe or 'not found'}")
    lines.append(f"  control vendor: {ipmi_vendor}")
    if devices:
        device_status = [
            path + (" accessible" if path in accessible_devices else " not accessible")
            for path in devices
        ]
        lines.append("  local device: " + ", ".join(device_status))
    else:
        lines.append(
            "  local device: missing (" + ", ".join(LOCAL_IPMI_DEVICE_PATHS) + ")"
        )
    lines.append(f"  {IPMITOOL_ARGS_ENV}: {'set' if configured_args else 'not set'}")

    if exe is None:
        lines.append("  access: unavailable; install ipmitool first")
        return lines
    if not ipmi_access_configured():
        lines.append(
            "  access: not configured; local IPMI needs /dev/ipmi*, "
            f"or set {IPMITOOL_ARGS_ENV} for remote BMC access"
        )
        return lines

    try:
        cmd = build_ipmitool_command(["mc", "info"], exe)
        if cmd is None:
            lines.append("  access: unavailable; ipmitool command could not be built")
            return lines
        result = runner(cmd, capture_output=True, text=True, timeout=DIAGNOSTIC_TIMEOUT)
    except subprocess.TimeoutExpired:
        lines.append("  mc info: timed out")
        return lines
    except OSError as exc:
        lines.append(f"  mc info: failed ({exc})")
        return lines

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        lines.append(f"  mc info: failed ({detail})")
    else:
        first_line = (
            result.stdout.splitlines()[0] if result.stdout.splitlines() else "ok"
        )
        lines.append(f"  mc info: ok ({first_line})")
    return lines


def build_fan_diagnostics(
    ipmitool_exe: str | None = None,
    ipmi_vendor: str = "auto",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    """Build a read-only diagnostic report for urgent fan-control setup."""
    lines: list[str] = []
    lines.extend(_ipmi_status_lines(ipmitool_exe, ipmi_vendor, runner))

    power_source = ComponentPowerSource()
    power_readings = _read_source(power_source)
    lines.append("")
    lines.append("Power Readings:")
    if power_readings:
        for label, value in power_readings:
            lines.append(f"  {label}: {float(value):.1f} W")
        totals = collect_power_totals([power_source])
        lines.append(f"  machine total: {format_power(totals.machine)}")
        lines.append(f"  fan total: {format_power(totals.fan)}")
        lines.append(f"  cpu total: {format_power(totals.cpu)}")
        lines.append(f"  gpu total: {format_power(totals.gpu)}")
    else:
        lines.append("  none")

    fan_source = FanSource()
    fan_readings = _read_source(fan_source)
    lines.append("")
    lines.append("Fan RPM Readings:")
    if fan_readings:
        for label, value in fan_readings:
            note = " likely PSU fan" if _is_likely_psu_fan(label) else ""
            lines.append(f"  {label}: {int(value)} RPM{note}")
    else:
        lines.append("  none")

    targets = discover_fan_control_targets(
        ipmitool_exe=ipmitool_exe,
        allow_unsafe=True,
        ipmi_vendor=ipmi_vendor,
    )
    lines.append("")
    lines.append("Fan Control Targets:")
    if targets:
        for target in targets:
            detail = target.kind
            if target.zone is not None:
                detail += f" zone={target.zone}"
            if target.pwm_path is not None:
                detail += f" pwm={target.pwm_path}"
            lines.append(f"  {target.target_id}: {target.label} ({detail})")
    else:
        lines.append("  none")

    lines.append("")
    lines.append("Immediate Checks:")
    lines.append(
        "  1. Ignore fan RPM labels marked likely PSU fan when mapping control effects."
    )
    lines.append(
        "  2. If local IPMI is missing, try loading ipmi_si and ipmi_devintf, "
        f"or use {IPMITOOL_ARGS_ENV} for LAN BMC access."
    )
    lines.append(
        "  3. Fan writes stay disabled unless s-tui is started with --enable-fan-control."
    )
    return "\n".join(lines)
