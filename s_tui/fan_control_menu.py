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

import contextlib
import glob
import http.cookiejar
import json
import logging
import os
import re
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

import urwid

from s_tui.helper_functions import cat, which
from s_tui.ipmi import discover_bmc_ip, ipmitool_extra_args
from s_tui.sturwid.ui_elements import ViListBox

MIN_FAN_DUTY = 0
MAX_FAN_DUTY = 100
IPMI_TIMEOUT = 5.0
BMC_TIMEOUT = 8.0
BMC_URL_ENV = "S_TUI_BMC_URL"
BMC_USERNAME_ENV = "S_TUI_BMC_USERNAME"
BMC_PASSWORD_ENV = "S_TUI_BMC_PASSWORD"
BMC_VERIFY_TLS_ENV = "S_TUI_BMC_VERIFY_TLS"
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
    bmc_base_url: str | None = None


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


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"", "0", "false", "no", "off"}


def _normalize_bmc_base_url(value: str) -> str:
    base_url = value.strip().rstrip("/")
    if not base_url:
        raise OSError(f"{BMC_URL_ENV} is empty")
    if not re.match(r"^https?://", base_url):
        base_url = f"https://{base_url}"
    return base_url


_discovered_bmc_base_url: str | None = None


def _discover_bmc_base_url() -> str:
    global _discovered_bmc_base_url

    if _discovered_bmc_base_url is not None:
        return _discovered_bmc_base_url

    address = discover_bmc_ip()
    if address is None:
        raise OSError(
            f"Set {BMC_URL_ENV}, or allow sudo for `ipmitool lan print`"
        )
    _discovered_bmc_base_url = _normalize_bmc_base_url(address)
    logging.info(
        "Discovered BMC URL %s via local IPMI LAN settings",
        _discovered_bmc_base_url,
    )
    return _discovered_bmc_base_url


def _configured_bmc_base_url(target: FanControlTarget | None) -> str | None:
    base_url = (target.bmc_base_url if target is not None else None) or os.environ.get(
        BMC_URL_ENV,
        "",
    )
    if base_url.strip():
        return _normalize_bmc_base_url(base_url)
    return None


def _candidate_bmc_base_urls(target: FanControlTarget | None) -> list[str]:
    candidates = []
    configured = _configured_bmc_base_url(target)
    if configured is not None:
        candidates.append(configured)

    try:
        discovered = _discover_bmc_base_url()
    except OSError:
        discovered = None
    if discovered is not None and discovered not in candidates:
        candidates.append(discovered)

    if candidates:
        return candidates
    raise OSError(
        f"Set {BMC_URL_ENV}, or allow sudo for `ipmitool lan print`"
    )


def _configured_or_discovered_bmc_base_url(target: FanControlTarget | None) -> str:
    configured = _configured_bmc_base_url(target)
    if configured is not None:
        return configured
    return _discover_bmc_base_url()


class _InspurBmcClient:
    def __init__(self, base_url: str, verify_tls: bool = False) -> None:
        context = None if verify_tls else ssl._create_unverified_context()
        self.base_url = _normalize_bmc_base_url(base_url)
        self.csrf_token: str | None = None
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        )

    def _request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> object:
        request_headers = {"Accept": "application/json"}
        if headers is not None:
            request_headers.update(headers)
        if self.csrf_token is not None:
            request_headers["X-CSRFTOKEN"] = self.csrf_token

        url = self.base_url + (path if path.startswith("/") else f"/{path}")
        request = urllib.request.Request(
            url,
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            with self.opener.open(request, timeout=BMC_TIMEOUT) as response:
                text = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise OSError(f"BMC {path} returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise OSError(f"BMC {path} failed: {exc.reason}") from exc

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise OSError(f"BMC {path} returned non-JSON data") from exc
        return payload

    def _request_dict(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        payload = self._request(method, path, body, headers)
        if not isinstance(payload, dict):
            raise OSError(f"BMC {path} returned unexpected data")
        return payload

    def form(
        self, method: str, path: str, payload: dict[str, object]
    ) -> dict[str, object]:
        body = urllib.parse.urlencode(payload).encode()
        return self._request_dict(
            method,
            path,
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    def json(
        self, method: str, path: str, payload: dict[str, object]
    ) -> dict[str, object]:
        body = json.dumps(payload).encode()
        return self._request_dict(
            method,
            path,
            body=body,
            headers={"Content-Type": "application/json"},
        )

    def get_json(self, path: str) -> dict[str, object]:
        return self._request_dict("GET", path)

    def get_payload(self, path: str) -> object:
        return self._request("GET", path)


_inspur_bmc_client_cache_key: tuple[str, str, str, bool] | None = None
_inspur_bmc_client_cache: _InspurBmcClient | None = None


def _inspur_bmc_credentials(
    target: FanControlTarget | None,
) -> tuple[str, str, str, bool]:
    username = os.environ.get(BMC_USERNAME_ENV, "")
    password = os.environ.get(BMC_PASSWORD_ENV, "")
    if not username or not password:
        raise OSError(
            f"Set {BMC_USERNAME_ENV} and {BMC_PASSWORD_ENV} to control Inspur BMC fans"
        )
    return (
        _configured_or_discovered_bmc_base_url(target),
        username,
        password,
        _env_flag(BMC_VERIFY_TLS_ENV, default=False),
    )


def _inspur_bmc_auth() -> tuple[str, str, bool]:
    username = os.environ.get(BMC_USERNAME_ENV, "")
    password = os.environ.get(BMC_PASSWORD_ENV, "")
    if not username or not password:
        raise OSError(
            f"Set {BMC_USERNAME_ENV} and {BMC_PASSWORD_ENV} to control Inspur BMC fans"
        )
    return username, password, _env_flag(BMC_VERIFY_TLS_ENV, default=False)


def _inspur_bmc_is_configured(target: FanControlTarget | None = None) -> bool:
    if not os.environ.get(BMC_USERNAME_ENV, "").strip():
        return False
    if not os.environ.get(BMC_PASSWORD_ENV, "").strip():
        return False
    try:
        _candidate_bmc_base_urls(target)
    except OSError:
        return False
    return True


def _login_inspur_bmc(
    target: FanControlTarget,
    client_factory: Callable[[str, bool], _InspurBmcClient] = _InspurBmcClient,
) -> _InspurBmcClient:
    username, password, verify_tls = _inspur_bmc_auth()
    errors = []

    for base_url in _candidate_bmc_base_urls(target):
        try:
            client = client_factory(base_url, verify_tls)
            random_tag = client.get_json("/api/randomtag")
            encrypt_ctrl = int(random_tag.get("encrypt_ctrl", 0))
            if encrypt_ctrl != 0:
                raise OSError(
                    "BMC requires encrypted login; this backend supports plain API login"
                )

            login = client.form(
                "POST",
                "/api/session",
                {
                    "encrypt_flag": encrypt_ctrl,
                    "username": username,
                    "password": password,
                    "login_tag": random_tag.get("random", ""),
                },
            )
            if login.get("ok") != 0:
                raise OSError(f"BMC login failed: {login.get('error', 'unknown error')}")
            csrf_token = login.get("CSRFToken")
            if not isinstance(csrf_token, str) or not csrf_token:
                raise OSError("BMC login did not return a CSRF token")
            client.csrf_token = csrf_token
            return client
        except OSError as exc:
            errors.append(f"{base_url}: {exc}")
            logging.debug("Failed to log in to BMC API at %s", base_url, exc_info=True)

    raise OSError(
        "BMC login failed for all candidate URLs: " + "; ".join(errors)
    )


def _cached_inspur_bmc_client(
    target: FanControlTarget | None = None,
) -> _InspurBmcClient:
    global _inspur_bmc_client_cache, _inspur_bmc_client_cache_key

    if target is None:
        target = FanControlTarget("bmc:inspur:all", "BMC Inspur all fans", "bmc-inspur")
    username, password, verify_tls = _inspur_bmc_auth()
    candidates = _candidate_bmc_base_urls(target)
    if _inspur_bmc_client_cache is not None:
        for base_url in candidates:
            if _inspur_bmc_client_cache_key == (base_url, username, password, verify_tls):
                return _inspur_bmc_client_cache

    _inspur_bmc_client_cache = _login_inspur_bmc(target)
    _inspur_bmc_client_cache_key = (
        _inspur_bmc_client_cache.base_url,
        username,
        password,
        verify_tls,
    )
    return _inspur_bmc_client_cache


def read_inspur_bmc_payload(path: str) -> object | None:
    """Read an Inspur BMC API payload when BMC credentials are configured."""
    global _inspur_bmc_client_cache, _inspur_bmc_client_cache_key

    if not _inspur_bmc_is_configured():
        return None
    try:
        return _cached_inspur_bmc_client().get_payload(path)
    except OSError:
        _inspur_bmc_client_cache = None
        _inspur_bmc_client_cache_key = None
        logging.debug("Failed to read BMC API %s", path, exc_info=True)
        return None


def build_ipmi_commands(
    target: FanControlTarget,
    mode: str,
    duty_percent: int,
    ipmitool_exe: str,
    min_duty: int = MIN_FAN_DUTY,
    ipmi_args: list[str] | None = None,
) -> list[list[str]]:
    """Build raw ipmitool commands for a supported BMC target."""
    args = [] if ipmi_args is None else ipmi_args

    def cmd(*subcommands: str) -> list[str]:
        return [ipmitool_exe, *args, *subcommands]

    if target.kind == "ipmi-dell":
        if mode == "auto":
            return [cmd("raw", "0x30", "0x30", "0x01", "0x01")]
        if mode in {"manual", "full"}:
            selected_duty = (
                MAX_FAN_DUTY
                if mode == "full"
                else _parse_duty_percent(duty_percent, min_duty)
            )
            return [
                cmd("raw", "0x30", "0x30", "0x01", "0x00"),
                cmd(
                    "raw",
                    "0x30",
                    "0x30",
                    "0x02",
                    "0xff",
                    _hex_byte(selected_duty),
                ),
            ]

    if target.kind == "ipmi-supermicro":
        zone = target.zone if target.zone is not None else 0
        if mode == "auto":
            return [cmd("raw", "0x30", "0x45", "0x01", "0x00")]
        if mode == "full":
            return [cmd("raw", "0x30", "0x45", "0x01", "0x01")]
        if mode == "manual":
            duty = _parse_duty_percent(duty_percent, min_duty)
            return [
                cmd("raw", "0x30", "0x45", "0x01", "0x01"),
                cmd(
                    "raw",
                    "0x30",
                    "0x70",
                    "0x66",
                    "0x01",
                    _hex_byte(zone),
                    _hex_byte(duty),
                ),
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


def _apply_inspur_bmc_target(
    target: FanControlTarget,
    mode: str,
    duty_percent: int,
    min_duty: int = MIN_FAN_DUTY,
    client_factory: Callable[[str, bool], _InspurBmcClient] = _InspurBmcClient,
) -> None:
    client = _login_inspur_bmc(target, client_factory=client_factory)

    if mode == "auto":
        client.json(
            "PUT",
            "/api/settings/fans-mode",
            {"control_mode": "auto", "cooling_mode": "normal"},
        )
        return

    if mode not in {"manual", "full"}:
        raise ValueError(f"Unsupported fan control mode '{mode}'")

    duty = (
        MAX_FAN_DUTY if mode == "full" else _parse_duty_percent(duty_percent, min_duty)
    )
    client.json(
        "PUT",
        "/api/settings/fans-mode",
        {"control_mode": "manual", "cooling_mode": "normal"},
    )
    fan_info = client.get_json("/api/status/fan_info")
    fans = fan_info.get("fans")
    if not isinstance(fans, list):
        raise OSError("BMC did not return a fan list")

    fan_ids = []
    for fan in fans:
        if not isinstance(fan, dict):
            continue
        if fan.get("is_disable") or fan.get("status") == "Absent":
            continue
        fan_id = fan.get("id")
        if fan_id is not None:
            fan_ids.append(fan_id)

    if not fan_ids:
        raise OSError("BMC returned no controllable fans")

    for fan_id in fan_ids:
        client.json("PUT", f"/api/settings/fan/{fan_id}", {"duty": duty})


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
        commands = build_ipmi_commands(
            target,
            mode,
            duty_percent,
            exe,
            min_duty,
            ipmi_args=ipmitool_extra_args(),
        )
        _run_ipmi_commands(commands, runner)
        return

    if target.kind == "hwmon":
        _apply_hwmon_target(target, mode, duty_percent, min_duty)
        return

    if target.kind == "bmc-inspur":
        _apply_inspur_bmc_target(target, mode, duty_percent, min_duty)
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
    ipmi_vendor: str = "auto",
) -> list[FanControlTarget]:
    forced_vendor = ipmi_vendor.strip().lower()
    identity = (
        forced_vendor if forced_vendor != "auto" else _read_dmi_identity(dmi_base_path)
    )
    targets = []
    if "inspur" in identity or "inagile" in identity:
        base_url = None
        with contextlib.suppress(OSError):
            base_url = _configured_or_discovered_bmc_base_url(None)
        label = "BMC Inspur all fans"
        if base_url:
            label = f"{label} ({base_url})"
        targets.append(
            FanControlTarget(
                "bmc:inspur:all",
                label,
                "bmc-inspur",
                bmc_base_url=base_url,
            )
        )

    exe = ipmitool_exe or which("ipmitool")
    if exe is None:
        return targets

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
    allow_unsafe: bool = False,
    ipmi_vendor: str = "auto",
) -> list[FanControlTarget]:
    """Return fan control targets only after explicit user opt-in."""
    if not allow_unsafe:
        return []
    targets = []
    targets.extend(_discover_hwmon_targets(hwmon_base_path))
    targets.extend(_discover_ipmi_targets(ipmitool_exe, dmi_base_path, ipmi_vendor))
    return targets


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
        self.duty_edit = urwid.IntEdit(f"Manual duty [% {min_duty}-100]: ", 50)

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
                urwid.Text(("high temp txt", "Manual duty accepts any integer 0-100.")),
                self.status_text,
            ]
        )

        set_duty_button = urwid.Button("Set Duty", on_press=self.on_set_duty)
        set_duty_button._label.align = "center"
        auto_button = urwid.Button("Auto", on_press=self.on_auto)
        auto_button._label.align = "center"
        full_button = urwid.Button("Full", on_press=self.on_full)
        full_button._label.align = "center"
        apply_button = urwid.Button("Apply Selected", on_press=self.on_apply)
        apply_button._label.align = "center"
        close_button = urwid.Button("Close", on_press=self.on_close)
        close_button._label.align = "center"
        self.titles.append(
            urwid.Columns(
                [set_duty_button, auto_button, full_button, apply_button, close_button]
            )
        )

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

    def _apply_mode(self, mode: str) -> None:
        target = self._get_selected_target()
        if target is None:
            self.status_text.set_text(("high temp txt", "No fan target selected"))
            return

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

    def on_set_duty(self, _: object) -> None:
        self._apply_mode("manual")

    def on_auto(self, _: object) -> None:
        self._apply_mode("auto")

    def on_full(self, _: object) -> None:
        self._apply_mode("full")

    def on_apply(self, _: object) -> None:
        self._apply_mode(self._get_selected_mode())

    def on_close(self, _: object) -> None:
        self.return_fn()
