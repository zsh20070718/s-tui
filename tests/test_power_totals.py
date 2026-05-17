"""Tests for aggregate power totals."""

from s_tui.power_totals import collect_power_totals, format_power


class FakeSource:
    def __init__(self, name, sensors, readings, available=None):
        self.name = name
        self.sensors = sensors
        self.readings = readings
        self.sensor_available = (
            available if available is not None else [True] * len(sensors)
        )

    def get_source_name(self):
        return self.name

    def get_sensor_list(self):
        return self.sensors

    def get_reading_list(self):
        return self.readings


def test_machine_power_prefers_total_sensor_over_dcmi():
    source = FakeSource(
        "CompPower",
        ["IPMI:Pwr Consumption", "IPMI:DCMI", "GPU0:NVIDIA H200"],
        [410.0, 405.0, 75.0],
    )

    totals = collect_power_totals([source])

    assert totals.machine == 410.0


def test_machine_power_sums_psu_inputs_when_no_total_sensor():
    source = FakeSource(
        "CompPower",
        ["IPMI:PS1 Input", "IPMI:PS2 Input"],
        [300.0, 280.0],
    )

    totals = collect_power_totals([source])

    assert totals.machine == 580.0


def test_fan_power_uses_only_fan_watt_sensors():
    source = FakeSource(
        "CompPower",
        ["IPMI:Fan Board Power", "IPMI:System Power"],
        [12.5, 420.0],
    )

    totals = collect_power_totals([source])

    assert totals.fan == 12.5
    assert totals.machine == 420.0


def test_cpu_gpu_power_uses_rapl_package_and_gpu_readings():
    rapl = FakeSource(
        "Power",
        ["package-0", "core", "package-1"],
        [180.0, 45.0, 170.0],
    )
    component = FakeSource(
        "CompPower",
        [
            "GPU0:NVIDIA H200",
            "hwmon:nvidia:power1",
            "hwmon:amdgpu:PPT",
        ],
        [75.0, 75.0, 50.0],
    )

    totals = collect_power_totals([rapl, component])

    assert totals.cpu_gpu == 475.0


def test_cpu_gpu_power_falls_back_to_component_cpu():
    source = FakeSource(
        "CompPower",
        ["IPMI:CPU Socket 1 Power", "GPU0:NVIDIA A100"],
        [120.0, 250.0],
    )

    totals = collect_power_totals([source])

    assert totals.cpu_gpu == 370.0


def test_unavailable_and_invalid_readings_are_ignored():
    source = FakeSource(
        "CompPower",
        ["IPMI:DCMI", "GPU0:NVIDIA A100", "IPMI:Fan Power"],
        [500.0, "bad", 20.0],
        [False, True, True],
    )

    totals = collect_power_totals([source])

    assert totals.machine is None
    assert totals.cpu_gpu is None
    assert totals.fan == 20.0


def test_format_power():
    assert format_power(None) == "N/A"
    assert format_power(123.456) == "123.5 W"
