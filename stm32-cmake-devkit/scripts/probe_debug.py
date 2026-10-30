"""按 USB VID 识别 ST-Link / J-Link，烧录 ELF，并做一次 GDB 调试。"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path


OPENOCD_TARGETS = (
    ("STM32WBA", "target/stm32wbax.cfg"),
    ("STM32H7", "target/stm32h7x.cfg"),
    ("STM32H5", "target/stm32h5x.cfg"),
    ("STM32WB", "target/stm32wbx.cfg"),
    ("STM32WL", "target/stm32wlx.cfg"),
    ("STM32U5", "target/stm32u5x.cfg"),
    ("STM32U0", "target/stm32u0x.cfg"),
    ("STM32L5", "target/stm32l5x.cfg"),
    ("STM32L4", "target/stm32l4x.cfg"),
    ("STM32L1", "target/stm32l1.cfg"),
    ("STM32L0", "target/stm32l0.cfg"),
    ("STM32G4", "target/stm32g4x.cfg"),
    ("STM32G0", "target/stm32g0x.cfg"),
    ("STM32F7", "target/stm32f7x.cfg"),
    ("STM32F4", "target/stm32f4x.cfg"),
    ("STM32F3", "target/stm32f3x.cfg"),
    ("STM32F2", "target/stm32f2x.cfg"),
    ("STM32F1", "target/stm32f1x.cfg"),
    ("STM32F0", "target/stm32f0x.cfg"),
    ("STM32C0", "target/stm32c0x.cfg"),
)

SKIP_WALK = {".git", "build", ".vscode", "cmake-build-debug", "cmake-build-release"}

# ST-Link V2/V2-1/V3 的 PID。VID_0483 里还有虚拟串口 5740 和 DFU，不能算探针。
STLINK_PIDS = frozenset({
    "3744", "3748", "374a", "374b", "3752",
    "374d", "374e", "374f", "3753", "3754", "3755", "3757",
})
_USB_ID = re.compile(r"VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})(\S*)", re.I)

CFSR_BITS = (
    (0, "IACCVIOL"),
    (1, "DACCVIOL"),
    (3, "MUNSTKERR"),
    (4, "MSTKERR"),
    (5, "MLSPERR"),
    (7, "MMARVALID"),
    (8, "IBUSERR"),
    (9, "PRECISERR"),
    (10, "IMPRECISERR"),
    (11, "UNSTKERR"),
    (12, "STKERR"),
    (13, "LSPERR"),
    (15, "BFARVALID"),
    (16, "UNDEFINSTR"),
    (17, "INVSTATE"),
    (18, "INVPC"),
    (19, "NOCP"),
    (24, "UNALIGNED"),
    (25, "DIVBYZERO"),
)
_MEM_LINE = re.compile(
    r"^\s*([A-Za-z_][\w]*)\s*(?:\([^)]*\))?\s*:\s*ORIGIN\s*=\s*(0x[0-9A-Fa-f]+)\s*,\s*LENGTH\s*=\s*(\S+)",
    re.M,
)
_SIZE_LINE = re.compile(r"^\s*(\.[A-Za-z0-9_.]+)\s+(\d+)\s+(0x[0-9A-Fa-f]+)\s*$", re.M)
_NM_LINE = re.compile(r"^([0-9A-Fa-f]+)\s+([0-9A-Fa-f]+)\s+([A-Za-z])\s+(\S+)", re.M)
_JLINK_FAIL = re.compile(
    r"cannot connect|could not connect|failed to|error while|unspecified error|no device found",
    re.I,
)
_JLINK_OK = re.compile(r"O\.K\.|Flash download|Programming Done|Verify successful", re.I)


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def emit(payload: dict, as_json: bool, code: int) -> int:
    if as_json:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print(payload.get("summary", ""))
        details = payload.get("details") or {}
        for key in ("probe", "elf", "device", "gdb_port"):
            if details.get(key):
                print(f"  {key}: {details[key]}")
        report = details.get("report") or details.get("boot_log") or ""
        if report:
            print(report[-4000:])
        output = details.get("output") or details.get("gdb_output")
        if output and payload.get("status") != "ok":
            print(output[-4000:])
    return code


def to_jlink_device(name: str) -> str:
    """STM32H743ZITx -> STM32H743ZI。已经是 J-Link 设备名时保持不变。"""
    return re.sub(r"[A-Za-z]x$", "", name.strip())


def openocd_target_cfg(device: str) -> str:
    folded = device.upper()
    for prefix, cfg in OPENOCD_TARGETS:
        if folded.startswith(prefix):
            return cfg
    raise ValueError(f"无法从芯片型号 {device} 推断 OpenOCD target cfg")


def run_capture(args: list[str], timeout: int = 120, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _program_files() -> list[Path]:
    roots = []
    for key in ("ProgramFiles", "ProgramFiles(x86)"):
        value = os.environ.get(key)
        if value:
            roots.append(Path(value))
    return roots


def find_openocd() -> Path | None:
    found = shutil.which("openocd")
    if found:
        return Path(found)
    roots = []
    roaming = os.environ.get("APPDATA")
    if roaming:
        roots.append(Path(roaming) / "xPacks" / "@xpack-dev-tools" / "openocd")
    roots.append(Path.home() / ".local" / "xPacks" / "@xpack-dev-tools" / "openocd")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "stm32-cmake-devkit" / "openocd")
    roots.append(Path.home() / ".local" / "stm32-cmake-devkit" / "openocd")
    matches: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        matches.extend(root.rglob("openocd.exe"))
        matches.extend(root.rglob("openocd"))
    files = [item for item in matches if item.is_file() and item.name.startswith("openocd")]
    files.sort(key=lambda item: item.as_posix(), reverse=True)
    return files[0] if files else None


def openocd_scripts(executable: Path) -> Path | None:
    candidates = [
        executable.parent.parent / "openocd" / "scripts",
        executable.parent.parent / "share" / "openocd" / "scripts",
    ]
    for candidate in candidates:
        if (candidate / "interface" / "stlink.cfg").is_file():
            return candidate
    return None


def find_jlink() -> Path | None:
    env_path = os.environ.get("JLINK_PATH", "")
    if env_path and Path(env_path).is_file():
        return Path(env_path)
    name = "JLink.exe" if sys.platform == "win32" else "JLinkExe"
    found = shutil.which(name)
    if found:
        return Path(found)
    roots = [root / "SEGGER" for root in _program_files()]
    roots.append(Path("/opt/SEGGER"))
    matches: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        matches.extend(path for path in root.glob("JLink*/" + name) if path.is_file())
    matches.sort(key=lambda item: item.as_posix(), reverse=True)
    return matches[0] if matches else None


def find_jlink_gdb(jlink: Path | None) -> Path | None:
    names = ["JLinkGDBServerCL.exe", "JLinkGDBServerCLExe"]
    if jlink:
        for name in names:
            sibling = jlink.with_name(name)
            if sibling.is_file():
                return sibling
    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def find_gdb() -> Path | None:
    for name in ("arm-none-eabi-gdb", "gdb-multiarch"):
        found = shutil.which(name)
        if found:
            return Path(found)
    manual = Path("C:/arm-gcc/bin/arm-none-eabi-gdb.exe")
    if manual.is_file():
        return manual
    return None


def counts_from_text(text: str) -> dict:
    """复合设备的 MI_00/01/02 算一只探针。VID_0483 的 CDC/DFU 不算 ST-Link。"""
    stlink: set[str] = set()
    jlink: set[str] = set()
    for vid, pid, tail in _USB_ID.findall(text):
        identity = re.sub(r"&MI_[0-9A-Fa-f]+", "", tail, flags=re.I).upper()
        key = pid.lower() + identity
        vid_l = vid.lower()
        pid_l = pid.lower()
        if vid_l == "0483" and pid_l in STLINK_PIDS:
            stlink.add(key)
        elif vid_l == "1366":
            jlink.add(key)
    return {"stlink": len(stlink), "jlink": len(jlink)}


def detect_counts() -> dict:
    if sys.platform == "win32":
        result = run_capture(["pnputil", "/enum-devices", "/connected"], timeout=60)
        text = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0 or "VID_" not in text.upper():
            ps = (
                "Get-PnpDevice -PresentOnly | "
                "Where-Object { $_.InstanceId -match 'VID_0483|VID_1366' } | "
                "ForEach-Object { $_.InstanceId }"
            )
            result = run_capture(["powershell", "-NoProfile", "-Command", ps], timeout=60)
            text = (result.stdout or "") + (result.stderr or "")
        return counts_from_text(text)
    stlink = 0
    jlink = 0
    base = Path("/sys/bus/usb/devices")
    if base.is_dir():
        for device in base.iterdir():
            vendor = device / "idVendor"
            if not vendor.is_file():
                continue
            value = vendor.read_text(encoding="utf-8", errors="replace").strip().lower()
            product = device / "idProduct"
            pid = product.read_text(encoding="utf-8", errors="replace").strip().lower() if product.is_file() else ""
            if value == "0483" and pid in STLINK_PIDS:
                stlink += 1
            elif value == "1366":
                jlink += 1
    return {"stlink": stlink, "jlink": jlink}


def resolve_probe(requested: str, counts: dict) -> str:
    choice = requested.strip().lower()
    if choice == "auto":
        env_choice = os.environ.get("FLASH_PROBE", "").strip().lower()
        if env_choice:
            choice = env_choice
    if choice in {"stlink", "jlink"}:
        return choice
    if choice != "auto":
        raise ValueError(f"未知探针 {requested}，使用 auto、stlink 或 jlink")
    if counts["stlink"] and counts["jlink"]:
        raise RuntimeError("同时检测到 ST-Link (VID_0483) 和 J-Link (VID_1366)。请指定 --probe stlink 或 --probe jlink")
    if counts["stlink"]:
        return "stlink"
    if counts["jlink"]:
        return "jlink"
    raise RuntimeError("未检测到 ST-Link (VID_0483) 或 J-Link (VID_1366)")


def _walk_skip(dirnames: list[str]) -> None:
    dirnames[:] = [name for name in dirnames if name not in SKIP_WALK and not name.startswith(".")]


def _config_device(project: Path) -> str:
    config = project / ".embeddedskills" / "config.json"
    if not config.is_file():
        return ""
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ""
    if not isinstance(data, dict):
        return ""
    return str((data.get("jlink") or {}).get("device") or "").strip()


def _cmake_device(project: Path) -> str:
    lists = project / "CMakeLists.txt"
    if not lists.is_file():
        return ""
    match = re.search(r"^#\s*STM32_DEVICE:\s*(\S+)", lists.read_text(encoding="utf-8", errors="replace"), re.M)
    return match.group(1) if match else ""


def _named_devices(project: Path, kind: str) -> set[str]:
    names: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(project):
        _walk_skip(dirnames)
        for filename in filenames:
            path = Path(dirpath) / filename
            if kind == "keil" and filename.lower().endswith((".uvprojx", ".uvproj")):
                text = path.read_text(encoding="utf-8", errors="replace")
                for match in re.findall(r"<Device>\s*([^<]+?)\s*</Device>", text):
                    names.add(match.strip())
            elif kind == "ioc" and filename.endswith(".ioc"):
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.startswith("Mcu.Name="):
                        names.add(line.split("=", 1)[1].strip())
                        break
    return names


def _one_device(names: set[str], label: str) -> str:
    if len(names) > 1:
        raise RuntimeError(f"工程里有多个不同的 {label}: " + ", ".join(sorted(names)))
    if len(names) == 1:
        return next(iter(names))
    return ""


def find_device(project: Path, explicit: str | None) -> str:
    if explicit:
        return explicit
    found = _config_device(project)
    if found:
        return found
    found = _cmake_device(project)
    if found:
        return found
    found = _one_device(_named_devices(project, "keil"), "Device")
    if found:
        return found
    found = _one_device(_named_devices(project, "ioc"), "Mcu.Name")
    if found:
        return found
    raise RuntimeError("没有芯片型号。传入 --device，例如 STM32H743ZI")


def find_elf(project: Path, explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.is_absolute():
            path = project / path
        if not path.is_file():
            raise FileNotFoundError(f"找不到 ELF: {path}")
        return path.resolve()
    preferred = [
        project / "build" / "Debug" / "firmware.elf",
        project / "build" / "Release" / "firmware.elf",
        project / "build" / "firmware.elf",
    ]
    for candidate in preferred:
        if candidate.is_file():
            return candidate.resolve()
    found = [
        path for path in project.glob("build/**/*.elf")
        if path.is_file() and "CMakeFiles" not in path.parts
    ]
    if len(found) == 1:
        return found[0].resolve()
    if not found:
        raise FileNotFoundError("build/ 下没有 ELF。先编译工程")
    listed = ", ".join(str(path.relative_to(project)) for path in found[:8])
    raise FileNotFoundError("有多个 ELF，请用 --elf 指定: " + listed)


def program_command(image: Path, address: str) -> str:
    if image.suffix.lower() == ".bin":
        if not address:
            raise ValueError(".bin 烧录必须提供 --address，例如 0x08000000")
        return f'program "{image.as_posix()}" {address} verify reset exit'
    return f'program "{image.as_posix()}" verify reset exit'


def jlink_load_line(image: Path, address: str) -> str:
    if image.suffix.lower() == ".bin":
        if not address:
            raise ValueError(".bin 烧录必须提供 --address，例如 0x08000000")
        return f"loadbin {image.as_posix()} {address}"
    return f"loadfile {image.as_posix()}"


def read_text_auto(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    if len(data) >= 2 and data[1] == 0:
        return data.decode("utf-16-le", errors="replace")
    return data.decode("utf-8", errors="replace")


def jlink_flash_succeeded(code: int, text: str) -> bool:
    if code != 0 or _JLINK_FAIL.search(text):
        return False
    return _JLINK_OK.search(text) is not None


def flash_stlink(image: Path, device: str, address: str = "") -> tuple[bool, str]:
    openocd = find_openocd()
    if not openocd:
        return False, "openocd 不在 PATH 中"
    target = openocd_target_cfg(device)
    command = [str(openocd)]
    scripts = openocd_scripts(openocd)
    if scripts:
        command.extend(["-s", str(scripts)])
    command.extend([
        "-f", "interface/stlink.cfg",
        "-f", target,
        "-c", program_command(image, address),
    ])
    result = run_capture(command, timeout=180)
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    ok = result.returncode == 0 and re.search(r"verify\s+ok|verified\s+ok", output, re.I)
    return ok, output


def flash_jlink(image: Path, device: str, address: str = "") -> tuple[bool, str]:
    jlink = find_jlink()
    if not jlink:
        return False, "找不到 JLink.exe / JLinkExe。安装 SEGGER J-Link 或设置 JLINK_PATH"
    jlink_device = to_jlink_device(device)
    command_file = Path(tempfile.gettempdir()) / "stm32-cmake-devkit-flash.jlink"
    log_path = Path(tempfile.gettempdir()) / "stm32-cmake-devkit-jlink.log"
    if log_path.exists():
        log_path.unlink()
    command_file.write_text(
        f"log {log_path.as_posix()}\nh\n{jlink_load_line(image, address)}\nr\ng\nqc\n",
        encoding="ascii",
    )
    command = [
        str(jlink),
        "-device", jlink_device,
        "-if", "SWD",
        "-speed", "4000",
        "-autoconnect", "1",
        "-NoGui", "1",
        "-ExitOnError", "1",
        "-CommandFile", str(command_file),
    ]
    result = run_capture(command, timeout=180)
    logged = read_text_auto(log_path) if log_path.is_file() else ""
    output = (logged + "\n" + (result.stdout or "") + (result.stderr or "")).strip()
    return jlink_flash_succeeded(result.returncode, output), output


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        try:
            sock.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _free_port(preferred: int) -> int:
    if not _port_open(preferred):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_port(port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_open(port):
            return True
        time.sleep(0.2)
    return False


def _stop(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()


def start_gdb_server(probe: str, device: str, reset: bool = True) -> tuple[subprocess.Popen[str] | None, int, str]:
    if probe == "stlink":
        openocd = find_openocd()
        if not openocd:
            return None, 0, "openocd 不在 PATH 中"
        port = _free_port(3333)
        command = [str(openocd)]
        scripts = openocd_scripts(openocd)
        if scripts:
            command.extend(["-s", str(scripts)])
        command.extend([
            "-f", "interface/stlink.cfg",
            "-f", openocd_target_cfg(device),
            "-c", f"gdb_port {port}",
            "-c", "tcl_port disabled",
            "-c", "telnet_port disabled",
        ])
        if not reset:
            command.extend(["-c", "reset_config none"])
    else:
        server = find_jlink_gdb(find_jlink())
        if not server:
            return None, 0, "找不到 JLinkGDBServerCL"
        port = _free_port(2331)
        command = [
            str(server),
            "-select", "USB",
            "-device", to_jlink_device(device),
            "-if", "SWD",
            "-speed", "4000",
            "-port", str(port),
            "-nogui",
            "-singlerun",
            "-noir",
        ]
        if not reset:
            command.append("-noreset")
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if not _wait_port(port, 15):
        output = ""
        _stop(process)
        if process.stdout:
            output = process.stdout.read()[-2000:]
        return None, port, f"GDB 端口 {port} 未打开\n{output}"
    return process, port, ""


def run_gdb(elf: Path | None, port: int, commands: list[str] | None, probe: str, mode: str = "debug") -> tuple[bool, str]:
    gdb = find_gdb()
    if not gdb:
        return False, "找不到 arm-none-eabi-gdb 或 gdb-multiarch"
    sequence = [
        "set pagination off",
        "set confirm off",
        f"target extended-remote 127.0.0.1:{port}",
    ]
    if mode == "diagnose":
        sequence.extend([
            "monitor halt",
            "info registers",
            "bt",
            "x/wx 0xE000ED28",
            "x/wx 0xE000ED2C",
            "x/wx 0xE000ED34",
            "x/wx 0xE000ED38",
        ])
    elif commands:
        sequence.extend(commands)
    elif probe == "stlink":
        sequence.extend(["monitor reset halt", "break main", "continue", "bt", "info locals"])
    else:
        sequence.extend(["monitor reset", "monitor halt", "break main", "continue", "bt", "info locals"])
    gdb_args = [str(gdb), "--nx", "--batch"]
    if elf is not None:
        gdb_args.extend(["--se", str(elf)])
    for command in sequence:
        gdb_args.extend(["-ex", command])
    try:
        result = run_capture(gdb_args, timeout=30)
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        output = ((exc.stdout or "") + (exc.stderr or "")).strip()
        timed_out = True
    if mode == "diagnose":
        output = annotate_faults(output)
        ok = not timed_out and diagnose_succeeded(output)
        if timed_out:
            output += "\nGDB 在 30 秒内没有结束。目标可能没有停住。"
        return ok, output
    reached = "main" in output and ("Breakpoint" in output or "#" in output)
    ok = not timed_out and reached
    if timed_out:
        output += "\nGDB 在 30 秒内没有结束。固件可能没有停在 main。"
    return ok, output


def decode_cfsr(value: int) -> list[str]:
    return [name for bit, name in CFSR_BITS if value & (1 << bit)]


def annotate_faults(output: str) -> str:
    found = re.search(r"0xe000ed28:\s+0x([0-9a-fA-F]+)", output, re.I)
    if not found:
        return output
    names = decode_cfsr(int(found.group(1), 16))
    return output + "\nCFSR bits: " + (", ".join(names) if names else "(none)")


def diagnose_succeeded(output: str) -> bool:
    lowered = output.lower()
    if "0xe000ed28" in lowered or "cfsr bits:" in lowered:
        return True
    return bool(re.search(r"\bpc\b", lowered)) and "0x" in lowered


def has_freertos(project: Path) -> bool:
    for dirpath, dirnames, filenames in os.walk(project):
        _walk_skip(dirnames)
        if "FreeRTOS.h" in filenames:
            return True
    return False


def posix_path(path: Path | None) -> str:
    if path is None:
        return ""
    return path.as_posix()


def find_svd(project: Path, device: str) -> tuple[str, str]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(project):
        _walk_skip(dirnames)
        for filename in filenames:
            if filename.lower().endswith(".svd"):
                files.append(Path(dirpath) / filename)
    if not files:
        return "", ""
    token = to_jlink_device(device).upper()
    named = [path for path in files if token and token in path.name.upper()]
    chosen = named[0] if len(named) == 1 else (files[0] if len(files) == 1 else None)
    if chosen is None:
        return "", "找到多个 SVD，未写入 svdFile"
    return chosen.relative_to(project).as_posix(), ""


def parse_length(token: str) -> int:
    token = token.strip().rstrip(",")
    match = re.fullmatch(r"0x([0-9A-Fa-f]+)", token)
    if match:
        return int(match.group(1), 16)
    match = re.fullmatch(r"(\d+)([KMG])?", token, re.I)
    if not match:
        raise ValueError(f"无法解析 LENGTH {token}")
    value = int(match.group(1))
    scale = {"": 1, "K": 1024, "M": 1024 * 1024, "G": 1024 ** 3}
    return value * scale[(match.group(2) or "").upper()]


def parse_memory(text: str) -> list[dict]:
    regions = []
    for name, origin, length in _MEM_LINE.findall(text):
        regions.append({
            "name": name,
            "origin": int(origin, 16),
            "length": parse_length(length),
            "used": 0,
        })
    return regions


def parse_size_sections(text: str) -> list[dict]:
    return [
        {"name": name, "size": int(size), "addr": int(addr, 16)}
        for name, size, addr in _SIZE_LINE.findall(text)
    ]


def parse_nm_symbols(text: str) -> list[dict]:
    return [
        {"address": int(address, 16), "size": int(size, 16), "kind": kind, "name": name}
        for address, size, kind, name in _NM_LINE.findall(text)
    ]


def assign_regions(sections: list[dict], regions: list[dict]) -> None:
    for section in sections:
        for region in regions:
            if region["origin"] <= section["addr"] < region["origin"] + region["length"]:
                region["used"] += section["size"]
                break


def format_size_report(regions: list[dict], symbols: list[dict], note: str) -> str:
    lines = []
    if note:
        lines.append(note)
    if regions:
        for region in regions:
            percent = (100.0 * region["used"] / region["length"]) if region["length"] else 0.0
            lines.append(
                f"{region['name']:<12} 0x{region['origin']:08X}  {region['used']} / {region['length']} ({percent:.1f}%)"
            )
    text_syms = sorted(
        (item for item in symbols if item["kind"] in {"T", "t", "W", "w"}),
        key=lambda item: item["size"],
        reverse=True,
    )
    data_syms = sorted(
        (item for item in symbols if item["kind"] in {"D", "d", "B", "b"}),
        key=lambda item: item["size"],
        reverse=True,
    )
    lines.append("text/weak:")
    lines.extend(f"  {item['size']:8d} {item['kind']} {item['name']}" for item in text_syms[:10])
    lines.append("data/bss:")
    lines.extend(f"  {item['size']:8d} {item['kind']} {item['name']}" for item in data_syms[:10])
    return "\n".join(lines)


def find_binutil(name: str) -> Path | None:
    found = shutil.which(name)
    if found:
        return Path(found)
    suffix = ".exe" if sys.platform == "win32" else ""
    manual = Path("C:/arm-gcc/bin") / f"{name}{suffix}"
    return manual if manual.is_file() else None


def find_linker_script(project: Path) -> tuple[Path | None, str]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(project):
        _walk_skip(dirnames)
        for filename in filenames:
            if filename.lower().endswith(".ld"):
                found.append(Path(dirpath) / filename)
    flash = [path for path in found if "FLASH" in path.name.upper()]
    if len(flash) == 1:
        return flash[0], ""
    if len(found) == 1:
        return found[0], ""
    if found:
        return None, "有多个链接脚本，未按 MEMORY 汇总"
    return None, "没有链接脚本"


def build_size_report(project: Path, elf: Path) -> str:
    size_tool = find_binutil("arm-none-eabi-size")
    nm_tool = find_binutil("arm-none-eabi-nm")
    if size_tool is None or nm_tool is None:
        raise RuntimeError("找不到 arm-none-eabi-size 或 arm-none-eabi-nm")
    size_run = run_capture([str(size_tool), "-A", str(elf)], timeout=30)
    nm_run = run_capture([str(nm_tool), "-S", "--size-sort", str(elf)], timeout=30)
    linker, note = find_linker_script(project)
    regions = parse_memory(linker.read_text(encoding="utf-8", errors="replace")) if linker else []
    sections = parse_size_sections((size_run.stdout or "") + (size_run.stderr or ""))
    symbols = parse_nm_symbols((nm_run.stdout or "") + (nm_run.stderr or ""))
    assign_regions(sections, regions)
    return format_size_report(regions, symbols, note)


def find_rtt_logger(jlink: Path | None) -> Path | None:
    names = ["JLinkRTTLogger.exe", "JLinkRTTLogger"]
    if jlink:
        for name in names:
            sibling = jlink.with_name(name)
            if sibling.is_file():
                return sibling
    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def read_serial_config(project: Path) -> tuple[str, int] | None:
    path = project / ".embeddedskills" / "config.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    serial = data.get("serial") if isinstance(data, dict) else None
    if not isinstance(serial, dict):
        return None
    port = str(serial.get("port") or "").strip()
    if not port:
        return None
    try:
        baud = int(serial.get("baudrate") or 115200)
    except (TypeError, ValueError):
        baud = 115200
    return port, baud


def _port_name_ok(port: str) -> bool:
    return re.fullmatch(r"COM\d+|/dev/tty\w+", port, re.I) is not None


def capture_rtt(device: str, seconds: float) -> tuple[str, str]:
    logger = find_rtt_logger(find_jlink())
    if logger is None:
        return "", "没有 JLinkRTTLogger"
    outfile = Path(tempfile.gettempdir()) / "stm32-cmake-devkit-rtt.log"
    if outfile.exists():
        outfile.unlink()
    command = [
        str(logger),
        "-Device", to_jlink_device(device),
        "-If", "SWD",
        "-Speed", "4000",
        "-RTTChannel", "0",
        str(outfile),
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    time.sleep(max(0.5, seconds))
    _stop(process)
    tool_out = process.stdout.read()[-1500:] if process.stdout else ""
    if outfile.is_file() and outfile.stat().st_size:
        return read_text_auto(outfile).strip(), tool_out
    return "", (tool_out or "RTT 日志为空").strip()


def capture_serial(port: str, baud: int, seconds: float) -> tuple[str, str]:
    if not _port_name_ok(port):
        return "", f"串口名不合法: {port}"
    if sys.platform == "win32":
        script = (
            f"$serial = New-Object System.IO.Ports.SerialPort '{port}',{int(baud)}; "
            "$serial.ReadTimeout = 200; $serial.Open(); "
            f"$deadline = (Get-Date).AddSeconds({float(seconds)}); "
            "$builder = New-Object System.Text.StringBuilder; "
            "while ((Get-Date) -lt $deadline) { "
            "try { [void]$builder.Append($serial.ReadExisting()) } catch {} ; "
            "Start-Sleep -Milliseconds 50 }; "
            "$serial.Close(); Write-Output $builder.ToString()"
        )
        result = run_capture(["powershell", "-NoProfile", "-Command", script], timeout=int(seconds) + 20)
        text = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        if result.returncode != 0 and not text:
            return "", err or "串口打开失败"
        return text, err
    return _capture_serial_posix(port, baud, seconds)


def _capture_serial_posix(port: str, baud: int, seconds: float) -> tuple[str, str]:
    import select
    import termios

    flag = getattr(termios, f"B{int(baud)}", None)
    if flag is None:
        return "", f"termios 没有波特率 {baud}"
    try:
        fd = os.open(port, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError as exc:
        return "", str(exc)
    try:
        attrs = termios.tcgetattr(fd)
        attrs[4] = flag
        attrs[5] = flag
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        chunks: list[bytes] = []
        deadline = time.time() + seconds
        while time.time() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.2)
            if not ready:
                continue
            try:
                data = os.read(fd, 4096)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
        return b"".join(chunks).decode("utf-8", "replace"), ""
    finally:
        os.close(fd)


def capture_boot_log(project: Path, device: str, seconds: float) -> tuple[str, str, str]:
    if find_rtt_logger(find_jlink()):
        text, note = capture_rtt(device, seconds)
        return "rtt", text, note
    serial = read_serial_config(project)
    if not serial:
        return "", "", "没有 JLinkRTTLogger，工程里也没有 serial.port"
    text, note = capture_serial(serial[0], serial[1], seconds)
    return "serial", text, note


def write_launch(project: Path, elf: Path, device: str, force: bool) -> tuple[Path, bool, str]:
    vscode = project / ".vscode"
    vscode.mkdir(parents=True, exist_ok=True)
    destination = vscode / "launch.json"
    replaced = True
    if destination.exists() and not force:
        destination = vscode / "launch.stm32-cmake.json"
        replaced = False
    openocd = find_openocd()
    scripts = openocd_scripts(openocd) if openocd else None
    gdb = find_gdb()
    jlink_server = find_jlink_gdb(find_jlink())
    try:
        target = openocd_target_cfg(device)
    except ValueError:
        target = "target/stm32h7x.cfg"
    config = {
        "version": "0.2.0",
        "configurations": [
            {
                "name": "Debug (J-Link)",
                "type": "cortex-debug",
                "request": "launch",
                "cwd": "${workspaceFolder}",
                "executable": "${workspaceFolder}/" + elf.relative_to(project).as_posix(),
                "servertype": "jlink",
                "serverpath": posix_path(jlink_server) or "JLinkGDBServerCL",
                "gdbPath": posix_path(gdb) or "arm-none-eabi-gdb",
                "device": to_jlink_device(device),
                "interface": "swd",
                "runToEntryPoint": "main",
            },
            {
                "name": "Debug (ST-Link OpenOCD)",
                "type": "cortex-debug",
                "request": "launch",
                "cwd": "${workspaceFolder}",
                "executable": "${workspaceFolder}/" + elf.relative_to(project).as_posix(),
                "servertype": "openocd",
                "serverpath": posix_path(openocd) or "openocd",
                "configFiles": ["interface/stlink.cfg", target],
                "gdbPath": posix_path(gdb) or "arm-none-eabi-gdb",
                "device": to_jlink_device(device),
                "interface": "swd",
                "runToEntryPoint": "main",
            },
        ],
    }
    if scripts:
        config["configurations"][1]["searchDir"] = [posix_path(scripts)]
    svd, svd_note = find_svd(project, device)
    attaches = []
    for item in config["configurations"]:
        attach = dict(item)
        attach["name"] = item["name"].replace("Debug", "Attach", 1)
        attach["request"] = "attach"
        attach.pop("runToEntryPoint", None)
        attaches.append(attach)
    config["configurations"].extend(attaches)
    if svd:
        for item in config["configurations"]:
            item["svdFile"] = "${workspaceFolder}/" + svd
    if has_freertos(project):
        for item in config["configurations"]:
            item["rtos"] = "FreeRTOS"
    destination.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return destination, replaced, svd_note


def self_test() -> int:
    sample = "\n".join([
        r"USB\VID_0483&PID_374B&MI_00\6&ABC",
        r"USB\VID_0483&PID_374B&MI_01\6&ABC",
        r"USB\VID_0483&PID_374B&MI_02\6&ABC",
        r"USB\VID_0483&PID_5740\7&CDC",
        r"USB\VID_1366&PID_0105&MI_00\8&JL",
        r"USB\VID_1366&PID_0105&MI_01\8&JL",
        r"USB\VID_0483&PID_3748\SN2",
    ])
    counts = counts_from_text(sample)
    memory = parse_memory("FLASH (rx) : ORIGIN = 0x08000000, LENGTH = 1024K\nRAM (xrw) : ORIGIN = 0x20000000, LENGTH = 0x20000\n")
    sections = parse_size_sections(".text  100  0x08000000\n.bss  16  0x20000000\n")
    assign_regions(sections, memory)
    symbols = parse_nm_symbols("08000000 00000040 T main\n20000000 00000010 B buffer\n08000100 00000004 W weak_fn\n")
    checks = [
        to_jlink_device("STM32H743ZITx") == "STM32H743ZI",
        to_jlink_device("STM32F407VGTx") == "STM32F407VG",
        to_jlink_device("STM32F103C8Tx") == "STM32F103C8",
        to_jlink_device("STM32H743ZI") == "STM32H743ZI",
        openocd_target_cfg("STM32H743ZITx") == "target/stm32h7x.cfg",
        openocd_target_cfg("STM32F407VG") == "target/stm32f4x.cfg",
        openocd_target_cfg("STM32G431CB") == "target/stm32g4x.cfg",
        openocd_target_cfg("STM32L476RG") == "target/stm32l4x.cfg",
        openocd_target_cfg("STM32WBA52CG") == "target/stm32wbax.cfg",
        counts == {"stlink": 2, "jlink": 1},
        decode_cfsr(1) == ["IACCVIOL"],
        decode_cfsr(1 << 9) == ["PRECISERR"],
        decode_cfsr(1 << 16) == ["UNDEFINSTR"],
        decode_cfsr(0) == [],
        parse_length("128K") == 128 * 1024,
        parse_length("0x20000") == 0x20000,
        memory[0]["used"] == 100 and memory[1]["used"] == 16,
        {item["name"] for item in symbols} == {"main", "buffer", "weak_fn"},
        "main" in format_size_report(memory, symbols, ""),
        jlink_load_line(Path("app.elf"), "") == "loadfile app.elf",
        jlink_load_line(Path("app.bin"), "0x08000000") == "loadbin app.bin 0x08000000",
        jlink_flash_succeeded(0, "O.K.\nFlash download: Total"),
        not jlink_flash_succeeded(0, "cannot connect to target\nO.K."),
        not jlink_flash_succeeded(1, "O.K.\nFlash download"),
    ]
    if not all(checks):
        print("probe_debug self-test failed", counts, memory, symbols)
        return 1
    try:
        resolve_probe("auto", {"stlink": 1, "jlink": 1})
        print("both-probe guard failed")
        return 1
    except RuntimeError:
        pass
    try:
        program_command(Path("app.bin"), "")
        print("bin address guard failed")
        return 1
    except ValueError:
        pass
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        (root / "CMakeLists.txt").write_text("# STM32_DEVICE: STM32F407VGTx\n", encoding="utf-8")
        if find_device(root, None) != "STM32F407VGTx":
            print("cmake device marker failed")
            return 1
        if find_device(root, "STM32H743ZI") != "STM32H743ZI":
            print("explicit device failed")
            return 1
        (root / "CMakeLists.txt").write_text("project(x)\n", encoding="utf-8")
        keil = root / "MDK-ARM"
        keil.mkdir()
        (keil / "demo.uvprojx").write_text("<Device>STM32F103C8</Device>\n", encoding="utf-8")
        if find_device(root, None) != "STM32F103C8":
            print("uvprojx device failed")
            return 1
        config = root / ".embeddedskills"
        config.mkdir()
        (config / "config.json").write_text('{"jlink":{"device":"STM32H743ZI"}}\n', encoding="utf-8")
        if find_device(root, None) != "STM32H743ZI":
            print("config device priority failed")
            return 1
        log_path = root / "jlink.log"
        log_path.write_bytes("O.K.\nFlash download: ok\n".encode("utf-16-le"))
        if "O.K." not in read_text_auto(log_path):
            print("utf-16 log read failed")
            return 1
        (root / "STM32F103.svd").write_text("<device/>\n", encoding="utf-8")
        (root / "app.elf").write_bytes(b"")
        launch_path, replaced, note = write_launch(root, root / "app.elf", "STM32F103C8", False)
        payload = json.loads(launch_path.read_text(encoding="utf-8"))
        names = [item["name"] for item in payload["configurations"]]
        if names != ["Debug (J-Link)", "Debug (ST-Link OpenOCD)", "Attach (J-Link)", "Attach (ST-Link OpenOCD)"]:
            print("launch names failed", names)
            return 1
        if any(item.get("request") == "attach" and "runToEntryPoint" in item for item in payload["configurations"]):
            print("attach still runs to main")
            return 1
        if not all(item.get("svdFile", "").endswith("STM32F103.svd") for item in payload["configurations"]):
            print("svd missing", note)
            return 1
        if not replaced:
            print("fresh launch.json should be primary")
            return 1
    print("probe_debug self-test ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(description="识别 ST-Link/J-Link 并烧录、调试")
    parser.add_argument(
        "action",
        nargs="?",
        choices=["detect", "flash", "debug", "gen-launch", "diagnose", "size-report", "log"],
        help="不填且带 --self-test 时只跑自检",
    )
    parser.add_argument("--project", default=".")
    parser.add_argument("--elf", default="")
    parser.add_argument("--device", default="")
    parser.add_argument("--probe", default="auto", choices=["auto", "stlink", "jlink"])
    parser.add_argument("--address", default="", help=".bin 烧录地址，例如 0x08000000")
    parser.add_argument("--verify-boot", action="store_true", help="烧录成功后抓约 3 秒 RTT 或串口日志")
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--commands", nargs="*", default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    if not args.action:
        parser.error("需要 action: detect, flash, debug, diagnose, size-report, log, gen-launch")
    project = Path(args.project).resolve()
    needs_probe = args.action in {"detect", "flash", "debug", "diagnose"}
    counts = detect_counts() if needs_probe else {"stlink": 0, "jlink": 0}
    try:
        if args.action == "detect":
            try:
                probe = resolve_probe(args.probe, counts)
                summary = f"探针: {probe}"
                status = "ok"
                code = 0
            except RuntimeError as exc:
                probe = ""
                summary = str(exc)
                status = "error"
                code = 1
            return emit({
                "status": status,
                "action": "detect",
                "summary": summary,
                "details": {"counts": counts, "probe": probe},
            }, args.json, code)

        if args.action == "size-report":
            elf = find_elf(project, args.elf or None)
            report = build_size_report(project, elf)
            return emit({
                "status": "ok",
                "action": "size-report",
                "summary": f"已统计 {elf.name}",
                "details": {"elf": str(elf), "report": report},
            }, args.json, 0)

        if args.action == "log":
            device = find_device(project, args.device or None)
            source, text, note = capture_boot_log(project, device, args.seconds)
            status = "ok" if text.strip() else "partial"
            summary = f"{source or '日志'} {'已抓到' if text.strip() else '为空'}"
            return emit({
                "status": status,
                "action": "log",
                "summary": summary,
                "details": {"device": device, "source": source, "boot_log": text or note},
            }, args.json, 0 if status in {"ok", "partial"} else 1)

        device = find_device(project, args.device or None)
        if args.action == "gen-launch":
            elf = find_elf(project, args.elf or None)
            path, primary, note = write_launch(project, elf, device, args.force)
            return emit({
                "status": "ok",
                "action": "gen-launch",
                "summary": f"已写入 {path.name}",
                "details": {
                    "path": str(path),
                    "replaced_primary": primary,
                    "device": device,
                    "elf": str(elf),
                    "note": note,
                },
            }, args.json, 0)

        probe = resolve_probe(args.probe, counts)
        if args.action == "flash":
            image = find_elf(project, args.elf or None)
            if probe == "stlink":
                ok, output = flash_stlink(image, device, args.address)
            else:
                ok, output = flash_jlink(image, device, args.address)
            status = "ok" if ok else "error"
            summary = f"{probe} 烧录{'成功' if ok else '失败'}"
            boot_log = ""
            source = ""
            if ok and args.verify_boot:
                source, boot_log, note = capture_boot_log(project, device, args.seconds)
                if boot_log.strip():
                    summary += f"；{source} 启动日志已抓到"
                else:
                    status = "partial"
                    summary += "；启动日志为空"
                    boot_log = note
            return emit({
                "status": status,
                "action": "flash",
                "summary": summary,
                "details": {
                    "probe": probe,
                    "device": device,
                    "elf": str(image),
                    "source": source,
                    "boot_log": boot_log[-4000:],
                    "output": output[-8000:],
                },
            }, args.json, 0 if status in {"ok", "partial"} else 1)

        elf = None
        if args.action != "diagnose" or args.elf:
            elf = find_elf(project, args.elf or None)
        elif args.action == "diagnose":
            try:
                elf = find_elf(project, None)
            except FileNotFoundError:
                elf = None
        reset = args.action != "diagnose"
        server, port, server_error = start_gdb_server(probe, device, reset=reset)
        if server is None:
            return emit({
                "status": "error",
                "action": args.action,
                "summary": server_error.splitlines()[0] if server_error else "GDB Server 启动失败",
                "details": {"probe": probe, "device": device, "elf": str(elf or ""), "output": server_error},
            }, args.json, 1)
        try:
            ok, gdb_output = run_gdb(elf, port, args.commands, probe, mode=args.action if args.action == "diagnose" else "debug")
        finally:
            _stop(server)
        if args.action == "diagnose":
            summary = "已停住并读到寄存器或故障状态" if ok else "没有读到寄存器或故障状态"
        else:
            summary = "已停在 main" if ok else "调试会话没有确认停在 main"
        return emit({
            "status": "ok" if ok else "error",
            "action": args.action,
            "summary": summary,
            "details": {
                "probe": probe,
                "device": device,
                "elf": str(elf or ""),
                "gdb_port": port,
                "gdb_output": gdb_output[-8000:],
            },
        }, args.json, 0 if ok else 1)
    except (RuntimeError, ValueError, FileNotFoundError) as exc:
        return emit({
            "status": "error",
            "action": args.action,
            "summary": str(exc),
            "details": {"counts": counts},
        }, args.json, 1)


if __name__ == "__main__":
    sys.exit(main())
