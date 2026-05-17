# s-tui

这是一个基于上游 [amanusk/s-tui](https://github.com/amanusk/s-tui) 改造的命令行 TUI 监控工具版本。原项目主要监控 CPU 温度、频率、利用率和 RAPL 功率；本版本面向 ASC26/Jiangwan 机器测试，额外加入了组件功率监控和受保护的风扇控制入口。

## 当前能力

- CPU 监控：温度、频率、利用率、RAPL 功率。
- GPU/组件功率监控：新增 `CompPower` 数据源。
- 风扇转速显示：优先读取 Inspur BMC API，失败后回退到 psutil/IPMI sensor。
- 风扇控制菜单：默认隐藏；只有显式传入 `--enable-fan-control` 后才会暴露受支持目标。
- 终端一次性输出：`--terminal` 和 `--json` 已接入 `CompPower`。
- 默认 UI：显示 Total/CPU/Fan/GPU/Temp 的极简监视器；原始图形菜单使用 `--classic-ui`。
- 压测：保留上游内置 CPU stress 和外部 `stress`/`stress-ng` 支持。

## 新增数据源

### `CompPower`

`CompPower` 会尝试从以下来源读取瞬时功率，单位为 W：

- Linux hwmon：`/sys/class/hwmon/hwmon*/power*_input`
- NVIDIA GPU：`nvidia-smi --query-gpu=index,name,power.draw --format=csv,noheader,nounits`
- Inspur BMC API：`/api/sensors/temAndPowerReading` 中的功率传感器
- IPMI sensor：`ipmitool sensor` 中单位为 `Watts` 或 `W` 的传感器行
- IPMI DCMI：`ipmitool dcmi power reading`

如果某台机器只暴露其中一种来源，TUI 中只显示该来源。例如在 `fdu-gpu-1` 上，目前能读到 NVIDIA H200 的功率，但读不到 IPMI/DCMI 功率。

### 风扇控制

`Fan Control` 默认禁用。本版本不再自动暴露以下任何可写后端：

- Dell iDRAC raw IPMI fan command，仅在 DMI 厂商信息包含 Dell 时显示。
- Supermicro raw IPMI fan command，仅在 DMI 厂商信息包含 Supermicro 或 Super Micro 时显示。
- 可写的 Linux hwmon PWM 控制文件：`pwmN` 和可选的 `pwmN_enable`。
- Inspur BMC Web API，仅在 DMI 匹配或显式传入 `--fan-control-vendor inspur` 时显示。

默认不会暴露任何风扇控制入口，因为部分机器的 hwmon PWM/IPMI raw 命令可能实际控制 PSU 风扇或风扇板，而不是机箱风扇。需要写风扇 duty 时，必须同时传入 `--enable-fan-control` 和匹配的 vendor 参数。

### 101 丐版风扇监视器

目前只在 101 环境部署和验证。由于本机相关 IPMI 路径不稳定，101 的 Total/CPU/Fan/GPU/Temp 和 Inspur 风扇控制走 BMC Web API，而不是依赖 IPMI fan control。

推荐运行方式：

```bash
cd ~/s-tui2
S_TUI_BMC_USERNAME=<username> \
S_TUI_BMC_PASSWORD=<password> \
python -m s_tui.s_tui --enable-fan-control --fan-control-vendor inspur
```

`S_TUI_BMC_URL` 是可选项。默认会通过 `ipmitool lan print`、`sudo -n ipmitool lan print` 或 `sudo ipmitool lan print` 自动发现 BMC IP；如果显式传入的 URL 已经过期，程序会继续尝试自动发现到的候选 URL。

需要固定 BMC 地址时可手动指定：

```bash
cd ~/s-tui2
S_TUI_BMC_URL=https://<bmc-ip> \
S_TUI_BMC_USERNAME=<username> \
S_TUI_BMC_PASSWORD=<password> \
python -m s_tui.s_tui --enable-fan-control --fan-control-vendor inspur
```

技术路径：

- BMC 登录：`/api/randomtag` 和 `/api/session`。
- 风扇读数：`/api/status/fan_info`。
- 温度/功率读数：`/api/sensors/temAndPowerReading`。
- 风扇模式和 duty：`/api/settings/fans-mode`、`/api/settings/fan/<id>`。
- 只有在用户显式开启风扇控制后才会写 BMC API；普通监视读数只做只读请求。

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
python -m pytest tests/test_simple_tui.py tests/test_power_totals.py tests/test_fan_control_menu.py tests/test_fan_source.py tests/test_component_power_source.py tests/test_cli.py -q
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
- `ipmitool`：读取 IPMI 功率/风扇传感器，或用于自动发现本机 BMC IP。
- `lm-sensors`/hwmon：暴露温度、风扇和功率传感器。

## 风扇控制安全说明

风扇调速可能影响机器散热。本版本采取以下保护：

- 默认不暴露任何风扇写入口，必须显式传入 `--enable-fan-control`。
- 默认不自动发现 hwmon PWM 或 IPMI raw 风扇控制目标。
- 手动 duty 必须在 `--min-fan-duty` 和 100% 之间，默认最低 0%。
- Dell/Supermicro raw IPMI 控制只在 DMI 厂商匹配或显式指定 vendor 时显示。
- Inspur 风扇控制走 BMC Web API，只在显式开启并匹配/指定 `inspur` vendor 时显示。
- 未知厂商机器不会自动暴露 Dell/Supermicro/Inspur 写目标。
- Giga Computing 的 `fdu-gpu-1` 当前不会显示 raw IPMI fan target。

在新机器上不使用本工具做风扇控制；请通过 BMC Web UI 或厂商文档确认具体风扇域后再操作。

## Upstream

本项目基于 GPLv2 授权的 upstream `s-tui`：

- Website: <https://amanusk.github.io/s-tui/>
- GitHub: <https://github.com/amanusk/s-tui>

原项目版权信息和许可证保留在源码文件及 [LICENSE](LICENSE) 中。
