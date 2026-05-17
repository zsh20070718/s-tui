"""Tests for the minimal power/fan TUI."""

import pytest

from s_tui.fan_control_menu import FanControlTarget
from s_tui.simple_tui import (
    DirectDutyEdit,
    SimplePowerFanView,
    format_power_details,
    format_temperature_details,
    parse_fan_duty,
    read_current_fan_percent,
    read_key_temperatures,
    select_default_fan_target,
)


class FakeSource:
    def __init__(self):
        self.sensor_available = [True, True, True, True]
        self.updated = False

    def get_is_available(self):
        return True

    def update(self):
        self.updated = True

    def get_source_name(self):
        return "CompPower"

    def get_sensor_list(self):
        return [
            "BMC:Total_Power",
            "BMC:Fan_Power",
            "BMC:CPU_Power",
            "GPU0:NVIDIA A800",
        ]

    def get_reading_list(self):
        return [576.0, 108.0, 260.0, 87.4]


def _text(widget):
    return widget.get_text()[0]


def test_parse_fan_duty_accepts_integer_percent():
    assert parse_fan_duty("58") == 58
    assert parse_fan_duty(" 0 ") == 0
    assert parse_fan_duty("100") == 100


def test_parse_fan_duty_rejects_invalid_values():
    for value in ("", "abc", "-1", "101"):
        with pytest.raises(ValueError):
            parse_fan_duty(value)


def test_select_default_fan_target_prefers_inspur_bmc():
    hwmon = FanControlTarget("hwmon:x", "hwmon", "hwmon")
    bmc = FanControlTarget("bmc:inspur:all", "bmc", "bmc-inspur")

    assert select_default_fan_target([hwmon, bmc]) == bmc


def test_format_power_details_groups_cpu_fan_gpu_and_percent():
    source = FakeSource()

    details = format_power_details([source], "58%")

    assert details == "\n".join(
        [
            "CPU:",
            "  CPU: 260.0 W",
            "Fan:",
            "  Power: 108.0 W",
            "  Percent: 58%",
            "GPU:",
            "  GPU0: 87.4 W",
        ]
    )


def test_read_current_fan_percent_collapses_matching_values():
    payload = {
        "fans": [
            {"speed_percent": 58, "status": "OK"},
            {"speed_percent": "58", "status": "OK"},
            {"speed_percent": 100, "status": "Absent"},
        ]
    }

    assert read_current_fan_percent(lambda path: payload) == "58%"


def test_read_current_fan_percent_reports_range_for_mixed_values():
    payload = {"fans": [{"speed_percent": 55}, {"speed_percent": 60}]}

    assert read_current_fan_percent(lambda path: payload) == "55-60%"


def test_read_key_temperatures_keeps_important_bmc_temps():
    payload = [
        {"name": "Inlet_Temp", "unit": "deg_c", "reading": 24, "accessible": 0},
        {"name": "Outlet_Temp", "unit": "deg_c", "reading": 32, "accessible": 0},
        {"name": "CPU0_Temp", "unit": "deg_c", "reading": 47, "accessible": 0},
        {"name": "GPU0_Temp", "unit": "deg_c", "reading": 36, "accessible": 0},
        {"name": "GPU0_Mem_Temp", "unit": "deg_c", "reading": 50, "accessible": 0},
        {"name": "NVME_Temp", "unit": "deg_c", "reading": 0, "accessible": 213},
    ]

    assert read_key_temperatures(lambda path: payload) == [
        ("Inlet", 24.0),
        ("Outlet", 32.0),
        ("CPU0", 47.0),
        ("GPU0", 36.0),
    ]


def test_format_temperature_details():
    assert format_temperature_details([]) == "Temp:\n  N/A"
    assert format_temperature_details([("CPU0", 47), ("GPU0", 36.5)]) == (
        "Temp:\n  CPU0: 47.0 C\n  GPU0: 36.5 C"
    )


def test_simple_view_shows_total_and_detail_groups(monkeypatch):
    source = FakeSource()
    monkeypatch.setattr("s_tui.simple_tui.read_current_fan_percent", lambda: "58%")
    monkeypatch.setattr(
        "s_tui.simple_tui.read_key_temperatures",
        lambda: [("Inlet", 24.0), ("CPU0", 47.0), ("GPU0", 36.0)],
    )
    view = SimplePowerFanView([source], None)

    view.update_displayed_information()

    assert source.updated is True
    assert _text(view.total_text) == "Total: 576.0 W"
    assert _text(view.details_text) == "\n".join(
        [
            "CPU:",
            "  CPU: 260.0 W",
            "Fan:",
            "  Power: 108.0 W",
            "  Percent: 58%",
            "GPU:",
            "  GPU0: 87.4 W",
        ]
    )
    assert _text(view.temp_text) == "\n".join(
        [
            "Temp:",
            "  Inlet: 24.0 C",
            "  CPU0: 47.0 C",
            "  GPU0: 36.0 C",
        ]
    )


def test_direct_duty_input_applies_without_select_or_apply():
    target = FanControlTarget("bmc:inspur:all", "bmc", "bmc-inspur")
    calls = []

    def apply_fn(*args, **kwargs):
        calls.append((args, kwargs))

    view = SimplePowerFanView([], target, apply_fn=apply_fn)

    view.on_duty_submit("58")

    assert calls == [
        (
            (target, "manual", 58),
            {"ipmitool_exe": None, "min_duty": 0},
        )
    ]
    assert _text(view.status_text) == "fan duty set to 58%"


def test_direct_duty_edit_submits_on_enter():
    submissions = []
    edit = DirectDutyEdit(submissions.append)
    edit.set_edit_text("65")

    result = edit.keypress((20,), "enter")

    assert result is None
    assert submissions == ["65"]
