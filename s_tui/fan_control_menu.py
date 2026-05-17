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

"""Fan control menu and backends."""

from __future__ import annotations

import glob
import logging
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

import urwid

from s_tui.helper_functions import cat, which
from s_tui.sturwid.ui_elements import ViListBox

MIN_FAN_DUTY = 20
MAX_FAN_DUTY = 100
IPMI_TIMEOUT = 5.0
_PWM_RE = re.compile(r"pwm(\d+)$")


@dataclass(frozen=True)
class FanControlTarget:
    """A fan control target exposed in the TUI."""

    target_id: str
    label: str
    kind: str
    zone: int | None = None
    pwm_path: str | None = None
    enable_path: str | None = None


def _read_text(path: str, fallback: str = "") -> str:
    try:
        return str(cat(path, fallback=fallback, binary=False)).strip()
    except OSError:
        return fallback


def _hex_byte(value: int) -> str:
    return f"0x{value:02x}"


def _parse_duty_percent(value: str | int, min_duty: int = MIN_FAN_DUTY) -> int:
    try:
        percent = int(str(value).strip())
    except ValueError as exc:
        raise ValueError("Fan duty must be an integer percent") from exc
    if percent < min_duty or percent > MAX_FAN_DUTY:
        raise ValueError(f"Fan duty must be between {min_duty} and {MAX_FAN_DUTY}%")
    return percent


def _percent_to_pwm(percent: int) -> int:
    return round(percent * 255 / 100)


def _write_text(path: str, value: str) -> None:
    with open(path, "w") as f:
        f.write(value)


def _run_ipmi_commands(
    commands: list[list[str]],
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    for cmd in commands:
        try:
            result = runner(
                cmd,
                capture_output=True,
                text=True,
                timeout=IPMI_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            raise OSError(f"{cmd[0]} timed out") from exc
        except OSError as exc:
            raise OSError(f"Failed to run {cmd[0]}: {exc}") from exc

        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise OSError(f"{' '.join(cmd)} failed: {detail}")


def build_ipmi_commands(
    target: FanControlTarget,
    mode: str,
    duty_percent: int,
    ipmitool_exe: str,
    min_duty: int = MIN_FAN_DUTY,
) -> list[list[str]]:
    """Build raw ipmitool commands for a supported BMC target."""
    if target.kind == "ipmi-dell":
        if mode == "auto":
            return [[ipmitool_exe, "raw", "0x30", "0x30", "0x01", "0x01"]]
        if mode in {"manual", "full"}:
            selected_duty = (
                MAX_FAN_DUTY
                if mode == "full"
                else _parse_duty_percent(duty_percent, min_duty)
            )
            return [
                [ipmitool_exe, "raw", "0x30", "0x30", "0x01", "0x00"],
                [
                    ipmitool_exe,
                    "raw",
                    "0x30",
                    "0x30",
                    "0x02",
                    "0xff",
                    _hex_byte(selected_duty),
                ],
            ]

    if target.kind == "ipmi-supermicro":
        zone = target.zone if target.zone is not None else 0
        if mode == "auto":
            return [[ipmitool_exe, "raw", "0x30", "0x45", "0x01", "0x00"]]
        if mode == "full":
            return [[ipmitool_exe, "raw", "0x30", "0x45", "0x01", "0x01"]]
        if mode == "manual":
            duty = _parse_duty_percent(duty_percent, min_duty)
            return [
                [ipmitool_exe, "raw", "0x30", "0x45", "0x01", "0x01"],
                [
                    ipmitool_exe,
                    "raw",
                    "0x30",
                    "0x70",
                    "0x66",
                    "0x01",
                    _hex_byte(zone),
                    _hex_byte(duty),
                ],
            ]

    raise ValueError(f"Unsupported fan control mode '{mode}' for {target.label}")


def _apply_hwmon_target(
    target: FanControlTarget,
    mode: str,
    duty_percent: int,
    min_duty: int = MIN_FAN_DUTY,
    writer: Callable[[str, str], None] = _write_text,
) -> None:
    if target.pwm_path is None:
        raise OSError("hwmon target has no PWM path")

    if mode == "auto":
        if target.enable_path is None:
            raise OSError("This hwmon target has no automatic mode control")
        writer(target.enable_path, "2")
        return

    if mode not in {"manual", "full"}:
        raise ValueError(f"Unsupported fan control mode '{mode}'")

    selected_duty = MAX_FAN_DUTY if mode == "full" else duty_percent
    duty = _parse_duty_percent(selected_duty, min_duty)
    if target.enable_path is not None:
        writer(target.enable_path, "1")
    writer(target.pwm_path, str(_percent_to_pwm(duty)))


def apply_fan_target(
    target: FanControlTarget,
    mode: str,
    duty_percent: int,
    ipmitool_exe: str | None = None,
    min_duty: int = MIN_FAN_DUTY,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    """Apply a fan mode to a target."""
    if target.kind.startswith("ipmi-"):
        exe = ipmitool_exe or which("ipmitool")
        if exe is None:
            raise OSError("ipmitool is not installed")
        commands = build_ipmi_commands(target, mode, duty_percent, exe, min_duty)
        _run_ipmi_commands(commands, runner)
        return

    if target.kind == "hwmon":
        _apply_hwmon_target(target, mode, duty_percent, min_duty)
        return

    raise ValueError(f"Unsupported fan control target '{target.kind}'")


def _discover_hwmon_targets(
    base_path: str = "/sys/class/hwmon",
) -> list[FanControlTarget]:
    targets = []
    for pwm_path in sorted(glob.glob(os.path.join(base_path, "hwmon*", "pwm*"))):
        match = _PWM_RE.search(os.path.basename(pwm_path))
        if match is None:
            continue
        pwm_id = match.group(1)
        enable_path = os.path.join(os.path.dirname(pwm_path), f"pwm{pwm_id}_enable")
        if not os.access(pwm_path, os.W_OK):
            continue
        if os.path.exists(enable_path) and not os.access(enable_path, os.W_OK):
            continue

        hwmon_dir = os.path.dirname(pwm_path)
        chip_name = _read_text(
            os.path.join(hwmon_dir, "name"), os.path.basename(hwmon_dir)
        )
        fan_label = _read_text(
            os.path.join(hwmon_dir, f"fan{pwm_id}_label"), f"pwm{pwm_id}"
        )
        targets.append(
            FanControlTarget(
                target_id=f"hwmon:{pwm_path}",
                label=f"hwmon:{chip_name}:{fan_label}",
                kind="hwmon",
                pwm_path=pwm_path,
                enable_path=enable_path if os.path.exists(enable_path) else None,
            )
        )
    return targets


def _read_dmi_identity(base_path: str = "/sys/class/dmi/id") -> str:
    values = []
    for filename in ("sys_vendor", "board_vendor", "product_name", "chassis_vendor"):
        value = _read_text(os.path.join(base_path, filename))
        if value:
            values.append(value.lower())
    return " ".join(values)


def _discover_ipmi_targets(
    ipmitool_exe: str | None = None,
    dmi_base_path: str = "/sys/class/dmi/id",
) -> list[FanControlTarget]:
    exe = ipmitool_exe or which("ipmitool")
    if exe is None:
        return []

    identity = _read_dmi_identity(dmi_base_path)
    targets = []
    if "dell" in identity:
        targets.append(FanControlTarget("ipmi:dell", "IPMI Dell iDRAC", "ipmi-dell"))
    if "supermicro" in identity or "super micro" in identity:
        targets.extend(
            [
                FanControlTarget(
                    "ipmi:supermicro:0",
                    "IPMI Supermicro zone 0",
                    "ipmi-supermicro",
                    zone=0,
                ),
                FanControlTarget(
                    "ipmi:supermicro:1",
                    "IPMI Supermicro zone 1",
                    "ipmi-supermicro",
                    zone=1,
                ),
            ]
        )
    return targets


def discover_fan_control_targets(
    ipmitool_exe: str | None = None,
    hwmon_base_path: str = "/sys/class/hwmon",
    dmi_base_path: str = "/sys/class/dmi/id",
) -> list[FanControlTarget]:
    """Return no targets; automatic fan control discovery is unsafe."""
    return []


class FanControlMenu:
    """Menu for changing fan speed."""

    MAX_TITLE_LEN = 70

    def __init__(
        self,
        return_fn: Callable[[], None],
        targets: list[FanControlTarget],
        ipmitool_exe: str | None = None,
        min_duty: int = MIN_FAN_DUTY,
    ) -> None:
        self.return_fn = return_fn
        self.targets = targets
        self.ipmitool_exe = ipmitool_exe
        self.min_duty = min_duty
        self.status_text = urwid.Text("")
        self.target_group: list[urwid.RadioButton] = []
        self.mode_group: list[urwid.RadioButton] = []
        self.mode_values: list[str] = []
        self.target_buttons: list[urwid.AttrMap] = []
        self.mode_buttons: list[urwid.AttrMap] = []
        self.duty_edit = urwid.Edit(f"Duty [% {min_duty}-100]: ", "50")

        self.titles: list[urwid.Widget] = []
        self._build_ui()
        self.main_window = urwid.LineBox(
            ViListBox(urwid.SimpleFocusListWalker(self.titles)),
            title="Fan Control",
        )

    def is_controllable(self) -> bool:
        return bool(self.targets)

    def _build_ui(self) -> None:
        self.titles = [urwid.Text(("bold text", "  Fan Control  \n"), "center")]

        self.titles.append(urwid.Text(("bold text", "Target"), align="center"))
        for idx, target in enumerate(self.targets):
            rb = urwid.RadioButton(self.target_group, target.label, state=(idx == 0))
            am = urwid.AttrMap(rb, "button normal", "button select")
            self.target_buttons.append(am)
            self.titles.append(am)

        self.titles.append(urwid.Divider())
        self.titles.append(urwid.Text(("bold text", "Mode"), align="center"))
        for label, mode in [
            ("Auto", "auto"),
            ("Manual duty", "manual"),
            ("Full speed", "full"),
        ]:
            rb = urwid.RadioButton(
                self.mode_group,
                label,
                state=(mode == "auto"),
                user_data=mode,
            )
            am = urwid.AttrMap(rb, "button normal", "button select")
            self.mode_buttons.append(am)
            self.mode_values.append(mode)
            self.titles.append(am)

        self.titles.extend(
            [
                self.duty_edit,
                urwid.Text(
                    (
                        "high temp txt",
                        "Manual mode writes fan controls immediately.",
                    )
                ),
                self.status_text,
            ]
        )

        apply_button = urwid.Button("Apply", on_press=self.on_apply)
        apply_button._label.align = "center"
        close_button = urwid.Button("Close", on_press=self.on_close)
        close_button._label.align = "center"
        self.titles.append(urwid.Columns([apply_button, close_button]))

    def get_size(self) -> tuple[int, int]:
        return len(self.titles) + 5, self.MAX_TITLE_LEN

    def refresh_state(self) -> None:
        self.status_text.set_text("")

    def _get_selected_target(self) -> FanControlTarget | None:
        for rb, target in zip(self.target_group, self.targets):
            if rb.state:
                return target
        return None

    def _get_selected_mode(self) -> str:
        for rb, mode in zip(self.mode_group, self.mode_values):
            if rb.state:
                return mode
        return "auto"

    def on_apply(self, _: object) -> None:
        target = self._get_selected_target()
        if target is None:
            self.status_text.set_text(("high temp txt", "No fan target selected"))
            return

        mode = self._get_selected_mode()
        duty_text = self.duty_edit.edit_text
        try:
            if mode == "manual":
                duty_percent = _parse_duty_percent(duty_text, self.min_duty)
            elif mode == "full":
                duty_percent = MAX_FAN_DUTY
            else:
                duty_percent = self.min_duty
            apply_fan_target(
                target,
                mode,
                duty_percent,
                ipmitool_exe=self.ipmitool_exe,
                min_duty=self.min_duty,
            )
        except (OSError, ValueError) as err:
            logging.debug("Failed to apply fan control: %s", err)
            self.status_text.set_text(("high temp txt", str(err)))
            return

        self.status_text.set_text(f"Applied {mode} to {target.label}")

    def on_close(self, _: object) -> None:
        self.return_fn()
