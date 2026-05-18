"""Tests for the minimal power/fan TUI."""

import threading
import time
from types import SimpleNamespace

import pytest

from s_tui.fan_control_menu import FanControlTarget
from s_tui.simple_tui import (
    DirectDutyEdit,
    SimpleDisplaySampler,
    SimpleDisplaySnapshot,
    SimplePowerFanView,
    format_power_details,
    format_temperature_details,
    parse_fan_duty,
    read_current_fan_percent,
    read_key_temperatures,
    run_simple_power_fan_ui,
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


def _wait_for_snapshot(sampler):
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        snapshot = sampler.get_latest_snapshot()
        if snapshot is not None:
            return snapshot
        time.sleep(0.01)
    raise AssertionError("timed out waiting for sampler snapshot")


def test_simple_sampler_collects_off_main_thread(monkeypatch):
    main_thread_id = threading.get_ident()
    factory_thread_ids = []
    update_thread_ids = []
    updated = threading.Event()
    source = FakeSource()

    def source_factory():
        factory_thread_ids.append(threading.get_ident())
        return [source]

    def update():
        update_thread_ids.append(threading.get_ident())
        source.updated = True
        updated.set()

    source.update = update
    monkeypatch.setattr("s_tui.simple_tui.read_current_fan_percent", lambda: "58%")
    monkeypatch.setattr("s_tui.simple_tui.read_key_temperatures", list)
    sampler = SimpleDisplaySampler(source_factory, refresh_seconds=10.0)

    sampler.start()
    assert updated.wait(1.0)
    snapshot = _wait_for_snapshot(sampler)
    sampler.stop(timeout=0.2)

    assert factory_thread_ids == update_thread_ids
    assert factory_thread_ids[0] != main_thread_id
    assert snapshot.total_text == "Total: 576.0 W"


def test_simple_sampler_keeps_latest_snapshot_only():
    sampler = SimpleDisplaySampler(lambda: [], refresh_seconds=10.0)
    first = SimpleDisplaySnapshot("Total: 1.0 W", "first", "Temp:\n  N/A")
    second = SimpleDisplaySnapshot("Total: 2.0 W", "second", "Temp:\n  N/A")

    sampler._publish_snapshot(first)
    sampler._publish_snapshot(second)

    assert sampler.get_latest_snapshot() == second
    assert sampler.get_latest_snapshot() is None


def test_simple_sampler_publishes_stale_snapshot_on_failure(monkeypatch):
    calls = 0
    second_call = threading.Event()
    good = SimpleDisplaySnapshot("Total: 9.0 W", "good details", "good temp")

    def collect(sources):
        nonlocal calls
        calls += 1
        if calls == 1:
            return good
        second_call.set()
        raise RuntimeError("boom")

    monkeypatch.setattr("s_tui.simple_tui._collect_display_snapshot", collect)
    sampler = SimpleDisplaySampler(lambda: [], refresh_seconds=0.01)

    sampler.start()
    assert second_call.wait(1.0)
    deadline = time.monotonic() + 1.0
    snapshot = None
    while time.monotonic() < deadline:
        candidate = sampler.get_latest_snapshot()
        if candidate is not None and candidate.sensor_status_is_error:
            snapshot = candidate
            break
        time.sleep(0.01)
    sampler.stop(timeout=0.2)

    assert snapshot is not None
    assert snapshot.total_text == "Total: 9.0 W"
    assert snapshot.details_text == "good details"
    assert snapshot.sensor_status_text == "sampling error: boom"
    assert snapshot.sensor_status_is_error is True


def test_simple_sampler_publishes_empty_error_before_good_sample(monkeypatch):
    collected = threading.Event()

    def collect(sources):
        collected.set()
        raise RuntimeError("no data")

    monkeypatch.setattr("s_tui.simple_tui._collect_display_snapshot", collect)
    sampler = SimpleDisplaySampler(lambda: [], refresh_seconds=10.0)

    sampler.start()
    assert collected.wait(1.0)
    snapshot = _wait_for_snapshot(sampler)
    sampler.stop(timeout=0.2)

    assert snapshot.total_text == "Total: N/A"
    assert snapshot.details_text == "CPU:\n  N/A\nFan:\n  N/A\n  Percent: N/A\nGPU:\n  N/A"
    assert snapshot.temp_text == "Temp:\n  N/A"
    assert snapshot.sensor_status_text == "sampling error: no data"
    assert snapshot.sensor_status_is_error is True


def test_sampler_status_does_not_overwrite_fan_status():
    view = SimplePowerFanView([], None)
    view._set_status("fan duty set to 58%")

    view.apply_snapshot(
        SimpleDisplaySnapshot(
            "Total: 1.0 W",
            "details",
            "temp",
            sensor_status_text="sampling error: boom",
            sensor_status_is_error=True,
        )
    )

    assert _text(view.status_text) == "fan duty set to 58%"
    assert _text(view.sensor_status_text) == "sampling error: boom"


def test_simple_sampler_stop_returns_promptly_when_collection_blocks(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def collect(sources):
        entered.set()
        release.wait(1.0)
        return SimpleDisplaySnapshot("Total: 1.0 W", "details", "temp")

    monkeypatch.setattr("s_tui.simple_tui._collect_display_snapshot", collect)
    sampler = SimpleDisplaySampler(lambda: [], refresh_seconds=10.0)
    sampler.start()
    assert entered.wait(1.0)

    started = time.monotonic()
    sampler.stop(timeout=0.01)
    elapsed = time.monotonic() - started
    try:
        assert elapsed < 0.2
        assert sampler._thread is not None
        assert sampler._thread.is_alive()
    finally:
        release.set()
        sampler.stop(timeout=0.5)


def test_run_simple_power_fan_ui_non_tty_stays_synchronous(monkeypatch, capsys):
    source = FakeSource()
    monkeypatch.setattr(
        "s_tui.simple_tui.sys.stdin",
        SimpleNamespace(isatty=lambda: False),
    )
    monkeypatch.setattr("s_tui.simple_tui._build_power_sources", lambda: [source])
    monkeypatch.setattr("s_tui.simple_tui.read_current_fan_percent", lambda: "58%")
    monkeypatch.setattr("s_tui.simple_tui.read_key_temperatures", lambda: [("CPU0", 47.0)])

    run_simple_power_fan_ui(SimpleNamespace(enable_fan_control=False))

    assert source.updated is True
    assert capsys.readouterr().out == (
        "Total: 576.0 W\n"
        "CPU:\n"
        "  CPU: 260.0 W\n"
        "Fan:\n"
        "  Power: 108.0 W\n"
        "  Percent: 58%\n"
        "GPU:\n"
        "  GPU0: 87.4 W\n"
        "Temp:\n"
        "  CPU0: 47.0 C\n"
    )


def test_run_simple_power_fan_ui_tty_tick_uses_sampler_snapshot(monkeypatch):
    update_calls = []
    applied = []
    stopped = []
    snapshot = SimpleDisplaySnapshot("Total: 3.0 W", "details", "temp")

    class FakeSampler:
        def __init__(self, refresh_seconds):
            self.refresh_seconds = refresh_seconds

        def start(self):
            pass

        def stop(self, timeout):
            stopped.append(timeout)

        def get_latest_snapshot(self):
            return snapshot

    class FakeLoop:
        def __init__(self, view, handle_mouse):
            self.view = view
            self.alarms = []

        def set_alarm_in(self, seconds, callback):
            self.alarms.append((seconds, callback))

        def run(self):
            seconds, callback = self.alarms.pop(0)
            assert seconds == 0
            callback(self)

    def fail_update(self):
        update_calls.append(self)
        raise AssertionError("TTY tick must not sample synchronously")

    def record_snapshot(self, applied_snapshot):
        applied.append(applied_snapshot)

    monkeypatch.setattr(
        "s_tui.simple_tui.sys.stdin",
        SimpleNamespace(isatty=lambda: True),
    )
    monkeypatch.setattr("s_tui.simple_tui.discover_fan_control_targets", lambda **kwargs: [])
    monkeypatch.setattr("s_tui.simple_tui.SimpleDisplaySampler", FakeSampler)
    monkeypatch.setattr("s_tui.simple_tui.urwid.MainLoop", FakeLoop)
    monkeypatch.setattr(SimplePowerFanView, "update_displayed_information", fail_update)
    monkeypatch.setattr(SimplePowerFanView, "apply_snapshot", record_snapshot)

    run_simple_power_fan_ui(
        SimpleNamespace(
            enable_fan_control=False,
            refresh_rate="0.5",
            no_mouse=False,
            debug_run=False,
        )
    )

    assert update_calls == []
    assert applied == [snapshot]
    assert stopped == [1.0]
