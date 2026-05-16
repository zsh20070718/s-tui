# s-tui

这是一个基于上游 [amanusk/s-tui](https://github.com/amanusk/s-tui) 改造的命令行 TUI 监控工具版本。原项目主要监控 CPU 温度、频率、利用率和 RAPL 功率；本版本面向 ASC26/Jiangwan 机器测试，额外加入了组件功率监控和受保护的风扇控制入口。

## 当前能力

- CPU 监控：温度、频率、利用率、RAPL 功率。
- GPU/组件功率监控：新增 `CompPower` 数据源。
- 风扇转速显示：保留原有 psutil fan RPM 读取能力。
- 风扇控制菜单：新增 `Fan Control`，但只在检测到支持且可控的后端时显示。
- 终端一次性输出：`--terminal` 和 `--json` 已接入 `CompPower`。
- 压测：保留上游内置 CPU stress 和外部 `stress`/`stress-ng` 支持。

## 新增数据源

### `CompPower`

`CompPower` 会尝试从以下来源读取瞬时功率，单位为 W：

- Linux hwmon：`/sys/class/hwmon/hwmon*/power*_input`
- NVIDIA GPU：`nvidia-smi --query-gpu=index,name,power.draw --format=csv,noheader,nounits`
- IPMI sensor：`ipmitool sensor` 中单位为 `Watts` 或 `W` 的传感器行
- IPMI DCMI：`ipmitool dcmi power reading`

如果某台机器只暴露其中一种来源，TUI 中只显示该来源。例如在 `fdu-gpu-1` 上，目前能读到 NVIDIA H200 的功率，但读不到 IPMI/DCMI 功率。

### 风扇控制

`Fan Control` 支持以下后端：

- Dell iDRAC raw IPMI fan command，仅在 DMI 厂商信息包含 Dell 时显示。
- Supermicro raw IPMI fan command，仅在 DMI 厂商信息包含 Supermicro 或 Super Micro 时显示。
- 可写的 Linux hwmon PWM 控制文件：`pwmN` 和可选的 `pwmN_enable`。

默认不会在未知 BMC 厂商上暴露 Dell/Supermicro raw 命令，避免误发风扇控制命令。手动风扇 duty 默认下限为 20%，可通过 `--min-fan-duty` 调整。

## 安装和运行

推荐在独立虚拟环境中运行：

```bash
python3 -m venv /tmp/s-tui2-venv
source /tmp/s-tui2-venv/bin/activate
python -m pip install -e '.[test]'
python -m s_tui.s_tui
```

如果系统 Python 没有 `venv` 或 `pip`，可以使用已有 Conda Python。例如 `fdu-gpu-1` 上使用：

```bash
cd /mnt/data/personal/zsh/toys/s-tui2
/home/zsh/miniconda3/bin/python -m venv /tmp/s-tui2-venv-gpu
source /tmp/s-tui2-venv-gpu/bin/activate
python -m pip install -e '.[test]' ruff
python -m s_tui.s_tui
```

一次性输出当前读数：

```bash
python -m s_tui.s_tui --terminal
python -m s_tui.s_tui --json
```

提高风扇手动 duty 下限：

```bash
python -m s_tui.s_tui --min-fan-duty 40
```

## Jiangwan 运行状况

测试日期：2026-05-16。

### `fdu-cpu-1`

路径：

```bash
/mnt/data/personal/zsh/toys/s-tui2
```

结果：

- SSH 曾成功连接到 `fdusc-cpu-1`，但网络频繁超时。
- 远端 lint 和非硬件测试通过：
  - `ruff check`: passed
  - targeted tests: `26 passed`
  - non-hardware tests: `399 passed, 15 deselected`
- `--terminal` 能读取 CPU 频率、温度、利用率。
- NVIDIA 驱动不可用，`nvidia-smi` 返回无法和 NVIDIA driver 通信。
- `CompPower` 没有拿到可用功率读数。
- 没有执行风扇写操作。

### `fdu-gpu-1`

路径：

```bash
/mnt/data/personal/zsh/toys/s-tui2
```

运行环境：

- Hostname: `fdusc-gpu-1`
- 系统 Python: Python 3.12.3，但没有 `pip`/`venv`
- 可用 Python: `/home/zsh/miniconda3/bin/python`，Python 3.13.11
- 测试 venv: `/tmp/s-tui2-venv-gpu`

测试结果：

- `ruff check`: passed
- targeted tests: `28 passed`
- non-hardware tests: `401 passed, 15 deselected`
- TUI `debug_run` 能启动刷新循环并退出。
- `--terminal` smoke test 成功，输出中包含：
  - `CompPower: True`
  - `Power: True`
  - `Temp: True`
  - `Util: True`
  - `Fan: False`

硬件读数：

```text
DMI: Giga Computing / MA34-CP0-000
Fan Control targets: none
hwmon power: no readings
nvidia: GPU0:NVIDIA H200 NVL: about 72.7 W
ipmi_sensor: no readings
ipmi_dcmi: no readings
combined CompPower: GPU0:NVIDIA H200 NVL: about 72.7 W
```

结论：

- `fdu-gpu-1` 上 GPU 功率监控已经可用。
- 该机没有 `/dev/ipmi0`，`ipmitool dcmi power reading` 无法打开本机 IPMI device。
- DMI 厂商为 Giga Computing，不是 Dell/Supermicro，所以不会显示 raw IPMI 风扇控制目标。
- psutil 没有暴露 fan RPM，因此当前 `Fan` 不可用。

更详细的测试记录见 [docs/JIANGWAN_TEST_REPORT.md](docs/JIANGWAN_TEST_REPORT.md)。

## 常用命令

本地测试：

```bash
python -m ruff check s_tui tests
python -m pytest tests/test_component_power_source.py tests/test_fan_control_menu.py tests/test_cli.py -q
python -m pytest tests/ -m 'not hardware' -q
```

在 `fdu-gpu-1` 运行：

```bash
ssh fdu-gpu-1
cd /mnt/data/personal/zsh/toys/s-tui2
source /tmp/s-tui2-venv-gpu/bin/activate
python -m s_tui.s_tui
```

如果 `/tmp/s-tui2-venv-gpu` 不存在：

```bash
/home/zsh/miniconda3/bin/python -m venv /tmp/s-tui2-venv-gpu
source /tmp/s-tui2-venv-gpu/bin/activate
python -m pip install -e '.[test]' ruff
python -m s_tui.s_tui
```

## 依赖

基础依赖：

- Python 3.10+
- `urwid>=3.0.2`
- `psutil>=7.0.0`

可选硬件工具：

- `nvidia-smi`：读取 NVIDIA GPU 功率。
- `ipmitool`：读取 IPMI 功率，或在受支持机器上做风扇控制。
- `lm-sensors`/hwmon：暴露温度、风扇和功率传感器。

## 风扇控制安全说明

风扇调速可能影响机器散热。本版本采取以下保护：

- 默认不执行任何风扇写操作，只有用户在 `Fan Control` 菜单里 Apply 才会写。
- 手动 duty 必须在 `--min-fan-duty` 和 100% 之间，默认最低 20%。
- Dell/Supermicro raw IPMI 控制只在 DMI 厂商匹配时显示。
- 未知厂商机器不会暴露 Dell/Supermicro raw 目标。
- Giga Computing 的 `fdu-gpu-1` 当前不会显示 raw IPMI fan target。

在新机器上第一次测试时，建议把下限设高：

```bash
python -m s_tui.s_tui --min-fan-duty 40
```

## Upstream

本项目基于 GPLv2 授权的 upstream `s-tui`：

- Website: <https://amanusk.github.io/s-tui/>
- GitHub: <https://github.com/amanusk/s-tui>

原项目版权信息和许可证保留在源码文件及 [LICENSE](LICENSE) 中。
