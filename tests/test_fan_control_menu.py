"""Tests for fan control command generation and backends."""

import pytest

from s_tui.fan_control_menu import (
    FanControlTarget,
    _apply_hwmon_target,
    _discover_ipmi_targets,
    _parse_duty_percent,
    apply_fan_target,
    build_ipmi_commands,
    discover_fan_control_targets,
)


def test_parse_duty_percent_enforces_minimum():
    assert _parse_duty_percent("35", min_duty=20) == 35
    with pytest.raises(ValueError, match="between 20 and 100"):
        _parse_duty_percent("10", min_duty=20)


def test_build_dell_manual_commands():
    target = FanControlTarget("ipmi:dell", "IPMI Dell iDRAC", "ipmi-dell")

    commands = build_ipmi_commands(target, "manual", 45, "/usr/bin/ipmitool")

    assert commands == [
        ["/usr/bin/ipmitool", "raw", "0x30", "0x30", "0x01", "0x00"],
        [
            "/usr/bin/ipmitool",
            "raw",
            "0x30",
            "0x30",
            "0x02",
            "0xff",
            "0x2d",
        ],
    ]


def test_build_supermicro_zone_manual_commands():
    target = FanControlTarget(
        "ipmi:supermicro:1",
        "IPMI Supermicro zone 1",
        "ipmi-supermicro",
        zone=1,
    )

    commands = build_ipmi_commands(target, "manual", 60, "ipmitool")

    assert commands == [
        ["ipmitool", "raw", "0x30", "0x45", "0x01", "0x01"],
        [
            "ipmitool",
            "raw",
            "0x30",
            "0x70",
            "0x66",
            "0x01",
            "0x01",
            "0x3c",
        ],
    ]


def test_apply_ipmi_target_runs_commands():
    target = FanControlTarget("ipmi:dell", "IPMI Dell iDRAC", "ipmi-dell")
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    apply_fan_target(
        target,
        "auto",
        20,
        ipmitool_exe="/usr/bin/ipmitool",
        runner=runner,
    )

    assert calls == [["/usr/bin/ipmitool", "raw", "0x30", "0x30", "0x01", "0x01"]]


def test_apply_hwmon_manual_writes_enable_and_pwm():
    target = FanControlTarget(
        "hwmon:/sys/class/hwmon/hwmon0/pwm1",
        "hwmon:nct6775:pwm1",
        "hwmon",
        pwm_path="/sys/class/hwmon/hwmon0/pwm1",
        enable_path="/sys/class/hwmon/hwmon0/pwm1_enable",
    )
    calls = []

    _apply_hwmon_target(
        target, "manual", 50, writer=lambda path, val: calls.append((path, val))
    )

    assert calls == [
        ("/sys/class/hwmon/hwmon0/pwm1_enable", "1"),
        ("/sys/class/hwmon/hwmon0/pwm1", "128"),
    ]


def test_discover_ipmi_targets_uses_dmi_vendor(tmp_path):
    (tmp_path / "sys_vendor").write_text("Supermicro")

    targets = _discover_ipmi_targets(
        ipmitool_exe="/usr/bin/ipmitool",
        dmi_base_path=str(tmp_path),
    )

    assert [target.target_id for target in targets] == [
        "ipmi:supermicro:0",
        "ipmi:supermicro:1",
    ]


def test_discover_ipmi_targets_hides_unknown_vendor(tmp_path):
    (tmp_path / "sys_vendor").write_text("Generic Server")

    targets = _discover_ipmi_targets(
        ipmitool_exe="/usr/bin/ipmitool",
        dmi_base_path=str(tmp_path),
    )

    assert targets == []


def test_discover_fan_control_targets_is_disabled(tmp_path):
    (tmp_path / "hwmon0").mkdir()
    (tmp_path / "hwmon0" / "pwm1").write_text("255")

    targets = discover_fan_control_targets(
        ipmitool_exe="/usr/bin/ipmitool",
        hwmon_base_path=str(tmp_path),
        dmi_base_path=str(tmp_path),
    )

    assert targets == []
