#!/usr/bin/env python
"""Minimal terminal UI for power readings and direct fan duty control."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable

import urwid

from s_tui.fan_control_menu import (
    FanControlTarget,
    apply_fan_target,
    discover_fan_control_targets,
    read_inspur_bmc_payload,
)
from s_tui.power_totals import (
    _is_cpu_label,
    _is_fan_label,
    _is_gpu_label,
    _is_machine_total_label,
    _is_psu_input_label,
    collect_power_totals,
    format_power,
)
from s_tui.sources.component_power_source import ComponentPowerSource
from s_tui.sources.rapl_power_source import RaplPowerSource

DEFAULT_REFRESH_SECONDS = 0.5
KEY_TEMPERATURE_NAMES = {"inlet_temp", "outlet_temp"}


def parse_fan_duty(value: str) -> int:
    """Parse a direct fan duty input as an integer percent."""
    try:
        duty = int(value.strip())
    except ValueError as exc:
        raise ValueError("fan duty must be an integer 0-100") from exc
    if duty < 0 or duty > 100:
        raise ValueError("fan duty must be between 0 and 100")
    return duty


def select_default_fan_target(
    targets: list[FanControlTarget],
) -> FanControlTarget | None:
    """Pick the best single target for the minimal UI."""
    for kind in ("bmc-inspur", "hwmon", "ipmi-supermicro", "ipmi-dell"):
        for target in targets:
            if target.kind == kind:
                return target
    return targets[0] if targets else None


def _first_float(value: object) -> float | None:
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _power_source_readings(source: object) -> list[tuple[str, float]]:
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
        readings.append((str(label), watts))
    return readings


def _group_power_readings(
    sources: list[object],
) -> tuple[list[tuple[str, float]], list[tuple[str, float]], list[tuple[str, float]]]:
    component_cpu = []
    rapl_cpu = []
    fan = []
    gpu = []

    for source in sources:
        source_name = source.get_source_name()
        for label, watts in _power_source_readings(source):
            if source_name == "Power" and _is_cpu_label(label):
                rapl_cpu.append((label, watts))
                continue
            if source_name != "CompPower":
                continue
            if _is_fan_label(label):
                fan.append((label, watts))
            elif _is_gpu_label(label):
                gpu.append((label, watts))
            elif (
                _is_cpu_label(label)
                and not _is_machine_total_label(label)
                and not _is_psu_input_label(label)
            ):
                component_cpu.append((label, watts))

    return component_cpu or rapl_cpu, fan, gpu


def _compact_label(label: str, group: str) -> str:
    display = label
    for prefix in ("BMC:", "IPMI:", "hwmon:"):
        if display.startswith(prefix):
            display = display[len(prefix) :]
            break
    if group == "gpu" and display.startswith("GPU"):
        return display.split(":", 1)[0]
    display = display.replace("_Power", "").replace("_", " ")
    if group == "fan" and display.lower() == "fan":
        return "Power"
    return display


def _format_group_lines(group: str, readings: list[tuple[str, float]]) -> list[str]:
    if not readings:
        return ["  N/A"]
    return [
        f"  {_compact_label(label, group)}: {format_power(watts)}"
        for label, watts in readings
    ]


def read_current_fan_percent(
    payload_reader: Callable[[str], object | None] = read_inspur_bmc_payload,
) -> str:
    """Read current fan speed percentage from the BMC fan list."""
    payload = payload_reader("/api/status/fan_info")
    if not isinstance(payload, dict):
        return "N/A"
    fans = payload.get("fans")
    if not isinstance(fans, list):
        return "N/A"

    percents = []
    for fan in fans:
        if not isinstance(fan, dict):
            continue
        if fan.get("is_disable") or fan.get("status") == "Absent":
            continue
        try:
            percent = int(str(fan.get("speed_percent", "")).strip())
        except ValueError:
            continue
        if 0 <= percent <= 100:
            percents.append(percent)

    if not percents:
        return "N/A"
    unique = sorted(set(percents))
    if len(unique) == 1:
        return f"{unique[0]}%"
    return f"{unique[0]}-{unique[-1]}%"


def format_power_details(sources: list[object], fan_percent: str) -> str:
    """Format CPU/Fan/GPU detail groups for the minimal UI."""
    cpu, fan, gpu = _group_power_readings(sources)
    lines = ["CPU:"]
    lines.extend(_format_group_lines("cpu", cpu))
    lines.append("Fan:")
    lines.extend(_format_group_lines("fan", fan))
    lines.append(f"  Percent: {fan_percent}")
    lines.append("GPU:")
    lines.extend(_format_group_lines("gpu", gpu))
    return "\n".join(lines)


def _is_key_temperature_name(name: str) -> bool:
    normalized = name.lower()
    if normalized in KEY_TEMPERATURE_NAMES:
        return True
    if normalized.startswith("cpu") and normalized.endswith("_temp"):
        return normalized[3:-5].isdigit()
    if normalized.startswith("gpu") and normalized.endswith("_temp"):
        return normalized[3:-5].isdigit()
    return False


def _compact_temperature_label(name: str) -> str:
    return name.replace("_Temp", "").replace("_", " ")


def read_key_temperatures(
    payload_reader: Callable[[str], object | None] = read_inspur_bmc_payload,
) -> list[tuple[str, float]]:
    """Read important BMC temperatures for the minimal UI."""
    payload = payload_reader("/api/sensors/temAndPowerReading")
    if not isinstance(payload, list):
        return []

    readings = []
    for sensor in payload:
        if not isinstance(sensor, dict):
            continue
        name = str(sensor.get("name", ""))
        if not _is_key_temperature_name(name):
            continue
        if str(sensor.get("unit", "")).lower() not in {"deg_c", "c", "degrees c"}:
            continue
        if sensor.get("accessible") not in (None, 0, "0"):
            continue
        value = _first_float(sensor.get("reading"))
        if value is None or value <= 0:
            continue
        readings.append((_compact_temperature_label(name), value))
    return readings


def format_temperature_details(temperatures: list[tuple[str, float]]) -> str:
    """Format important temperatures for the minimal UI."""
    lines = ["Temp:"]
    if not temperatures:
        lines.append("  N/A")
        return "\n".join(lines)
    lines.extend(f"  {label}: {value:.1f} C" for label, value in temperatures)
    return "\n".join(lines)


class DirectDutyEdit(urwid.Edit):
    """An edit box that submits the entered duty on Enter."""

    def __init__(self, on_submit: Callable[[str], None]) -> None:
        super().__init__("Fan duty 0-100, Enter: ")
        self.on_submit = on_submit

    def keypress(self, size: tuple[int, ...], key: str) -> str | None:
        if key == "enter":
            self.on_submit(self.edit_text)
            return None
        return super().keypress(size, key)


class SimplePowerFanView(urwid.WidgetWrap):
    """Show power details and one direct fan-duty input."""

    def __init__(
        self,
        sources: list[object],
        fan_target: FanControlTarget | None,
        ipmitool_exe: str | None = None,
        apply_fn: Callable[..., None] = apply_fan_target,
    ) -> None:
        self.sources = sources
        self.fan_target = fan_target
        self.ipmitool_exe = ipmitool_exe
        self.apply_fn = apply_fn
        self.total_text = urwid.Text("Total: N/A")
        self.details_text = urwid.Text("")
        self.temp_text = urwid.Text("")
        self.status_text = urwid.Text("")
        self.duty_edit = DirectDutyEdit(self.on_duty_submit)

        rows = [
            urwid.Text("Power", align="center"),
            urwid.Divider(),
            self.total_text,
            urwid.Divider(),
            self.details_text,
            urwid.Divider(),
            self.temp_text,
            urwid.Divider(),
            self.duty_edit,
            urwid.Text("q: quit, a: auto, f: full"),
            self.status_text,
        ]
        super().__init__(urwid.Filler(urwid.Pile(rows), valign="top", top=1))

    def update_displayed_information(self) -> None:
        for source in self.sources:
            if not source.get_is_available():
                continue
            try:
                source.update()
            except (OSError, TypeError, ValueError):
                logging.debug("Failed to update %s", source, exc_info=True)

        totals = collect_power_totals(self.sources)
        self.total_text.set_text(f"Total: {format_power(totals.machine)}")
        self.details_text.set_text(
            format_power_details(self.sources, read_current_fan_percent())
        )
        self.temp_text.set_text(format_temperature_details(read_key_temperatures()))

    def _set_status(self, text: str, error: bool = False) -> None:
        self.status_text.set_text(("high temp txt", text) if error else text)

    def _apply_mode(self, mode: str, duty: int) -> None:
        if self.fan_target is None:
            self._set_status("fan control unavailable", error=True)
            return
        try:
            self.apply_fn(
                self.fan_target,
                mode,
                duty,
                ipmitool_exe=self.ipmitool_exe,
                min_duty=0,
            )
        except (OSError, ValueError) as err:
            self._set_status(str(err), error=True)
            return
        if mode == "manual":
            self._set_status(f"fan duty set to {duty}%")
        else:
            self._set_status(f"fan mode set to {mode}")

    def on_duty_submit(self, value: str) -> None:
        try:
            duty = parse_fan_duty(value)
        except ValueError as err:
            self._set_status(str(err), error=True)
            return
        self._apply_mode("manual", duty)
        self.duty_edit.set_edit_text("")

    def keypress(self, size: tuple[int, ...], key: str) -> str | None:
        if key == "q":
            raise urwid.ExitMainLoop()
        if key == "a":
            self._apply_mode("auto", 0)
            return None
        if key == "f":
            self._apply_mode("full", 100)
            return None
        return super().keypress(size, key)


def _refresh_seconds(value: str) -> float:
    try:
        refresh = float(value)
    except ValueError:
        return DEFAULT_REFRESH_SECONDS
    return max(refresh, 0.05)


def _build_power_sources() -> list[object]:
    sources = [RaplPowerSource(), ComponentPowerSource()]
    return [source for source in sources if source.get_is_available()]


def run_simple_power_fan_ui(args: object) -> None:
    """Run the minimal terminal interface."""
    sources = _build_power_sources()
    targets = []
    if getattr(args, "enable_fan_control", False):
        targets = discover_fan_control_targets(
            allow_unsafe=True,
            ipmi_vendor=getattr(args, "fan_control_vendor", "auto"),
        )
    fan_target = select_default_fan_target(targets)
    view = SimplePowerFanView(sources, fan_target)
    view.update_displayed_information()

    if not sys.stdin.isatty():
        print(view.total_text.get_text()[0])
        print(view.details_text.get_text()[0])
        print(view.temp_text.get_text()[0])
        return

    refresh_rate = _refresh_seconds(getattr(args, "refresh_rate", "0.5"))
    loop = urwid.MainLoop(view, handle_mouse=not getattr(args, "no_mouse", False))

    def tick(loop_: urwid.MainLoop, user_data: object | None = None) -> None:
        view.update_displayed_information()
        loop_.set_alarm_in(refresh_rate, tick)

    def exit_debug_run(loop_: urwid.MainLoop, user_data: object | None = None) -> None:
        raise urwid.ExitMainLoop()

    loop.set_alarm_in(refresh_rate, tick)
    if getattr(args, "debug_run", False):
        loop.set_alarm_in(8.0, exit_debug_run)
    loop.run()
