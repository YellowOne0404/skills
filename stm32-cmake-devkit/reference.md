# STM32 命令行工具链手工安装

`scripts/env_setup.py` 失败或当前环境没有 winget / 包管理器时，按本文安装。版本号会更新；验证时以 `--version` 的实际输出为准，不必强求下面示例里的具体版本。

Windows 上 J-Link 用 SEGGER 软件调试。OpenOCD 只用于 ST-Link。不要用 OpenOCD 驱 J-Link。

## 一、ARM GNU Toolchain

ARM GNU Toolchain 是 ARM Cortex-M/R/A 的交叉编译工具链，包含 `arm-none-eabi-gcc`、`arm-none-eabi-gdb`、`arm-none-eabi-objcopy`、`arm-none-eabi-size`。主机是 x86/x64，目标是 Cortex-M 时必须用这套交叉编译器才能生成 ARM 机器码。

用途：把 C/C++ 编成 STM32 的 `.elf` / `.hex` / `.bin`；`arm-none-eabi-gdb` 做命令行调试。Keil/IAR 可以编，但本流程以命令行构建为验证路径。

Windows：

1. 打开 https://developer.arm.com/downloads/-/arm-gnu-toolchain-downloads
2. 下载 Windows 的 `.exe` 或 `.msi`（文件名类似 `arm-gnu-toolchain-<版本>-mingw-w64-x86_64-arm-none-eabi.exe`）
3. 按向导安装。路径不要含中文或空格以外的特殊字符
4. 把安装目录下的 `bin` 加入用户 Path。常见位置：
   - `C:\Program Files\Arm GNU Toolchain arm-none-eabi\<版本>\bin`
   - `C:\Program Files (x86)\Arm GNU Toolchain arm-none-eabi\<版本>\bin`
   - `C:\Program Files (x86)\Arm\GNU Toolchain mingw-w64-i686-arm-none-eabi\bin`
5. 新开 PowerShell：`arm-none-eabi-gcc --version`

也可以用：

```powershell
winget install --id Arm.GnuArmEmbeddedToolchain --exact --accept-package-agreements --accept-source-agreements
```

zip 包则解压后把 `bin` 加入 Path。

Linux（Debian/Ubuntu）：

```bash
sudo apt-get update
sudo apt-get install -y gcc-arm-none-eabi binutils-arm-none-eabi gdb-multiarch
```

发行版里的 GCC 可能偏旧，且不一定提供 `arm-none-eabi-gdb`。需要与 Windows 侧同一主版本时，从 ARM 官网下载 `arm-gnu-toolchain-<版本>-x86_64-arm-none-eabi.tar.xz`，解压到 `$HOME/.local/stm32-cmake-devkit/arm-gnu-toolchain`，把 `bin` 写入 `~/.bashrc` 的 `PATH`。没有 `arm-none-eabi-gdb` 时用 `gdb-multiarch`。

## 二、CMake

CMake 读取 `CMakeLists.txt` 和工具链文件，生成 Ninja 构建文件。本流程不依赖 `.uvprojx` 或 IAR 工程作为权威构建。

Windows：

1. https://cmake.org/download/ 下载 Windows x64 Installer
2. 安装时勾选 **Add CMake to the system PATH for the current user**
3. 新开 PowerShell：`cmake --version`

```powershell
winget install --id Kitware.CMake --exact --accept-package-agreements --accept-source-agreements
```

Linux：`sudo apt-get install -y cmake`。需要 3.22 或更新。

工具链文件使用 `cmake/gcc-arm-none-eabi.cmake`，里面是 Cortex-M 的 cpu/fpu/float-abi 以及 `nano.specs`。

## 三、Ninja

Ninja 只执行编译命令。配置由 CMake 完成。CMake preset 的 generator 是 Ninja。

Windows 推荐：

```powershell
winget install --id Ninja-build.Ninja --exact --accept-package-agreements --accept-source-agreements
```

手工：从 https://github.com/ninja-build/ninja/releases 下载 `ninja-win.zip`，把 `ninja.exe` 所在目录加入 Path。

Linux：`sudo apt-get install -y ninja-build`

验证：`ninja --version`

## 四、xPack OpenOCD

OpenOCD 通过 ST-Link 的 SWD 给 STM32 下载程序，并在 GDB 端口（默认 3333）上提供调试服务。xPack 是官方上游的预编译包。

Windows 推荐先：

```powershell
xpm install --global @xpack-dev-tools/openocd@0.12.0-7.1 --verbose
```

xpm 装好后，可执行文件通常在：

`%APPDATA%\xPacks\@xpack-dev-tools\openocd\<版本>\.content\bin\openocd.exe`

脚本目录在同级 `.content\openocd\scripts`。

手工：打开 https://github.com/xpack-dev-tools/openocd-xpack/releases/tag/v0.12.0-7

- Windows：`xpack-openocd-0.12.0-7-win32-x64.zip`
- Linux x64：`xpack-openocd-0.12.0-7-linux-x64.tar.gz`

解压后把 `bin` 加入 Path。验证：`openocd --version`，应包含 `0.12.0`。

Linux 也可以：`sudo apt-get install -y openocd`。发行版包往往更旧；ST-Link 连不上时改用上面的 xPack 0.12.0-7。

烧录示例（把 ELF 换成实际产物）：

```powershell
openocd -f interface/stlink.cfg -f target/stm32h7x.cfg -c "program build/Debug/firmware.elf verify reset exit"
```

其他系列把 `target/stm32h7x.cfg` 换成对应 cfg：F4 用 `target/stm32f4x.cfg`，F1 用 `target/stm32f1x.cfg`，G4 用 `target/stm32g4x.cfg`，L4 用 `target/stm32l4x.cfg`。

## 五、LLVM / clangd

clangd 通过 `compile_commands.json` 理解交叉编译参数（宏、头文件、`-mcpu`）。Microsoft C/C++ 插件在嵌入式工程里经常误报，编辑器应关闭它的 IntelliSense，改由 clangd 提供补全和跳转。

Windows：

```powershell
winget install --id LLVM.LLVM --exact --accept-package-agreements --accept-source-agreements
```

安装程序里勾选 **Add LLVM to the system PATH**。手工包从 https://github.com/llvm/llvm-project/releases 下载，把 `bin` 加入 Path。

Linux：`sudo apt-get install -y clangd`

验证：`clangd --version`

工程里的 `.clangd` 指向 `build/Debug`。`.vscode/settings.json` 使用：

```json
{
  "clangd.arguments": [
    "--query-driver=**/arm-none-eabi-*",
    "--background-index",
    "--completion-style=detailed",
    "--header-insertion=never"
  ],
  "C_Cpp.intelliSenseEngine": "disabled"
}
```

`--query-driver` 在部分 Windows 环境需要写成工具链 `bin` 的实际目录，例如 `C:/Program Files/Arm GNU Toolchain arm-none-eabi/14.2.Rel1/bin/arm-none-eabi-*`。转换脚本检测到编译器时会写成具体目录。

## 六、STM32CubeMX

CubeMX 用 `.ioc` 配置引脚、时钟和外设，并生成 HAL 初始化代码。只有改硬件配置时才需要它。日常编译不依赖 CubeMX 图形界面，但依赖它已经生成的 `Core/` 源码。

1. https://www.st.com/en/development-tools/stm32cubemx.html
2. 下载 Windows 安装包并运行 `SetupSTM32CubeMX-*.exe`
3. 安装路径不要含中文
4. 首次启动时设置固件库目录，放在空间足够的磁盘上

仅有 `.ioc`、没有 `Core/Src` 时无法编译。先在 CubeMX 里 Generate Code，再运行转换或 CMake。

## 七、ST-Link USB 驱动

OpenOCD 要能打开 ST-Link 的 USB 设备。Windows 10/11 多数情况会自动装驱动。连上后设备管理器里应出现 STM32 STLink。

驱动异常时安装 STSW-LINK009：

1. 在 ST 网站搜索 STSW-LINK009 并下载 zip
2. 64 位系统运行 `dpinst_amd64.exe` 或 `dpinst_x64.exe`，32 位运行 `dpinst_x86.exe`

Linux 把 OpenOCD 自带的 udev 规则装到 `/etc/udev/rules.d/` 后重新加载。用户需要在 `plugdev` 组，重新登录后生效。脚本在无法 `sudo -n` 时会打印要执行的命令。

## 八、USB-TTL 驱动

USB-TTL 把 MCU 的 USART 转成 COM 口，供串口日志或上位机读取。它不是 SWD 调试器，不能代替 ST-Link / J-Link。

看模块丝印再装对应驱动：CP2102 用 Silicon Labs CP210x，CH340 用沁恒 CH340，FTDI 用 FTDI VCP。装好后在设备管理器里确认 COM 号。Linux 通常由内核 `cp210x` / `ch341` / `ftdi_sio` 直接支持，用户需在 `dialout` 组。

## 九、Git

Windows：https://git-scm.com/download/win 或

```powershell
winget install --id Git.Git --exact --accept-package-agreements --accept-source-agreements
```

安装器会把 `C:\Program Files\Git\cmd` 加入 Path。验证：`git --version`

Linux：`sudo apt-get install -y git`

## 十、编辑器扩展

用 VS Code 或 Cursor 时安装：

- `llvm-vs-code-extensions.vscode-clangd`
- `ms-vscode.cmake-tools`
- `marus25.cortex-debug`

```powershell
cursor --install-extension llvm-vs-code-extensions.vscode-clangd
cursor --install-extension ms-vscode.cmake-tools
cursor --install-extension marus25.cortex-debug
```

没有 `cursor` 命令时把 `cursor` 换成 `code`。

## 十一、J-Link 软件

J-Link 烧录和 GDB Server 来自 SEGGER J-Link Software Pack，不是 OpenOCD。

Windows 下载页：https://www.segger.com/downloads/jlink

winget 可用时：

```powershell
winget install --id NordicSemiconductor.JLink --exact --accept-package-agreements --accept-source-agreements
```

该包失败时再试 `--id SEGGER.J-Link`。装完后典型路径：

`C:\Program Files\SEGGER\JLink\JLink.exe`

同目录还应有 `JLinkGDBServerCL.exe`。可用环境变量 `JLINK_PATH` 指向 `JLink.exe`。

Linux 从 SEGGER 下载 `.deb` / `.tgz`，解压到 `/opt/SEGGER/JLink`，把该目录加入 `PATH`，并安装 SEGGER 提供的 udev 规则。命令名是 `JLinkExe` 和 `JLinkGDBServerCLExe`。

## PATH 对照

修改 Path 后新开终端和编辑器。

| 工具 | Windows 常见路径 |
| --- | --- |
| ARM GNU Toolchain | `...\Arm GNU Toolchain arm-none-eabi\<版本>\bin` |
| CMake | `C:\Program Files\CMake\bin` |
| Ninja | winget Links 目录，或解压目录 |
| xPack OpenOCD | `%APPDATA%\xPacks\@xpack-dev-tools\openocd\<版本>\.content\bin` |
| LLVM / clangd | `C:\Program Files\LLVM\bin` |
| Git | `C:\Program Files\Git\cmd` |
| J-Link | `C:\Program Files\SEGGER\JLink` |

Linux 对应目录是 `/usr/bin`，或 `$HOME/.local/stm32-cmake-devkit/.../bin`。

## 编译

在工程根目录：

```bash
cmake --preset Debug
cmake --build --preset Debug
```

多产品仓库（存在 `hbuild.ps1` 与 `product/products.cmake`）不要用 preset 切换产品。preset 只服务 IDE 和 clangd，默认产品由工程自己的 `CMakeLists.txt` 决定。日常编译：

```powershell
.\hbuild.ps1 <产品序号> Debug -b
.\hbuild.ps1 <产品序号> Debug --rebuild
```

```bash
./hbuild.sh <产品序号> Debug -b
```

同一 `build/Debug` 或 `build/Release` 换产品时必须重建，否则 CMake 缓存里的 `PRODUCT_ID` 会串台。

## 烧录与调试

板子上电，SWD 接 ST-Link 或 J-Link。

ST-Link：

```powershell
openocd -f interface/stlink.cfg -f target/stm32h7x.cfg -c "program build/Debug/firmware.elf verify reset exit"
```

J-Link（设备名按芯片修改，例如 `STM32H743ZI`）：

```text
JLink.exe -device STM32H743ZI -if SWD -speed 4000 -autoconnect 1 -NoGui 1 -CommandFile flash.jlink
```

`flash.jlink` 内容：

```text
h
loadfile C:/path/to/firmware.elf
r
g
qc
```

成功时 OpenOCD 打印 `Verified OK`，J-Link 打印 `O.K.`。

GDB 在服务器起来之后连接。OpenOCD 用 `127.0.0.1:3333`，J-Link GDB Server 用 `127.0.0.1:2331`。

```text
target extended-remote 127.0.0.1:3333
break main
monitor reset halt
continue
bt
info locals
```
