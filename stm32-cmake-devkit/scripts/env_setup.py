"""检测并安装 STM32 命令行工具链（Windows / Linux）。只使用标准库。"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path


OPENOCD_VERSION = "0.12.0-7"
OPENOCD_ZIP = {
    "Windows": f"https://github.com/xpack-dev-tools/openocd-xpack/releases/download/v{OPENOCD_VERSION}/xpack-openocd-{OPENOCD_VERSION}-win32-x64.zip",
    "Linux-x64": f"https://github.com/xpack-dev-tools/openocd-xpack/releases/download/v{OPENOCD_VERSION}/xpack-openocd-{OPENOCD_VERSION}-linux-x64.tar.gz",
    "Linux-arm64": f"https://github.com/xpack-dev-tools/openocd-xpack/releases/download/v{OPENOCD_VERSION}/xpack-openocd-{OPENOCD_VERSION}-linux-arm64.tar.gz",
}

WINGET_IDS = {
    "gcc": ["Arm.GnuArmEmbeddedToolchain"],
    "cmake": ["Kitware.CMake"],
    "ninja": ["Ninja-build.Ninja"],
    "clangd": ["LLVM.LLVM"],
    "git": ["Git.Git"],
    "jlink": ["NordicSemiconductor.JLink", "SEGGER.J-Link"],
    "cubemx": ["STMicroelectronics.STM32CubeMX"],
}

VSCODE_EXTENSIONS = [
    "llvm-vs-code-extensions.vscode-clangd",
    "ms-vscode.cmake-tools",
    "marus25.cortex-debug",
]

# install_with: 这些工具随 gcc / jlink 包一起出现，不单独下载。
TOOLS = [
    {"id": "gcc", "required": True, "commands": ["arm-none-eabi-gcc"], "version": ["--version"]},
    {"id": "gdb", "required": True, "commands": ["arm-none-eabi-gdb", "gdb-multiarch"], "version": ["--version"], "install_with": "gcc"},
    {"id": "objcopy", "required": True, "commands": ["arm-none-eabi-objcopy"], "version": ["--version"], "install_with": "gcc"},
    {"id": "size", "required": True, "commands": ["arm-none-eabi-size"], "version": ["--version"], "install_with": "gcc"},
    {"id": "cmake", "required": True, "commands": ["cmake"], "version": ["--version"]},
    {"id": "ninja", "required": True, "commands": ["ninja"], "version": ["--version"]},
    {"id": "clangd", "required": True, "commands": ["clangd"], "version": ["--version"]},
    {"id": "openocd", "required": True, "commands": ["openocd"], "version": ["--version"]},
    {"id": "git", "required": False, "commands": ["git"], "version": ["--version"]},
    {"id": "jlink", "required": False, "commands": ["JLink.exe", "JLinkExe"], "version": None},
    {"id": "jlink-gdb", "required": False, "commands": ["JLinkGDBServerCL.exe", "JLinkGDBServerCLExe"], "version": None, "install_with": "jlink"},
    {"id": "cubemx", "required": False, "commands": ["STM32CubeMX.exe", "STM32CubeMX"], "version": None},
]


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def log(message: str) -> None:
    print(message, file=sys.stderr)


def emit(payload: dict, as_json: bool, code: int) -> int:
    if as_json:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print(payload.get("summary", ""))
        for tool in payload.get("details", {}).get("tools", []):
            state = "ok" if tool["found"] else "MISSING"
            extra = tool.get("version") or tool.get("path") or ""
            print(f"  [{state}] {tool['id']}  {extra}")
        for step in payload.get("next_actions", []):
            print(f"  next: {step}")
        errors = payload.get("details", {}).get("install_errors") or []
        for err in errors:
            print(f"  error: {err}")
    return code


def which(name: str) -> str | None:
    found = shutil.which(name)
    return found


def run_capture(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def version_of(path: str, args: list[str] | None) -> str:
    if not args:
        return "present"
    try:
        result = run_capture([path, *args], timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"present ({exc.__class__.__name__})"
    text = (result.stdout or result.stderr or "").strip().splitlines()
    return text[0] if text else "present"


def _program_files() -> list[Path]:
    roots = []
    for key in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        value = os.environ.get(key)
        if value:
            roots.append(Path(value))
    return roots


def _first_existing(candidates: list[Path]) -> Path | None:
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _glob_tool(roots: list[Path], relative_parts: list[str], names: list[str]) -> Path | None:
    matches: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        cursor = [root]
        for part in relative_parts:
            nxt: list[Path] = []
            for base in cursor:
                if part == "*":
                    try:
                        nxt.extend(p for p in base.iterdir() if p.is_dir())
                    except OSError:
                        continue
                else:
                    child = base / part
                    if child.is_dir():
                        nxt.append(child)
            cursor = nxt
            if not cursor:
                break
        for base in cursor:
            for name in names:
                found = base / name
                if found.is_file():
                    matches.append(found)
    if not matches:
        return None
    matches.sort(key=lambda path: path.as_posix().lower(), reverse=True)
    return matches[0]


def search_known(tool_id: str) -> Path | None:
    """PATH 之外的常见安装位置。只做浅层匹配，不扫描整个磁盘。"""
    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA", str(home / ".local")))
    roaming = Path(os.environ.get("APPDATA", str(home)))
    names = {
        "gcc": ["arm-none-eabi-gcc.exe", "arm-none-eabi-gcc"],
        "gdb": ["arm-none-eabi-gdb.exe", "arm-none-eabi-gdb", "gdb-multiarch"],
        "objcopy": ["arm-none-eabi-objcopy.exe", "arm-none-eabi-objcopy"],
        "size": ["arm-none-eabi-size.exe", "arm-none-eabi-size"],
        "cmake": ["cmake.exe", "cmake"],
        "ninja": ["ninja.exe", "ninja"],
        "clangd": ["clangd.exe", "clangd"],
        "openocd": ["openocd.exe", "openocd"],
        "git": ["git.exe", "git"],
        "jlink": ["JLink.exe", "JLinkExe"],
        "jlink-gdb": ["JLinkGDBServerCL.exe", "JLinkGDBServerCLExe"],
        "cubemx": ["STM32CubeMX.exe", "STM32CubeMX"],
    }[tool_id]
    extra: list[Path] = []
    if tool_id in {"gcc", "gdb", "objcopy", "size"}:
        bin_name = names[0]
        for root in _program_files():
            extra.extend([
                root / "Arm GNU Toolchain arm-none-eabi",
                root / "Arm",
            ])
        found = _glob_tool(extra, ["*", "bin"], names)
        if found:
            return found
        found = _glob_tool(
            [root / "Arm" for root in _program_files()],
            ["GNU Toolchain mingw-w64-i686-arm-none-eabi", "bin"],
            names,
        )
        if found:
            return found
        manual = Path("C:/arm-gcc/bin") / bin_name
        if manual.is_file():
            return manual
        return _glob_tool(
            [home / ".local" / "stm32-cmake-devkit" / "arm-gnu-toolchain"],
            ["*", "bin"],
            names,
        )
    if tool_id == "cmake":
        return _first_existing([root / "CMake" / "bin" / "cmake.exe" for root in _program_files()])
    if tool_id == "clangd":
        return _first_existing([root / "LLVM" / "bin" / "clangd.exe" for root in _program_files()])
    if tool_id == "openocd":
        found = _glob_tool(
            [roaming / "xPacks" / "@xpack-dev-tools" / "openocd", home / ".local" / "xPacks" / "@xpack-dev-tools" / "openocd"],
            ["*", ".content", "bin"],
            names,
        )
        if found:
            return found
        return _glob_tool(
            [local / "stm32-cmake-devkit" / "openocd", home / ".local" / "stm32-cmake-devkit" / "openocd"],
            ["*", "bin"],
            names,
        )
    if tool_id in {"jlink", "jlink-gdb"}:
        env_path = os.environ.get("JLINK_PATH", "")
        if tool_id == "jlink" and env_path and Path(env_path).is_file():
            return Path(env_path)
        if tool_id == "jlink-gdb" and env_path:
            sibling = Path(env_path).with_name("JLinkGDBServerCL.exe")
            if sibling.is_file():
                return sibling
        return _glob_tool(
            [root / "SEGGER" for root in _program_files()] + [Path("/opt/SEGGER")],
            ["*"],
            names,
        )
    if tool_id == "cubemx":
        return _first_existing(
            [root / "STMicroelectronics" / "STM32Cube" / "STM32CubeMX" / "STM32CubeMX.exe" for root in _program_files()]
        )
    if tool_id == "git":
        return _first_existing([root / "Git" / "cmd" / "git.exe" for root in _program_files()])
    return None


def locate(tool: dict) -> tuple[str | None, str]:
    for command in tool["commands"]:
        found = which(command)
        if found:
            return found, version_of(found, tool["version"])
    known = search_known(tool["id"])
    if known:
        return str(known), version_of(str(known), tool["version"])
    return None, ""


def collect_status() -> list[dict]:
    rows = []
    for tool in TOOLS:
        path, version = locate(tool)
        rows.append({
            "id": tool["id"],
            "required": tool["required"],
            "found": bool(path),
            "path": path or "",
            "version": version,
        })
    return rows


def prepend_process_path(directories: list[str]) -> None:
    current = os.environ.get("PATH", "")
    parts = current.split(os.pathsep)
    existing = {os.path.normcase(os.path.normpath(part)) for part in parts if part}
    prefix = []
    for directory in directories:
        if directory and Path(directory).is_dir():
            key = os.path.normcase(os.path.normpath(directory))
            if key not in existing:
                prefix.append(directory)
                existing.add(key)
    if prefix:
        os.environ["PATH"] = os.pathsep.join(prefix + parts)


def _broadcast_windows_env() -> None:
    import ctypes

    result = ctypes.c_size_t()
    ctypes.windll.user32.SendMessageTimeoutW(
        0xFFFF,
        0x001A,
        0,
        "Environment",
        0x0002,
        5000,
        ctypes.byref(result),
    )


def append_user_path(directories: list[str]) -> list[str]:
    added: list[str] = []
    usable = [str(Path(directory)) for directory in directories if directory and Path(directory).is_dir()]
    if not usable:
        return added
    if sys.platform == "win32":
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
            try:
                current, reg_type = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                current, reg_type = "", winreg.REG_EXPAND_SZ
            parts = [part for part in str(current).split(";") if part]
            known = {os.path.normcase(os.path.normpath(part)) for part in parts}
            for directory in usable:
                token = os.path.normcase(os.path.normpath(directory))
                if token not in known:
                    parts.append(directory)
                    known.add(token)
                    added.append(directory)
            if added:
                kind = reg_type if reg_type in (winreg.REG_EXPAND_SZ, winreg.REG_SZ) else winreg.REG_EXPAND_SZ
                winreg.SetValueEx(key, "Path", 0, kind, ";".join(parts))
                _broadcast_windows_env()
    else:
        bashrc = Path.home() / ".bashrc"
        text = bashrc.read_text(encoding="utf-8", errors="replace") if bashrc.exists() else ""
        lines = []
        for directory in usable:
            export = f'export PATH="{directory}:$PATH"  # stm32-cmake-devkit'
            if directory not in text and export not in text:
                lines.append(export)
                added.append(directory)
        if lines:
            with bashrc.open("a", encoding="utf-8") as handle:
                handle.write("\n" + "\n".join(lines) + "\n")
    prepend_process_path(added or usable)
    return added


def install_root() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    else:
        base = Path.home() / ".local"
    return base / "stm32-cmake-devkit"


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "stm32-cmake-devkit"})
    with urllib.request.urlopen(request, timeout=180) as response, dest.open("wb") as output:
        shutil.copyfileobj(response, output)


def _safe_members(names: list[str], dest: Path) -> None:
    root = dest.resolve()
    for name in names:
        target = (dest / name).resolve()
        if not target.is_relative_to(root):
            raise RuntimeError(f"压缩包路径越界: {name}")


def extract_openocd(archive: Path, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as package:
            _safe_members(package.namelist(), dest)
            package.extractall(dest)
    else:
        with tarfile.open(archive, "r:gz") as package:
            _safe_members([member.name for member in package.getmembers()], dest)
            package.extractall(dest)
    found = search_known("openocd")
    if found:
        return found
    for name in ("openocd.exe", "openocd"):
        matches = list(dest.rglob(name))
        if matches:
            return matches[0]
    raise RuntimeError(f"解压后未找到 openocd: {dest}")


def install_openocd() -> str:
    if locate(next(tool for tool in TOOLS if tool["id"] == "openocd"))[0]:
        return "already-present"
    xpm = which("xpm")
    if xpm:
        log("xpm install --global @xpack-dev-tools/openocd@0.12.0-7.1")
        result = run_capture(
            [xpm, "install", "--global", "@xpack-dev-tools/openocd@0.12.0-7.1"],
            timeout=300,
        )
        if result.returncode == 0 and locate(next(tool for tool in TOOLS if tool["id"] == "openocd"))[0]:
            return "xpm"
        log((result.stdout or "") + (result.stderr or ""))
    machine = platform.machine().lower()
    if sys.platform == "win32":
        url = OPENOCD_ZIP["Windows"]
    elif machine in {"aarch64", "arm64"}:
        url = OPENOCD_ZIP["Linux-arm64"]
    else:
        url = OPENOCD_ZIP["Linux-x64"]
    root = install_root() / "openocd"
    archive = root / Path(url).name
    log(f"download {url}")
    _download(url, archive)
    binary = extract_openocd(archive, root)
    append_user_path([str(binary.parent)])
    return str(binary)


def winget_install(package_ids: list[str]) -> tuple[bool, str]:
    winget = which("winget")
    if not winget:
        return False, "winget 不在 PATH 中"
    notes = []
    for package_id in package_ids:
        command = [
            winget, "install", "--id", package_id, "--exact",
            "--accept-package-agreements", "--accept-source-agreements",
            "--disable-interactivity",
        ]
        log(" ".join(command))
        try:
            result = run_capture(command, timeout=900)
        except subprocess.TimeoutExpired:
            notes.append(f"{package_id}: timeout")
            continue
        output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
        lowered = output.lower()
        if result.returncode == 0 or "already installed" in lowered or "已安装" in output:
            return True, output[-1500:]
        notes.append(f"{package_id}: exit {result.returncode}\n{output[-800:]}")
    return False, "\n".join(notes)


def linux_packages(manager: str) -> list[str]:
    if manager == "apt":
        return ["cmake", "ninja-build", "clangd", "git", "gcc-arm-none-eabi", "binutils-arm-none-eabi", "gdb-multiarch", "openocd"]
    if manager == "dnf":
        return ["cmake", "ninja-build", "clang-tools-extra", "git", "arm-none-eabi-gcc-cs", "arm-none-eabi-binutils-cs", "gdb", "openocd"]
    return ["cmake", "ninja", "clang", "git", "arm-none-eabi-gcc", "arm-none-eabi-gdb", "openocd"]


def install_linux_packages() -> tuple[bool, str]:
    if which("apt-get"):
        manager = "apt"
        commands = [
            ["sudo", "-n", "apt-get", "update"],
            ["sudo", "-n", "apt-get", "install", "-y", *linux_packages("apt")],
        ]
    elif which("dnf"):
        manager = "dnf"
        commands = [["sudo", "-n", "dnf", "install", "-y", *linux_packages("dnf")]]
    elif which("pacman"):
        manager = "pacman"
        commands = [["sudo", "-n", "pacman", "-Sy", "--noconfirm", *linux_packages("pacman")]]
    else:
        return False, "未找到 apt-get / dnf / pacman"
    chunks = []
    for command in commands:
        log(" ".join(command))
        result = run_capture(command, timeout=900)
        chunks.append((result.stdout or "") + (result.stderr or ""))
        if result.returncode != 0:
            hint = " ".join(command).replace(" -n", "")
            return False, f"{manager} 失败。请在终端执行: {hint}\n" + chunks[-1][-800:]
    return True, "\n".join(chunks)[-1500:]


def install_editor_extensions() -> tuple[bool, str]:
    launcher = which("cursor") or which("code")
    if not launcher:
        return False, "未找到 cursor 或 code 命令，扩展需在编辑器里手动安装: " + ", ".join(VSCODE_EXTENSIONS)
    notes = []
    ok = True
    for extension in VSCODE_EXTENSIONS:
        result = run_capture([launcher, "--install-extension", extension], timeout=180)
        if result.returncode != 0:
            ok = False
        notes.append((result.stdout or result.stderr or extension).strip().splitlines()[-1:])
    flat = [line for group in notes for line in group]
    return ok, "\n".join(flat)


def path_dirs_for_found(rows: list[dict]) -> list[str]:
    directories = []
    for row in rows:
        if row["path"]:
            directories.append(str(Path(row["path"]).parent))
    return directories


def ids_to_install(only: str, with_cubemx: bool, with_editor: bool) -> list[str]:
    if only.strip():
        return [item.strip() for item in only.split(",") if item.strip()]
    selected = ["gcc", "cmake", "ninja", "clangd", "openocd", "git", "jlink"]
    if with_cubemx:
        selected.append("cubemx")
    if with_editor:
        selected.append("editor")
    return selected


def install_one(tool_id: str) -> tuple[bool, str]:
    if tool_id == "openocd":
        try:
            return True, install_openocd()
        except Exception as exc:  # noqa: BLE001 - 把下载/解压错误回给调用方
            return False, str(exc)
    if tool_id == "editor":
        return install_editor_extensions()
    if sys.platform == "win32":
        packages = WINGET_IDS.get(tool_id)
        if not packages:
            return False, f"Windows 上没有 {tool_id} 的自动安装包"
        return winget_install(packages)
    if tool_id == "jlink":
        return False, "Linux 的 J-Link 软件需从 https://www.segger.com/downloads/jlink 安装，并加入 PATH"
    if tool_id == "cubemx":
        return False, "Linux 的 STM32CubeMX 需从 ST 官网安装"
    return True, "linux-packages"


def do_install(selected: list[str], as_json: bool) -> int:
    before = collect_status()
    by_id = {row["id"]: row for row in before}
    errors = []
    installed = []
    linux_bundle_done = False
    for tool_id in selected:
        if tool_id in {"gdb", "objcopy", "size", "jlink-gdb"}:
            continue
        row = by_id.get(tool_id)
        if row and row["found"]:
            continue
        if sys.platform != "win32" and tool_id in {"gcc", "cmake", "ninja", "clangd", "openocd", "git"}:
            if not linux_bundle_done:
                ok, note = install_linux_packages()
                linux_bundle_done = True
                if ok:
                    installed.append("linux-packages")
                else:
                    errors.append(note)
            continue
        ok, note = install_one(tool_id)
        if ok:
            installed.append(tool_id)
        else:
            errors.append(f"{tool_id}: {note}")
    after = collect_status()
    added = append_user_path(path_dirs_for_found(after))
    # PATH 更新后再查一次，避免只找到了文件但 which 仍失败。
    prepend_process_path(added)
    after = collect_status()
    missing_required = [row["id"] for row in after if row["required"] and not row["found"]]
    status = "ok" if not missing_required and not errors else "error"
    if not missing_required and errors:
        status = "partial"
    payload = {
        "status": status,
        "action": "install",
        "summary": "安装完成" if status == "ok" else "安装未完成，见 install_errors / missing",
        "details": {
            "tools": after,
            "installed": installed,
            "path_added": added,
            "install_errors": errors,
            "missing_required": missing_required,
        },
        "next_actions": ["新开一个终端后再执行 check"] if added else [],
    }
    code = 0 if status in {"ok", "partial"} and not missing_required else 1
    return emit(payload, as_json, code)


def do_check(as_json: bool) -> int:
    rows = collect_status()
    missing_required = [row["id"] for row in rows if row["required"] and not row["found"]]
    missing_optional = [row["id"] for row in rows if not row["required"] and not row["found"]]
    if missing_required:
        status = "error"
        summary = "缺少必需工具: " + ", ".join(missing_required)
        code = 1
    elif missing_optional:
        status = "partial"
        summary = "必需工具已就绪；未安装: " + ", ".join(missing_optional)
        code = 0
    else:
        status = "ok"
        summary = "工具链已就绪"
        code = 0
    payload = {
        "status": status,
        "action": "check",
        "summary": summary,
        "details": {
            "os": sys.platform,
            "tools": rows,
            "missing_required": missing_required,
            "missing_optional": missing_optional,
        },
        "next_actions": (["python env_setup.py install --json"] if missing_required else []),
    }
    return emit(payload, as_json, code)


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(description="检测或安装 STM32 CMake 工具链")
    parser.add_argument("action", choices=["check", "install"])
    parser.add_argument("--only", default="", help="逗号分隔的工具 id，例如 gcc,cmake,openocd")
    parser.add_argument("--with-cubemx", action="store_true", help="一并尝试安装 STM32CubeMX")
    parser.add_argument("--with-editor", action="store_true", help="安装 clangd / CMake Tools / Cortex-Debug 扩展")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.action == "check":
        return do_check(args.json)
    selected = ids_to_install(args.only, args.with_cubemx, args.with_editor)
    if args.only and args.with_editor and "editor" not in selected:
        selected.append("editor")
    return do_install(selected, args.json)


if __name__ == "__main__":
    sys.exit(main())
