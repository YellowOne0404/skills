# skills

可复用的 Cursor Agent Skills。每个技能一个目录，入口是该目录下的 `SKILL.md`。

| 技能 | 作用 |
| --- | --- |
| `stm32-cmake-devkit` | 安装 STM32 命令行工具链，把 Keil / CubeMX 工程转成 CMake，并按 USB 识别 ST-Link 或 J-Link 后烧录、调试 |

在对应目录下可以自检：

```bash
python scripts/cmake_migrate.py --self-test
python scripts/probe_debug.py --self-test
python scripts/env_setup.py check --json
```
