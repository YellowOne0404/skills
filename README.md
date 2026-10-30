# Cursor Agent Skills

[中文](#中文) | [English](#english)

面向嵌入式开发的 Cursor Agent Skills。每个技能一个目录，脚本只用 Python 标准库，Windows 和 Linux 都能跑。

Cursor Agent Skills for embedded development. Each skill is one directory. Scripts use the Python standard library only and run on Windows and Linux.

---

<a id="中文"></a>

## 中文

### 适不适合你

| 技能 | 用来做什么 | 输入 → 输出 | 不会做什么 |
| --- | --- | --- | --- |
| [`stm32-cmake-devkit`](stm32-cmake-devkit/SKILL.md) | 装 STM32 命令行工具链；把 Keil 或 CubeMX 工程转成 CMake；按 USB 识别 ST-Link / J-Link，完成烧录、GDB 调试、死机诊断和 Flash/RAM 占用分析 | Keil `.uvprojx` / `.uvproj`，或已生成源码的 CubeMX `.ioc` → `CMakeLists.txt`、`CMakePresets.json`、工具链文件、`.clangd`、Cortex-Debug 配置，以及 `.elf` | 不覆盖手写 `CMakeLists.txt`。两只探针同时在线时不猜测。Windows 上 J-Link 只用 SEGGER 软件，不用 OpenOCD |

只有 `.ioc`、旁边没有 CubeMX 生成的源码时，这个技能不能编出固件。需要先在 CubeMX 里生成代码。

### 安装

克隆本仓库后，任选一种方式。Cursor 会在对话里按需加载技能，也可以用 `/stm32-cmake-devkit` 显式调用。

**只给当前工程用**

```bash
git clone git@github.com:YellowOne0404/skills.git
```

把 `stm32-cmake-devkit` 拷进工程的 `.cursor/skills/`。

**本机所有工程共用**

把技能目录链接到 Cursor 的个人技能目录，仓库更新后技能跟着更新。

```powershell
New-Item -ItemType Junction -Path "$env:USERPROFILE\.cursor\skills\stm32-cmake-devkit" -Target "<克隆路径>\stm32-cmake-devkit"
```

```bash
ln -s /path/to/skills/stm32-cmake-devkit ~/.cursor/skills/stm32-cmake-devkit
```

个人技能目录以本机 Cursor 设置为准。上面的 `~/.cursor/skills` 是常见位置，不是唯一位置。

### 怎么用

在 Cursor 里直接说：

- “检查并安装 STM32 的 CMake 编译和调试环境”
- “把这个 Keil / CubeMX 工程转成 CMake 并编译”
- “识别探针，烧录固件，并抓几秒启动日志”
- “板子卡死了，不要复位，看停在哪里，读 CFSR”
- “看看这个 elf 占了多少 Flash 和 RAM”

也可以直接跑脚本。Agent 调用时加 `--json`。

```bash
python stm32-cmake-devkit/scripts/env_setup.py check
python stm32-cmake-devkit/scripts/env_setup.py install

python stm32-cmake-devkit/scripts/cmake_migrate.py --project <工程路径> --build --preset Debug

python stm32-cmake-devkit/scripts/probe_debug.py detect --project <工程路径>
python stm32-cmake-devkit/scripts/probe_debug.py flash --project <工程路径> --verify-boot
python stm32-cmake-devkit/scripts/probe_debug.py debug --project <工程路径>
python stm32-cmake-devkit/scripts/probe_debug.py diagnose --project <工程路径>
python stm32-cmake-devkit/scripts/probe_debug.py size-report --project <工程路径>
python stm32-cmake-devkit/scripts/probe_debug.py gen-launch --project <工程路径>
```

多产品、已经手写 CMake 的仓库不要用转换脚本覆盖构建文件。按该仓库自己的构建入口编译。

### 目录

```text
stm32-cmake-devkit/
├── SKILL.md              # Agent 何时启用、按什么步骤做
├── reference.md          # 工具链的手工安装和排查
└── scripts/
    ├── env_setup.py      # 检查并安装 gcc、CMake、Ninja、clangd、OpenOCD、J-Link
    ├── cmake_migrate.py  # Keil / CubeMX 转 CMake 并编译
    └── probe_debug.py    # 识别探针、烧录、调试、诊断、尺寸统计
```

改完脚本后，在技能目录里自检：

```bash
python scripts/cmake_migrate.py --self-test
python scripts/probe_debug.py --self-test
python scripts/env_setup.py check --json
```

---

<a id="english"></a>

## English

### Is this for you?

| Skill | What it does | Input → output | What it will not do |
| --- | --- | --- | --- |
| [`stm32-cmake-devkit`](stm32-cmake-devkit/SKILL.md) | Install the STM32 command-line toolchain; convert a Keil or CubeMX project to CMake; detect ST-Link or J-Link over USB, then flash, debug, diagnose a hang, and report Flash/RAM use | Keil `.uvprojx` / `.uvproj`, or a CubeMX `.ioc` with generated sources → `CMakeLists.txt`, `CMakePresets.json`, toolchain file, `.clangd`, Cortex-Debug configs, and an `.elf` | It does not overwrite a handwritten `CMakeLists.txt`. It does not guess when both probes are connected. On Windows, J-Link is driven by SEGGER software, not OpenOCD |

An `.ioc` file alone is not enough to build firmware. Generate the CubeMX sources first.

### Install

Clone this repository, then pick one option. Cursor loads a skill when the conversation needs it. You can also call `/stm32-cmake-devkit`.

**This project only**

```bash
git clone git@github.com:YellowOne0404/skills.git
```

Copy `stm32-cmake-devkit` into the project's `.cursor/skills/`.

**Every project on this machine**

Link the skill directory into Cursor's personal skills folder so a later `git pull` updates the skill in place.

```powershell
New-Item -ItemType Junction -Path "$env:USERPROFILE\.cursor\skills\stm32-cmake-devkit" -Target "<clone-path>\stm32-cmake-devkit"
```

```bash
ln -s /path/to/skills/stm32-cmake-devkit ~/.cursor/skills/stm32-cmake-devkit
```

The personal skills folder depends on the local Cursor setup. `~/.cursor/skills` is a common location, not the only one.

### Use it

Ask in Cursor:

- "Check and install the STM32 CMake build and debug toolchain."
- "Convert this Keil or CubeMX project to CMake and build it."
- "Detect the probe, flash the firmware, and capture a few seconds of boot log."
- "The board is hung. Do not reset it. Show where it stopped and read CFSR."
- "Show how much Flash and RAM this elf uses."

Or run the scripts directly. Add `--json` when an agent calls them.

```bash
python stm32-cmake-devkit/scripts/env_setup.py check
python stm32-cmake-devkit/scripts/env_setup.py install

python stm32-cmake-devkit/scripts/cmake_migrate.py --project <project> --build --preset Debug

python stm32-cmake-devkit/scripts/probe_debug.py detect --project <project>
python stm32-cmake-devkit/scripts/probe_debug.py flash --project <project> --verify-boot
python stm32-cmake-devkit/scripts/probe_debug.py debug --project <project>
python stm32-cmake-devkit/scripts/probe_debug.py diagnose --project <project>
python stm32-cmake-devkit/scripts/probe_debug.py size-report --project <project>
python stm32-cmake-devkit/scripts/probe_debug.py gen-launch --project <project>
```

Do not let the converter overwrite the build files of a multi-product repository that already has its own CMake. Build that repository with its own entry point.

### Layout

```text
stm32-cmake-devkit/
├── SKILL.md              # when the agent should run, and the workflow
├── reference.md          # manual toolchain install and troubleshooting
└── scripts/
    ├── env_setup.py      # check and install gcc, CMake, Ninja, clangd, OpenOCD, J-Link
    ├── cmake_migrate.py  # convert Keil / CubeMX to CMake and build
    └── probe_debug.py    # detect a probe, flash, debug, diagnose, report size
```

After changing a script, run the self-tests from the skill directory:

```bash
python scripts/cmake_migrate.py --self-test
python scripts/probe_debug.py --self-test
python scripts/env_setup.py check --json
```
