"""Tests for fan control command generation and backends."""

from typing import ClassVar

import pytest

from s_tui.fan_control_menu import (
    BMC_PASSWORD_ENV,
    BMC_URL_ENV,
    BMC_USERNAME_ENV,
    FanControlMenu,
    FanControlTarget,
    _apply_hwmon_target,
    _apply_inspur_bmc_target,
    _discover_ipmi_targets,
    _parse_duty_percent,
    apply_fan_target,
    build_ipmi_commands,
    discover_fan_control_targets,
)
from s_tui.ipmi import IPMITOOL_ARGS_ENV


class FakeInspurBmcClient:
    instances: ClassVar[list] = []

    def __init__(self, base_url, verify_tls):
        self.base_url = base_url
        self.verify_tls = verify_tls
        self.csrf_token = None
        self.calls = []
        self.instances.append(self)

    def get_json(self, path):
        self.calls.append(("GET", path))
        if path == "/api/randomtag":
            return {"random": 1234, "encrypt_ctrl": 0}
        if path == "/api/status/fan_info":
            return {
                "fans": [
                    {"id": 1, "status": "OK"},
                    {"id": 2, "status": "Absent"},
                    {"id": 3, "status": "OK", "is_disable": True},
                    {"id": 4, "status": "OK"},
                ]
            }
        raise AssertionError(f"unexpected GET {path}")

    def form(self, method, path, payload):
        self.calls.append((method, path, payload))
        return {"ok": 0, "CSRFToken": "csrf-token"}

    def json(self, method, path, payload):
        self.calls.append((method, path, payload))
        return {}


def test_parse_duty_percent_enforces_minimum():
    assert _parse_duty_percent("0") == 0
    assert _parse_duty_percent("35", min_duty=20) == 35
    with pytest.raises(ValueError, match="between 20 and 100"):
        _parse_duty_percent("10", min_duty=20)


def test_fan_control_menu_set_duty_accepts_zero(monkeypatch):
    target = FanControlTarget("bmc:inspur:all", "BMC Inspur all fans", "bmc-inspur")
    calls = []

    def apply(target, mode, duty_percent, **kwargs):
        calls.append((target.target_id, mode, duty_percent, kwargs["min_duty"]))

    monkeypatch.setattr("s_tui.fan_control_menu.apply_fan_target", apply)
    menu = FanControlMenu(lambda: None, [target], min_duty=0)
    menu.duty_edit.set_edit_text("0")

    menu.on_set_duty(None)

    assert calls == [("bmc:inspur:all", "manual", 0, 0)]


def test_fan_control_menu_rejects_out_of_range_duty(monkeypatch):
    target = FanControlTarget("bmc:inspur:all", "BMC Inspur all fans", "bmc-inspur")

    def apply(*args, **kwargs):
        raise AssertionError("should not write fan duty")

    monkeypatch.setattr("s_tui.fan_control_menu.apply_fan_target", apply)
    menu = FanControlMenu(lambda: None, [target], min_duty=0)
    menu.duty_edit.set_edit_text("101")

    menu.on_set_duty(None)

    assert "between 0 and 100" in menu.status_text.get_text()[0]


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


def test_apply_ipmi_target_uses_env_args(monkeypatch):
    monkeypatch.setenv(IPMITOOL_ARGS_ENV, "-I lanplus -H bmc -U admin -E")
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

    assert calls == [
        [
            "/usr/bin/ipmitool",
            "-I",
            "lanplus",
            "-H",
            "bmc",
            "-U",
            "admin",
            "-E",
            "raw",
            "0x30",
            "0x30",
            "0x01",
            "0x01",
        ]
    ]


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


def test_apply_inspur_auto_uses_bmc_api(monkeypatch):
    FakeInspurBmcClient.instances.clear()
    monkeypatch.setenv(BMC_URL_ENV, "10.16.180.2")
    monkeypatch.setenv(BMC_USERNAME_ENV, "admin")
    monkeypatch.setenv(BMC_PASSWORD_ENV, "admin")
    target = FanControlTarget("bmc:inspur:all", "BMC Inspur all fans", "bmc-inspur")

    _apply_inspur_bmc_target(
        target,
        "auto",
        20,
        client_factory=FakeInspurBmcClient,
    )

    client = FakeInspurBmcClient.instances[-1]
    assert client.base_url == "https://10.16.180.2"
    assert client.verify_tls is False
    assert client.csrf_token == "csrf-token"
    assert client.calls == [
        ("GET", "/api/randomtag"),
        (
            "POST",
            "/api/session",
            {
                "encrypt_flag": 0,
                "username": "admin",
                "password": "admin",
                "login_tag": 1234,
            },
        ),
        (
            "PUT",
            "/api/settings/fans-mode",
            {"control_mode": "auto", "cooling_mode": "normal"},
        ),
    ]


def test_apply_inspur_manual_sets_all_controllable_fans(monkeypatch):
    FakeInspurBmcClient.instances.clear()
    monkeypatch.setenv(BMC_URL_ENV, "https://bmc.example")
    monkeypatch.setenv(BMC_USERNAME_ENV, "admin")
    monkeypatch.setenv(BMC_PASSWORD_ENV, "admin")
    target = FanControlTarget("bmc:inspur:all", "BMC Inspur all fans", "bmc-inspur")

    _apply_inspur_bmc_target(
        target,
        "manual",
        50,
        client_factory=FakeInspurBmcClient,
    )

    client = FakeInspurBmcClient.instances[-1]
    assert client.calls[-4:] == [
        (
            "PUT",
            "/api/settings/fans-mode",
            {"control_mode": "manual", "cooling_mode": "normal"},
        ),
        ("GET", "/api/status/fan_info"),
        ("PUT", "/api/settings/fan/1", {"duty": 50}),
        ("PUT", "/api/settings/fan/4", {"duty": 50}),
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


def test_discover_fan_control_targets_requires_explicit_opt_in(tmp_path):
    (tmp_path / "sys_vendor").write_text("Supermicro")

    targets = discover_fan_control_targets(
        ipmitool_exe="/usr/bin/ipmitool",
        hwmon_base_path=str(tmp_path),
        dmi_base_path=str(tmp_path),
        allow_unsafe=True,
    )

    assert [target.target_id for target in targets] == [
        "ipmi:supermicro:0",
        "ipmi:supermicro:1",
    ]


def test_discover_fan_control_targets_can_force_vendor(tmp_path):
    (tmp_path / "sys_vendor").write_text("NULL")

    targets = discover_fan_control_targets(
        ipmitool_exe="/usr/bin/ipmitool",
        hwmon_base_path=str(tmp_path),
        dmi_base_path=str(tmp_path),
        allow_unsafe=True,
        ipmi_vendor="dell",
    )

    assert [target.target_id for target in targets] == ["ipmi:dell"]


def test_discover_fan_control_targets_can_force_inspur(tmp_path):
    (tmp_path / "sys_vendor").write_text("NULL")

    targets = discover_fan_control_targets(
        ipmitool_exe=None,
        hwmon_base_path=str(tmp_path),
        dmi_base_path=str(tmp_path),
        allow_unsafe=True,
        ipmi_vendor="inspur",
    )

    assert [target.target_id for target in targets] == ["bmc:inspur:all"]
    assert targets[0].kind == "bmc-inspur"
