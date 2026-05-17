"""Tests for FanSource with mocked psutil and IPMI inputs."""

from subprocess import CompletedProcess

import pytest

from s_tui.sources.fan_source import (
    FanReading,
    FanSource,
    _read_inspur_bmc_fans,
    _read_ipmi_fans,
)
from tests.conftest import SensorFan, make_fans_dict


def _completed(stdout="", stderr="", returncode=0):
    return CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def mock_sensors_fans(monkeypatch):
    fans = make_fans_dict(count=1)
    monkeypatch.setattr("psutil.sensors_fans", lambda: fans)
    return fans


class TestFanSourceInit:
    def test_name(self, mock_sensors_fans):
        src = FanSource()
        assert src.get_source_name() == "Fan"

    def test_measurement_unit(self, mock_sensors_fans):
        src = FanSource()
        assert src.get_measurement_unit() == "RPM"

    def test_is_available(self, mock_sensors_fans):
        src = FanSource()
        assert src.get_is_available() is True

    def test_sensor_list(self, mock_sensors_fans):
        src = FanSource()
        assert len(src.get_sensor_list()) == 1

    def test_pallet(self, mock_sensors_fans):
        src = FanSource()
        assert "fan" in src.get_pallet()[0]


class TestFanSourceUpdate:
    def test_update_populates_values(self, mock_sensors_fans):
        src = FanSource()
        src.update()
        readings = src.get_reading_list()
        assert readings[0] == 1200

    def test_keeps_server_fan_speeds_above_10k_rpm(self, monkeypatch):
        fans = {
            "hw": [
                SensorFan(label="f0", current=1200),
                SensorFan(label="f1", current=23000),
            ]
        }
        monkeypatch.setattr("psutil.sensors_fans", lambda: fans)
        src = FanSource()
        src.update()
        readings = src.get_reading_list()
        assert len(readings) == 2
        assert readings[0] == 1200
        assert readings[1] == 23000
        assert src.sensor_available[0] is True
        assert src.sensor_available[1] is True

    def test_filters_unreasonable_speeds(self, monkeypatch):
        fans = {"hw": [SensorFan(label="f0", current=99999)]}
        monkeypatch.setattr("psutil.sensors_fans", lambda: fans)
        src = FanSource()

        src.update()

        assert src.sensor_available[0] is False

    def test_edge_triggered_always_false(self, mock_sensors_fans):
        src = FanSource()
        assert src.get_edge_triggered() is False

    def test_get_top(self, mock_sensors_fans):
        src = FanSource()
        assert src.get_top() == 1


class TestFanSourceUnavailable:
    def test_no_fans(self, monkeypatch):
        monkeypatch.setattr("psutil.sensors_fans", lambda: {})
        monkeypatch.setattr("s_tui.sources.fan_source._read_ipmi_fans", lambda: [])
        src = FanSource()
        assert src.get_is_available() is False

    def test_attribute_error(self, monkeypatch):
        def raise_attribute_error():
            raise AttributeError

        monkeypatch.setattr("psutil.sensors_fans", raise_attribute_error)
        monkeypatch.setattr("s_tui.sources.fan_source._read_ipmi_fans", lambda: [])
        src = FanSource()
        assert src.get_is_available() is False


def test_read_ipmi_fans_parses_rpm_rows(monkeypatch):
    monkeypatch.setattr("s_tui.sources.fan_source.ipmi_access_configured", lambda: True)

    def runner(cmd, **kwargs):
        return _completed(
            stdout=(
                "FAN1 | 7200.000 | RPM | ok\n"
                "PS1 Fan | 9800.000 | RPM | ok\n"
                "Temp | 35.000 | degrees C | ok\n"
            )
        )

    readings = _read_ipmi_fans("/usr/bin/ipmitool", runner)

    assert readings == [
        FanReading("IPMI:FAN1", 7200),
        FanReading("IPMI:PS1 Fan", 9800),
    ]


def test_read_inspur_bmc_fans_parses_fan_info():
    payload = {
        "fans": [
            {"fan_name": "FAN0_F_Speed", "speed_rpm": 13300, "status": "OK"},
            {"fan_name": "FAN0_R_Speed", "speed_rpm": 12600, "status": "OK"},
            {"fan_name": "FAN1_F_Speed", "speed_rpm": "N/A", "status": "OK"},
            {"fan_name": "FAN1_R_Speed", "speed_rpm": 0, "status": "Absent"},
        ]
    }

    readings = _read_inspur_bmc_fans(lambda path: payload)

    assert readings == [
        FanReading("BMC:FAN0_F_Speed", 13300),
        FanReading("BMC:FAN0_R_Speed", 12600),
    ]
