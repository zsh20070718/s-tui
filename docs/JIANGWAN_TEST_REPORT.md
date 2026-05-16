# Jiangwan Test Report

测试日期：2026-05-16。

本文档记录本版本 `s-tui` 在江湾机器上的实际运行情况。测试目标是确认新增的组件功率监控和风扇控制入口是否能在真实服务器上运行，并明确哪些限制来自硬件、驱动或权限。

## 代码位置

`fdu-cpu-1` 和 `fdu-gpu-1` 均使用以下目录：

```bash
/mnt/data/personal/zsh/toys/s-tui2
```

`fdu-gpu-1` 上可用测试环境：

```bash
/tmp/s-tui2-venv-gpu
```

## 本地基线

在开发机本地完成的最终验证：

```text
ruff check: passed
targeted tests: 28 passed
non-hardware tests: 401 passed, 15 deselected
```

其中 targeted tests 包括：

- `tests/test_component_power_source.py`
- `tests/test_fan_control_menu.py`
- `tests/test_cli.py`

## fdu-cpu-1

### 基本信息

```text
SSH target: fdu-cpu-1
Hostname observed: fdusc-cpu-1
User: zsh
Target path: /mnt/data/personal/zsh/toys/s-tui2
```

### 通过的项目

远端测试曾完整跑通：

```text
ruff check: passed
targeted tests: 26 passed
non-hardware tests: 399 passed, 15 deselected
```

`python -m s_tui.s_tui --terminal` 能读取 CPU 频率、温度、利用率等基础信息。

### 硬件状态

`nvidia-smi` 可执行文件存在，但无法和 NVIDIA driver 通信：

```text
NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver.
```

`CompPower` 未拿到可用功率读数：

```text
component power probe: no component power readings
```

### 限制

- SSH/网络非常不稳定，多次出现 `Connection timed out`。
- 当前测试没有可用 NVIDIA driver 功率读数。
- 未确认到可用 IPMI/DCMI watt 读数。
- 未执行风扇写操作。

## fdu-gpu-1

### 基本信息

```text
SSH target: fdu-gpu-1
Hostname observed: fdusc-gpu-1
User: zsh
Target path: /mnt/data/personal/zsh/toys/s-tui2
DMI vendor: Giga Computing
Product: MA34-CP0-000
```

系统 Python 状况：

```text
/usr/bin/python3: Python 3.12.3
/usr/bin/python3: no pip
/usr/bin/python3: no venv / ensurepip
```

可用 Python：

```text
/home/zsh/miniconda3/bin/python: Python 3.13.11
```

测试环境：

```bash
/home/zsh/miniconda3/bin/python -m venv /tmp/s-tui2-venv-gpu
source /tmp/s-tui2-venv-gpu/bin/activate
python -m pip install -e '.[test]' ruff
```

### 通过的项目

```text
ruff check: passed
targeted tests: 28 passed
non-hardware tests: 401 passed, 15 deselected
```

TUI 主循环 `debug_run` 能启动、刷新并退出。调试日志中能看到 `CompPower` 刷新：

```text
Reading [72.74]
```

`--terminal` smoke test 成功，输出中检测到：

```text
CompPower: True
Power: True
Fan: False
Temp: True
Util: True
CompPower: GPU0:NVIDIA H200 NVL: 72.7,
```

### 功率 provider 诊断

```text
hwmon: no readings
nvidia: GPU0:NVIDIA H200 NVL: 72.68 W
ipmi_sensor: no readings
ipmi_dcmi: no readings
combined: GPU0:NVIDIA H200 NVL: 72.69 W
```

`nvidia-smi` 原始输出：

```text
0, NVIDIA H200 NVL, 72.70
```

### IPMI 状态

`ipmitool` 存在，但本机没有可用 IPMI device：

```text
Could not open device at /dev/ipmi0 or /dev/ipmi/0 or /dev/ipmidev/0: No such file or directory
```

所以：

- `ipmitool dcmi power reading` 不可用。
- `ipmitool sensor` 中没有可读取的 watt sensor。
- IPMI 风扇控制不可用。

### 风扇状态

`FanSource` 不可用：

```text
Fan: False
```

`Fan Control` 目标为空：

```text
DMI: giga computing giga computing ma34-cp0-000 giga computing
Fan Control targets: none
```

这是预期行为。当前代码只在检测到 Dell 或 Supermicro DMI 厂商时显示对应 raw IPMI 目标；`fdu-gpu-1` 是 Giga Computing，不能安全套用 Dell/Supermicro raw 命令。

## 结论

`fdu-gpu-1` 可以作为当前版本的有效演示机器：

- GPU 功率监控可用。
- TUI 和 terminal 输出均可显示 `CompPower`。
- 自动测试和 lint 均通过。

当前还不能验证风扇调速：

- `fdu-cpu-1` 网络不稳定且 GPU driver 不可用。
- `fdu-gpu-1` 没有 `/dev/ipmi0`，没有 psutil fan RPM，也不是 Dell/Supermicro DMI。

后续如果需要验证风扇控制，应选择满足至少一种条件的机器：

- Dell iDRAC，且本机 `ipmitool raw ...` 可用。
- Supermicro BMC，且本机 `ipmitool raw ...` 可用。
- Linux hwmon 暴露可写 `pwmN`/`pwmN_enable`。

第一次测试风扇写操作时建议：

```bash
python -m s_tui.s_tui --min-fan-duty 40
```

并在有人能恢复机器散热策略的条件下进行。
