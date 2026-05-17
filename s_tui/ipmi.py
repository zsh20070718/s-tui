#!/usr/bin/env python
"""Helpers for invoking ipmitool consistently."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from collections.abc import Callable, Iterable

from s_tui.helper_functions import which

IPMITOOL_ARGS_ENV = "S_TUI_IPMITOOL_ARGS"
LOCAL_IPMI_DEVICE_PATHS = ("/dev/ipmi0", "/dev/ipmi/0", "/dev/ipmidev/0")
DEFAULT_SENSOR_CACHE_SECONDS = 0.5
BMC_DISCOVERY_TIMEOUT = 5.0
DEFAULT_LAN_CHANNELS = ("", "1", "8")

_ipmi_sensor_cache_key: tuple[str, ...] | None = None
_ipmi_sensor_cache_time = 0.0
_ipmi_sensor_cache_result: subprocess.CompletedProcess[str] | None = None
_IP_ADDRESS_RE = re.compile(r"^\s*IP Address\s*:\s*(\S+)\s*$", re.IGNORECASE)


def get_ipmitool_exe(ipmitool_exe: str | None = None) -> str | None:
    """Return an explicit or PATH-discovered ipmitool executable."""
    return ipmitool_exe or which("ipmitool")


def ipmitool_extra_args(env_value: str | None = None) -> list[str]:
    """Return extra ipmitool arguments from S_TUI_IPMITOOL_ARGS.

    This lets users route all IPMI reads and writes through a remote BMC, for
    example: S_TUI_IPMITOOL_ARGS='-I lanplus -H 10.0.0.5 -U ADMIN -E'.
    """
    value = os.environ.get(IPMITOOL_ARGS_ENV, "") if env_value is None else env_value
    if not value.strip():
        return []
    return shlex.split(value)


def build_ipmitool_command(
    subcommands: Iterable[str],
    ipmitool_exe: str | None = None,
    extra_args: Iterable[str] | None = None,
) -> list[str] | None:
    """Build an ipmitool command, including configured transport arguments."""
    exe = get_ipmitool_exe(ipmitool_exe)
    if exe is None:
        return None
    try:
        args = ipmitool_extra_args() if extra_args is None else list(extra_args)
    except ValueError as exc:
        raise OSError(f"Invalid {IPMITOOL_ARGS_ENV}: {exc}") from exc
    return [exe, *args, *subcommands]


def local_ipmi_devices() -> list[str]:
    """Return local kernel IPMI device nodes that currently exist."""
    return [path for path in LOCAL_IPMI_DEVICE_PATHS if os.path.exists(path)]


def local_ipmi_accessible_devices() -> list[str]:
    """Return local IPMI device nodes readable and writable by this process."""
    return [path for path in local_ipmi_devices() if os.access(path, os.R_OK | os.W_OK)]


def ipmi_access_configured() -> bool:
    """Return whether local or remote ipmitool access has been configured."""
    return bool(os.environ.get(IPMITOOL_ARGS_ENV, "").strip()) or bool(
        local_ipmi_accessible_devices()
    )


def run_ipmi_sensor(
    ipmitool_exe: str | None = None,
    extra_args: Iterable[str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout: float = 10.0,
    cache_seconds: float = DEFAULT_SENSOR_CACHE_SECONDS,
) -> subprocess.CompletedProcess[str] | None:
    """Run `ipmitool sensor`, sharing a short cache across sources."""
    global _ipmi_sensor_cache_key, _ipmi_sensor_cache_result, _ipmi_sensor_cache_time

    cmd = build_ipmitool_command(["sensor"], ipmitool_exe, extra_args)
    if cmd is None:
        return None

    key = tuple(cmd)
    now = time.monotonic()
    if (
        runner is subprocess.run
        and _ipmi_sensor_cache_key == key
        and _ipmi_sensor_cache_result is not None
        and now - _ipmi_sensor_cache_time < cache_seconds
    ):
        return _ipmi_sensor_cache_result

    result = runner(cmd, capture_output=True, text=True, timeout=timeout)
    if runner is subprocess.run:
        _ipmi_sensor_cache_key = key
        _ipmi_sensor_cache_result = result
        _ipmi_sensor_cache_time = time.monotonic()
    return result


def parse_bmc_ip_from_lan_print(output: str) -> str | None:
    """Parse the BMC IPv4 address from `ipmitool lan print` output."""
    for line in output.splitlines():
        match = _IP_ADDRESS_RE.match(line)
        if match is None:
            continue
        address = match.group(1).strip()
        if address and address not in {"0.0.0.0", "::"}:
            return address
    return None


def _lan_print_commands(exe: str, channels: Iterable[str]) -> list[list[str]]:
    commands = []
    for channel in channels:
        command = [exe, "lan", "print"]
        if channel:
            command.append(str(channel))
        commands.append(command)
    for command in list(commands):
        commands.append(["sudo", "-n", *command])
    for command in list(commands[: len(commands) // 2]):
        commands.append(["sudo", *command])
    return commands


def discover_bmc_ip(
    ipmitool_exe: str | None = None,
    channels: Iterable[str] = DEFAULT_LAN_CHANNELS,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str | None:
    """Discover this host's BMC IP through local IPMI LAN settings."""
    exe = get_ipmitool_exe(ipmitool_exe)
    if exe is None:
        return None

    for command in _lan_print_commands(exe, channels):
        try:
            result = runner(
                command,
                capture_output=True,
                text=True,
                timeout=BMC_DISCOVERY_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode != 0:
            continue
        address = parse_bmc_ip_from_lan_print(result.stdout)
        if address is not None:
            return address
    return None
