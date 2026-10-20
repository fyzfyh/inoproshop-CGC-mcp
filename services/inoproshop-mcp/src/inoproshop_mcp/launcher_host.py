"""Discover and connect a desktop InoProShop without touching engineering files.

The existing bridge remains the authority for native operations.  This module
only starts its normal desktop ScriptEngine entry point and checks that the UI
thread responds through the public MCP interface.
"""
from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid


class LauncherHostError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _path_key(path) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve())).casefold()


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _process_executable(pid: int) -> str | None:
    if os.name != "nt":
        return None
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return None
    finally:
        kernel.CloseHandle(handle)


def _process_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(int(pid), 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        # Access denied is not proof that a process has exited.
        return ctypes.get_last_error() == 5
    try:
        code = wintypes.DWORD()
        return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
    finally:
        kernel.CloseHandle(handle)


def _registry_locations() -> list[str]:
    if os.name != "nt":
        return []
    import winreg
    found = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\InoProShop.exe", 0, winreg.KEY_READ | view) as key:
                    found.append(winreg.QueryValueEx(key, None)[0])
            except OSError:
                pass
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 0, winreg.KEY_READ | view) as uninstall:
                    for index in range(winreg.QueryInfoKey(uninstall)[0]):
                        try:
                            with winreg.OpenKey(uninstall, winreg.EnumKey(uninstall, index)) as key:
                                name = str(winreg.QueryValueEx(key, "DisplayName")[0])
                                if "inoproshop" not in name.casefold():
                                    continue
                                for field in ("InstallLocation", "DisplayIcon"):
                                    try:
                                        found.append(str(winreg.QueryValueEx(key, field)[0]))
                                    except OSError:
                                        pass
                        except OSError:
                            continue
            except OSError:
                pass
    return found


def _desktop_locations() -> list[str]:
    """Read process paths and shortcuts only in known Windows menu folders."""
    if os.name != "nt":
        return []
    shell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if not shell:
        return []
    script = r"""$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$found = @()
Get-Process -Name InoProShop | ForEach-Object { if ($_.Path) { $found += $_.Path } }
$folders = @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('CommonDesktopDirectory'), [Environment]::GetFolderPath('StartMenu'), [Environment]::GetFolderPath('CommonStartMenu'))
$links = New-Object -ComObject WScript.Shell
foreach ($folder in $folders) {
    if ($folder -and (Test-Path -LiteralPath $folder)) {
        Get-ChildItem -LiteralPath $folder -Filter '*Ino*Shop*.lnk' -File -Recurse | ForEach-Object {
            $target = $links.CreateShortcut($_.FullName).TargetPath
            if ($target) { $found += $target }
        }
    }
}
ConvertTo-Json -InputObject @($found | Select-Object -Unique) -Compress
"""
    try:
        result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        data = json.loads(result.stdout.decode("utf-8-sig", errors="replace"))
        return [value for value in data if isinstance(value, str)] if isinstance(data, list) else []
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []


def _common_locations() -> list[str]:
    locations = []
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        directory = os.environ.get(variable)
        if not directory:
            continue
        base = Path(directory)
        locations.extend(str(base / name) for name in ("InoProShop", "Inoproshop", "Inovance/InoProShop"))
        try:
            locations.extend(str(path) for path in base.glob("*InoProShop*") if path.is_dir())
        except OSError:
            pass
    # These are direct, bounded probes, never scans of users' drives.
    if os.name == "nt":
        locations.extend(drive + r"\Inoproshop" for drive in (os.environ.get("SystemDrive", "C:"), "D:", "E:"))
    return locations


def _exe_candidates(location: str):
    location = os.path.expandvars(location.strip())
    # DisplayIcon values may be quoted and have a trailing icon index.
    match = re.match(r'^"([^"]+\.exe)"(?:,\s*-?\d+)?$', location, flags=re.I)
    if match:
        location = match.group(1)
    else:
        location = re.sub(r",\s*-?\d+$", "", location).strip('"')
    path = Path(location).expanduser()
    if path.name.casefold() == "inoproshop.exe":
        yield path
    else:
        for relative in ("InoProShop.exe", "Common/InoProShop.exe", "CODESYS/Common/InoProShop.exe"):
            yield path / relative


def _profiles(exe: Path) -> list[str]:
    directory = exe.parent.parent / "Profiles"
    try:
        return sorted({path.stem for path in directory.glob("*.profile") if path.is_file() and path.stem.casefold().startswith("inoproshop")})
    except OSError:
        return []


def discover_installations(config: dict | None = None, ino_exe=None) -> list[dict]:
    """Return executable/profile pairs, without installing or launching anything."""
    saved = (config or {}).get("ide", {})
    if ino_exe:
        locations = [str(ino_exe)]
    else:
        locations = [saved.get("exe"), os.environ.get("INOPROSHOP_EXE"), *_registry_locations(), *_desktop_locations(), *_common_locations()]
    found = {}
    for location in locations:
        if not isinstance(location, str) or not location.strip():
            continue
        for exe in _exe_candidates(location):
            if not exe.is_file():
                continue
            exe = exe.resolve()
            for profile in _profiles(exe):
                found[(_path_key(exe), profile.casefold())] = {"exe": str(exe), "profile": profile}
    return sorted(found.values(), key=lambda item: (item["exe"].casefold(), item["profile"].casefold()))


def _choose(items: list[dict], description: str, non_interactive: bool, progress) -> dict:
    if len(items) == 1:
        return items[0]
    if non_interactive:
        raise LauncherHostError("SELECTION_REQUIRED", description + "，请通过参数指定，或在交互终端重新运行。")
    progress(description + "：")
    for index, item in enumerate(items, 1):
        if "host_instance_id" in item:
            label = f"PID {item['pid']} / {item.get('project_path') or '尚未打开工程'}"
        else:
            label = f"{item['exe']} / {item['profile']}"
        progress(f"  {index}. {label}")
    while True:
        try:
            answer = input("请输入编号：").strip()
        except (EOFError, KeyboardInterrupt):
            raise LauncherHostError("SELECTION_REQUIRED", "未选择 InoProShop；请在交互终端重新运行。") from None
        if answer.isdecimal() and 1 <= int(answer) <= len(items):
            return items[int(answer) - 1]
        progress(f"请输入 1 至 {len(items)} 之间的编号。")


async def _call(session, name: str, arguments: dict, timeout: float = 15) -> dict:
    try:
        result = await asyncio.wait_for(session.call_tool(name, arguments), timeout=timeout)
    except TimeoutError:
        raise LauncherHostError("HOST_BUSY", "InoProShop 暂未响应。请检查该 IDE 中的启动提示或对话框，处理后重新连接。") from None
    except Exception:
        # Protocol exceptions may contain credentials; do not echo their text.
        raise LauncherHostError("MCP_REQUEST_FAILED", "无法通过 MCP 验证 InoProShop；请查看连接诊断。") from None
    if isinstance(result, dict):
        data = result.get("structuredContent", result)
        is_error = result.get("isError", False)
    else:
        data = getattr(result, "structuredContent", None)
        is_error = getattr(result, "isError", False)
        if not isinstance(data, dict):
            for item in getattr(result, "content", []):
                if getattr(item, "type", None) == "text":
                    try:
                        data = json.loads(item.text)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(data, dict):
                        break
    if not isinstance(data, dict):
        raise LauncherHostError("MCP_RESPONSE_INVALID", "MCP 返回了无法识别的宿主状态。")
    if is_error or "error" in data:
        code = str(data.get("error", {}).get("code", "MCP_REQUEST_FAILED"))
        if code in ("HOST_BUSY", "HOST_UNAVAILABLE", "HOST_BINDING_CHANGED", "HOST_RESTARTED"):
            raise LauncherHostError(code, "InoProShop 尚未就绪或工程正在切换；请检查 IDE 后重新连接。")
        raise LauncherHostError(code, "无法验证 InoProShop 桥接；请检查服务与 IDE 是否使用同一连接配置。")
    return data


def _expected_version(root: Path) -> dict:
    directory = root / "adapters/inoproshop/bridge"
    try:
        hashes = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in ("adapter.py", "entry.py")}
    except OSError:
        raise LauncherHostError("BRIDGE_FILES_MISSING", "发布包缺少 InoProShop 桥接文件，请重新解压完整发布包。") from None
    return {"bridge": "1.0", "protocol": 1, "sources": hashes}


def _host_installation(host: dict, saved: dict) -> dict:
    exe = _process_executable(int(host["pid"]))
    remembered = saved.get("last_host_instance_id") == host.get("host_instance_id")
    if not exe and remembered:
        exe = saved.get("exe")
    profile = saved.get("profile") if remembered else None
    if exe and not profile:
        profiles = _profiles(Path(exe))
        if len(profiles) == 1:
            profile = profiles[0]
    return {"exe": exe, "profile": profile}


async def _probe_host(session, host: dict, expected: dict, project_root: Path, timeout=15) -> dict:
    response = await _call(session, "ino_get_capabilities", {"host_id": host["host_id"], "host_instance_id": host["host_instance_id"]}, timeout)
    capabilities = response.get("host", {})
    if capabilities.get("version") != expected:
        raise LauncherHostError("HOST_VERSION_MISMATCH", "此 IDE 加载的桥接版本与当前发布包不一致。")
    if capabilities.get("test_owned"):
        raise LauncherHostError("TEST_HOST", "测试使用的 IDE 不可用于日常连接。")
    write_root = capabilities.get("write_project_root")
    if not write_root or _path_key(write_root) != _path_key(project_root):
        raise LauncherHostError("HOST_ROOT_MISMATCH", "此 IDE 使用了不同的工程副本目录。")
    health = capabilities.get("runtime_health", {})
    if not (health.get("script_entry_has_returned") is True and health.get("ide_enabled") is True and health.get("invoke_required") is False and health.get("modal_windows") == 0):
        raise LauncherHostError("HOST_BUSY", "InoProShop 界面尚未就绪；请处理 IDE 中的对话框后重新连接。")
    # Empty IDEs intentionally report unknown native evidence.  Readiness does
    # not require a project, a snapshot, compilation, or capability promotion.
    return health


def _start_ide(exe: str, profile: str, wrapper: Path):
    env = os.environ.copy()
    env["__COMPAT_LAYER"] = "RunAsInvoker"
    options = {}
    command = [exe, "--profile=" + profile, "--runscript=" + str(wrapper)]
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 1  # The user explicitly needs the normal visible IDE.
        options["startupinfo"] = startup
        # This OEM parses the original command line rather than normal argv.
        # Quoting the entire "--runscript=path with spaces" silently skips the
        # script. Keep each option prefix outside the quoted value. Pass this
        # directly to CreateProcess (never through a shell).
        command = (subprocess.list2cmdline([exe])
                   + " --profile=" + subprocess.list2cmdline([profile])
                   + " --runscript=" + subprocess.list2cmdline([str(wrapper)]))
        options["executable"] = exe
    return subprocess.Popen(command, cwd=str(Path(exe).parent), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)


def _prepare_wrapper(root: Path, state: Path, project_root: Path) -> Path:
    run = state / "launcher" / "hosts" / uuid.uuid4().hex
    run.mkdir(parents=True)
    config_path = run / "bridge.json"
    _atomic_json(config_path, {"state_dir": str(state), "project_root": str(project_root), "native_evidence": str(state / "native_evidence.json"), "test_owned": False, "queue_timeout": 120})
    entry = root / "adapters/inoproshop/bridge/entry.py"
    error_path = run / "startup-error.json"
    wrapper = run / "run_host.py"
    wrapper.write_text(
        "# encoding: utf-8\nfrom __future__ import unicode_literals\n"
        "import io, json, time, traceback\n"
        "system.delay(1500)\n"
        f"INOPROSHOP_MCP_CONFIG = {str(config_path)!r}\n"
        f"__file__ = {str(entry)!r}\n"
        "try:\n    execfile(__file__, globals())\n"
        "except Exception:\n"
        f"    with io.open({str(error_path)!r}, 'w', encoding='utf-8') as failure:\n"
        "        failure.write(json.dumps({'at': time.time(), 'traceback': traceback.format_exc()}, ensure_ascii=False))\n"
        "    raise\n", encoding="utf-8")
    return wrapper


def _save_selection(config: dict, selected: dict) -> None:
    saved = config.setdefault("ide", {})
    saved.update(last_host_instance_id=selected["host_instance_id"], last_pid=selected["pid"])
    for key in ("exe", "profile"):
        if selected.get(key):
            saved[key] = selected[key]
        else:
            # A manually registered host may hide its process path.  Do not
            # attribute the previously selected installation to this new host.
            saved.pop(key, None)
    saved.pop("pending_start", None)


async def ensure_host(root, state, project_root, session, config, ino_exe=None, profile=None, check_only=False, non_interactive=False, timeout=180, progress=print) -> dict:
    """Reuse one healthy production bridge or launch a normal, empty IDE.

    ``session`` is an initialized official MCP ClientSession.  The caller owns
    its lifecycle and persists ``config``.  No project is opened or selected.
    On startup failure, a pending process record is retained and the IDE is
    never killed.  ``check_only`` makes no filesystem or config changes.
    """
    root, state, project_root = (Path(value).resolve() for value in (root, state, project_root))
    expected = _expected_version(root)
    saved = config.get("ide", {})
    profile = profile.removesuffix(".profile") if profile else None
    explicit_installations = None
    if ino_exe:
        ino_exe = str(Path(ino_exe).expanduser().resolve())
        if Path(ino_exe).name.casefold() != "inoproshop.exe" or not Path(ino_exe).is_file():
            raise LauncherHostError("IDE_NOT_FOUND", "-InoExe 必须指向已安装的 InoProShop.exe 文件。")
        explicit_installations = discover_installations(config, ino_exe)
        if profile:
            explicit_installations = [item for item in explicit_installations if item["profile"].casefold() == profile.casefold()]
        if not explicit_installations:
            raise LauncherHostError("IDE_NOT_FOUND", "指定路径没有可用的 InoProShop.exe 和对应 InoProShop 配置文件。")
    registrations = (await _call(session, "ino_list_hosts", {})).get("hosts", [])
    healthy, blocked = [], []
    for host in registrations:
        if not host.get("available") or host.get("test_owned") or host.get("version") != expected:
            continue
        registration_path = host.get("registration_path")
        if registration_path and _path_key(Path(registration_path).parent) != _path_key(state / "hosts"):
            continue
        if not all(host.get(key) for key in ("host_id", "host_instance_id", "pid")):
            continue
        installation = _host_installation(host, saved)
        if ino_exe and (not installation["exe"] or _path_key(installation["exe"]) != _path_key(ino_exe)):
            continue
        if profile and (not installation["profile"] or installation["profile"].casefold() != profile.casefold()):
            continue
        progress(f"正在检查 InoProShop（PID {host['pid']}）…")
        try:
            health = await _probe_host(session, host, expected, project_root)
        except LauncherHostError as exc:
            if exc.code not in ("HOST_VERSION_MISMATCH", "HOST_ROOT_MISMATCH", "TEST_HOST"):
                blocked.append((host, exc))
            continue
        healthy.append(dict(host, **installation, reused=True, runtime_health=health))
    if healthy:
        selected = next((item for item in healthy if item["host_instance_id"] == saved.get("last_host_instance_id")), None)
        selected = selected or _choose(healthy, "发现多个可用的 InoProShop，请选择本次使用的 IDE", non_interactive, progress)
        if not check_only:
            _save_selection(config, selected)
        progress("InoProShop 已连接。" + ("请在这个 IDE 中照常打开工程。" if not selected.get("project_path") else ""))
        return selected
    if blocked:
        host, error = next(((host, error) for host, error in blocked if host["host_instance_id"] == saved.get("last_host_instance_id")), blocked[0])
        raise LauncherHostError(error.code, f"已找到 InoProShop（PID {host['pid']}），但暂时无法操作。请处理该 IDE 的提示后重新连接；不会重复启动 IDE。", {"pid": host["pid"]})
    if check_only:
        raise LauncherHostError("HOST_NOT_RUNNING", "尚无匹配的 InoProShop 桥接连接。请运行连接入口启动 IDE。")

    record_path = state / "launcher" / "ide-process.json"
    pending = saved.get("pending_start") or _read_json(record_path)
    if pending.get("pid") and _process_alive(pending["pid"]):
        actual = _process_executable(pending["pid"])
        if not actual or _path_key(actual) == _path_key(pending.get("exe", "")):
            raise LauncherHostError("HOST_START_PENDING", f"先前启动的 InoProShop（PID {pending['pid']}）仍在运行但桥接未就绪，请检查该 IDE 的启动提示；不会重复启动。", {"pid": pending["pid"]})
    installations = explicit_installations if explicit_installations is not None else discover_installations(config)
    if profile:
        installations = [item for item in installations if item["profile"].casefold() == profile.casefold()]
    if not installations:
        raise LauncherHostError("IDE_NOT_FOUND", "未找到 InoProShop。请通过 -InoExe 指定 InoProShop.exe，必要时同时指定 -Profile。")
    preferred = next((item for item in installations if saved.get("exe") and _path_key(item["exe"]) == _path_key(saved["exe"]) and item["profile"] == saved.get("profile")), None)
    installation = preferred or _choose(installations, "发现多个 InoProShop 安装配置，请选择", non_interactive, progress)
    wrapper = _prepare_wrapper(root, state, project_root)
    progress("正在启动 InoProShop，桥接会自动加载；启动提示请在 IDE 中处理…")
    try:
        process = _start_ide(installation["exe"], installation["profile"], wrapper)
    except OSError:
        raise LauncherHostError("IDE_START_FAILED", "无法启动 InoProShop，请检查安装路径与当前用户的运行权限。") from None
    pending = dict(installation, pid=process.pid, run_dir=str(wrapper.parent), started_at=time.time())
    config.setdefault("ide", {})["pending_start"] = pending
    _atomic_json(record_path, pending)
    _atomic_json(wrapper.parent / "launch.json", pending)
    deadline, next_progress = time.monotonic() + timeout, time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise LauncherHostError("IDE_EXITED", "InoProShop 在桥接连接完成前已退出，请检查安装组件和启动日志。", {"run_dir": str(wrapper.parent)})
        hosts = (await _call(session, "ino_list_hosts", {}, min(15, max(0.1, deadline - time.monotonic())))).get("hosts", [])
        host = next((item for item in hosts if item.get("pid") == process.pid and item.get("available") and not item.get("test_owned")), None)
        if host:
            try:
                health = await _probe_host(session, host, expected, project_root, min(15, max(0.1, deadline - time.monotonic())))
            except LauncherHostError as exc:
                if exc.code not in ("HOST_BUSY", "HOST_BINDING_CHANGED", "HOST_RESTARTED", "HOST_UNAVAILABLE"):
                    raise
            else:
                selected = dict(host, **installation, reused=False, runtime_health=health)
                _save_selection(config, selected)
                _atomic_json(record_path, dict(pending, host_instance_id=host["host_instance_id"], status="ready"))
                progress("InoProShop 已连接。请在这个 IDE 中照常打开工程。")
                return selected
        if time.monotonic() >= next_progress:
            progress("仍在等待 InoProShop 桥接，请查看 IDE 是否有待处理的启动提示…")
            next_progress = time.monotonic() + 10
        await asyncio.sleep(0.5)
    raise LauncherHostError("IDE_START_TIMEOUT", "等待 InoProShop 桥接超时。IDE 保持打开，请处理其启动提示后重新连接；不会强制关闭或重复启动。", {"pid": process.pid, "run_dir": str(wrapper.parent)})
