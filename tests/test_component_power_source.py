"""Tests for component power providers."""

from subprocess import CompletedProcess

from s_tui.sources.component_power_source import (
    ComponentPowerSource,
    PowerReading,
    _read_hwmon_power,
    _read_ipmi_dcmi_power,
    _read_ipmi_sensor_power,
    _read_nvidia_power,
)


def _completed(stdout="", stderr="", returncode=0):
    return CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def test_read_hwmon_power(tmp_path):
    hwmon = tmp_path / "hwmon0"
    hwmon.mkdir()
    (hwmon / "name").write_text("amdgpu")
    (hwmon / "power1_label").write_text("PPT")
    (hwmon / "power1_input").write_text("123456000")

    readings = _read_hwmon_power(str(tmp_path))

    assert readings == [PowerReading("hwmon:amdgpu:PPT", 123.456)]


def test_read_nvidia_power_parses_csv():
    def runner(cmd, **kwargs):
        return _completed(
            stdout=(
                "0, NVIDIA A100-PCIE-40GB, 250.55\n"
                "1, NVIDIA A100-PCIE-40GB, [Not Supported]\n"
            )
        )

    readings = _read_nvidia_power("/usr/bin/nvidia-smi", runner)

    assert readings == [PowerReading("GPU0:NVIDIA A100-PCIE-40GB", 250.55)]


def test_read_ipmi_sensor_power_parses_watt_rows():
    def runner(cmd, **kwargs):
        return _completed(
            stdout=(
                "Temp | 35.000 | degrees C | ok\n"
                "Pwr Consumption | 416.000 | Watts | ok\n"
                "PS1 Input | na | Watts | ns\n"
            )
        )

    readings = _read_ipmi_sensor_power("/usr/bin/ipmitool", runner)

    assert readings == [PowerReading("IPMI:Pwr Consumption", 416.0)]


def test_read_ipmi_dcmi_power_parses_instantaneous_reading():
    def runner(cmd, **kwargs):
        return _completed(
            stdout=(
                "Instantaneous power reading:                   287 Watts\n"
                "Minimum during sampling period:               264 Watts\n"
            )
        )

    readings = _read_ipmi_dcmi_power("/usr/bin/ipmitool", runner)

    assert readings == [PowerReading("IPMI:DCMI", 287.0)]


def test_component_power_source_updates_and_marks_missing(mocker):
    samples = iter(
        [
            [PowerReading("GPU0:NVIDIA A100", 120.0)],
            [PowerReading("GPU0:NVIDIA A100", 155.0)],
            [],
        ]
    )
    mocker.patch(
        "s_tui.sources.component_power_source._probe_power_readings",
        side_effect=lambda: next(samples),
    )

    source = ComponentPowerSource()
    assert source.get_is_available() is True
    assert source.get_sensor_list() == ["GPU0:NVIDIA A100"]
    assert source.get_reading_list() == [120.0]

    source.update()
    assert source.get_reading_list() == [155.0]
    assert source.get_maximum() == 155.0

    source.update()
    assert source.sensor_available == [False]
