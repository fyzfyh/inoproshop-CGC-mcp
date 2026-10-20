"""Prepare a local desktop session; never edit a PLC project or invoke a model.

The PowerShell bootstrap owns dependency installation and the interactive OMP
process. This module only prepares/probes the database, controller and IDE.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager, contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

from .common import atomic_json, utc
from .core import TOOLS


class LauncherError(RuntimeError):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def read_json(path, default=None):
    if not Path(path).exists():
        return {} if default is None else default
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError):
        raise LauncherError("INVALID_LOCAL_CONFIG", "本地配置无法读取；请保留文件并检查诊断，勿删除数据库或凭据。") from None


@contextmanager
def startup_lock(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stream = (directory / "start.lock").open("a+b")
    try:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            if not stream.read(1):
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise LauncherError("START_IN_PROGRESS", "另一个启动入口正在准备同一环境，请等待它完成后重试。") from None
    try:
        yield
    finally:
        stream.close()


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except ConnectionRefusedError:
        return False
    except OSError as exc:
        # An access-denied sandbox is not evidence that the port is free.
        if getattr(exc, "winerror", None) == 10013 or exc.errno in (1, 13):
            raise LauncherError("LOCAL_NETWORK_DENIED", "当前运行环境禁止本机连接，请在正常桌面终端运行入口。") from None
        return False


def read_token(state):
    try:
        token = (Path(state) / "token").read_text(encoding="ascii").strip()
        if len(token) < 32:
            raise ValueError()
        return token
    except (OSError, ValueError):
        raise LauncherError("MCP_TOKEN_UNAVAILABLE", "MCP 本机凭据缺失或无效；保留现有状态，检查控制器是否启动。") from None


@asynccontextmanager
async def mcp_session(state, url, timeout=10):
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from datetime import timedelta

    token = read_token(state)
    async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token},
                                 timeout=timeout, trust_env=False) as client:
        async with streamable_http_client(url, http_client=client) as (reader, writer, _):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=timeout)) as session:
                await session.initialize()
                yield session


async def probe_controller(state, url):
    async with mcp_session(state, url, timeout=5) as session:
        tools = await session.list_tools()
        if {item.name for item in tools.tools} != set(TOOLS):
            raise LauncherError("MCP_CONTRACT_MISMATCH", "该端口的服务工具与当前版本不匹配，不能作为本次连接使用。")
    return {"status": "ready", "url": url, "tool_count": len(TOOLS)}


def check_controller_identity(state, project_root, port, database=None):
    from .__main__ import database_fingerprint
    from .launcher_database import process_matches
    import hmac

    identity = read_json(Path(state) / "service-runtime.json")
    if not identity:
        raise LauncherError("MCP_LEGACY_RESTART_REQUIRED", "当前手动启动的旧版 MCP 缺少连接身份记录。请在原服务终端正常退出 MCP，再运行一键入口；无需关闭 IDE 或数据库，启动器不会强制停止进程。")
    expected = {"state_dir": str(Path(state).resolve()),
                "project_root": str(Path(project_root).resolve())}
    for key, value in expected.items():
        if os.path.normcase(identity.get(key, "")) != os.path.normcase(value):
            raise LauncherError("MCP_CONFIG_MISMATCH", "正在运行的 MCP 使用了不同状态目录或工程副本目录，请使用对应入口。")
    if identity.get("port") != port:
        raise LauncherError("MCP_CONFIG_MISMATCH", "正在运行的 MCP 端口与当前配置不同。")
    process = identity.get("process_identity")
    fingerprint = identity.get("database_fingerprint")
    if not isinstance(process, dict) or not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise LauncherError("MCP_LEGACY_RESTART_REQUIRED", "当前旧版 MCP 缺少进程或数据库身份记录。请在原服务终端正常退出 MCP，再运行一键入口；无需关闭 IDE 或数据库，启动器不会强制停止进程。")
    if process.get("pid") != identity.get("pid") or not process_matches(process):
        raise LauncherError("MCP_PROCESS_IDENTITY_MISMATCH", "MCP 运行记录与现存进程不一致。请检查对应服务终端后正常退出旧服务，再运行一键入口；不会强制停止进程。")
    if database and database.get("status") == "ready":
        environment = {"NEO4J_URI": database["uri"], "NEO4J_USERNAME": database["username"],
                       "NEO4J_PASSWORD": database["password"], "NEO4J_DATABASE": os.environ.get("NEO4J_DATABASE")}
        if not hmac.compare_digest(fingerprint, database_fingerprint(read_token(state), environment)):
            raise LauncherError("MCP_DATABASE_CONFIG_MISMATCH", "现有 MCP 使用了不同的数据库连接配置。请在原服务终端正常退出 MCP，再运行一键入口；数据库与工程会保留。")
    return identity


async def ensure_controller(root, state, project_root, database, *, check_only=False,
                            port=8765, timeout=60, progress=print):
    root, state, project_root = map(Path, (root, state, project_root))
    url = f"http://127.0.0.1:{port}/mcp"
    if port_open(port):
        try:
            result = await asyncio.wait_for(probe_controller(state, url), 12)
            check_controller_identity(state, project_root, port, database)
            return dict(result, reused=True)
        except LauncherError:
            raise
        except Exception:
            raise LauncherError("MCP_PORT_CONFLICT_OR_AUTH", f"端口 {port} 已被占用，但无法通过本环境的 MCP 认证；不会停止该进程。") from None
    if check_only:
        return {"status": "missing", "code": "MCP_NOT_RUNNING", "message": "MCP 未运行，请运行 Connect-Ino.cmd。"}
    service = root / "services/inoproshop-mcp"
    python = (root / "runtime/python/python.exe" if (root / "offline").is_dir()
              else service / ".venv/Scripts/python.exe")
    if not python.is_file():
        raise LauncherError("PYTHON_MISSING", "服务依赖尚未准备好，请从 Connect-Ino.cmd 启动。")
    logs = state / "launcher/logs"
    logs.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(NEO4J_URI=database["uri"], NEO4J_USERNAME=database["username"],
               NEO4J_PASSWORD=database["password"], PYTHONUTF8="1")
    env["PYTHONPATH"] = str(service / "src")
    command = [str(python), "-m", "inoproshop_mcp", "serve", "--state-dir", str(state),
               "--project-root", str(project_root), "--port", str(port)]
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    with (logs / "controller.log").open("ab") as log:
        process = subprocess.Popen(command, cwd=str(service), env=env,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   creationflags=flags)
    atomic_json(state / "launcher/controller-process.json", {
        "pid": process.pid, "started_at": utc(), "executable": str(python),
        "state_dir": str(state), "project_root": str(project_root), "port": port,
    })
    progress("正在等待 MCP 完成认证与工具检查……")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise LauncherError("MCP_EXITED", "MCP 启动进程已退出，请检查 launcher/logs/controller.log；已有状态已保留。")
        if (state / "token").exists() and port_open(port):
            try:
                result = await asyncio.wait_for(probe_controller(state, url), 8)
                check_controller_identity(state, project_root, port, database)
                return dict(result, reused=False, pid=process.pid)
            except LauncherError:
                raise
            except Exception:
                pass
        await asyncio.sleep(0.5)
    raise LauncherError("MCP_START_TIMEOUT", "MCP 启动超时；已保留进程和日志，检查后重新运行入口即可。")


def safe_failure(exc):
    # Never serialize raw transport/driver exceptions: they may include secrets.
    if isinstance(exc, BaseExceptionGroup):
        pending = list(exc.exceptions)
        while pending:
            child = pending.pop(0)
            if isinstance(child, BaseExceptionGroup):
                pending.extend(child.exceptions)
            elif hasattr(child, "code") and hasattr(child, "message"):
                return safe_failure(child)
    code = getattr(exc, "code", "STARTUP_FAILED")
    if hasattr(exc, "code") and hasattr(exc, "message"):
        message = str(exc.message)
    else:
        message = "此阶段未完成，请查看诊断状态；已有数据和进程已保留。"
    return {"status": "blocked", "code": code, "message": message,
            "error_type": type(exc).__name__}


async def prepare(args, config, progress=print):
    from .launcher_database import ensure_database
    from .launcher_host import ensure_host

    root, state, projects = args.root, args.state_dir, args.project_root
    report = {"schema_version": 1, "at": utc(), "check_only": args.check,
              "model_invoked": False, "stages": {}}
    progress("[1/3] 检查本机数据库……")
    try:
        database = await asyncio.to_thread(ensure_database, root, root / "work/mcp_p0/neo4j",
                                           check_only=args.check, progress=progress)
        # Whitelist, rather than trying to redact an arbitrary credentials dict.
        report["stages"]["database"] = {key: database[key] for key in
            ("status", "code", "message", "reused", "managed", "pid") if key in database}
        if database.get("status") != "ready" and not args.check:
            raise LauncherError(database.get("code", "DATABASE_UNAVAILABLE"), database.get("message", "数据库未就绪。"))
    except Exception as exc:
        report["stages"]["database"] = safe_failure(exc)
        if not args.check:
            return report, None
        database = {}
    progress("[2/3] 检查 MCP 认证与工具……")
    try:
        controller = await ensure_controller(root, state, projects, database, check_only=args.check, port=args.port, progress=progress)
        report["stages"]["mcp"] = controller
        if controller.get("status") != "ready":
            report["stages"]["ide"] = {"status": "blocked", "code": "MCP_REQUIRED", "message": "需先启动 MCP 才能检查 IDE 桥接。"}
            return report, None
    except Exception as exc:
        report["stages"]["mcp"] = safe_failure(exc)
        report["stages"]["ide"] = {"status": "blocked", "code": "MCP_REQUIRED", "message": "需先恢复 MCP 连接。"}
        return report, None
    progress("[3/3] 检查 InoProShop 桥接……")
    try:
        async with mcp_session(state, controller["url"], timeout=190) as session:
            host = await ensure_host(root, state, projects, session, config,
                ino_exe=args.ino_exe, profile=args.profile, check_only=args.check,
                non_interactive=args.non_interactive, progress=progress)
        if host.get("status", "ready") != "ready":
            report["stages"]["ide"] = host
            return report, None
        report["stages"]["ide"] = {"status": "ready", **{key: host[key] for key in
            ("host_id", "host_instance_id", "pid", "reused", "project_path", "runtime_health") if key in host}}
        report["stages"]["project"] = {"status": "open" if host.get("project_path") else "not_open",
            "message": "工程已打开；可在 ohmypi 中请求读取。" if host.get("project_path") else "连接已就绪，请在 IDE 打开工程。"}
        context = {"schema_version": 1, "at": utc(), "state_dir": str(state),
            "project_root": str(projects), "workspace": str(args.workspace), "url": controller["url"],
            "host_id": host["host_id"], "host_instance_id": host["host_instance_id"],
            "pid": host.get("pid"), "exe": host.get("exe"), "profile": host.get("profile")}
        return report, context
    except Exception as exc:
        report["stages"]["ide"] = safe_failure(exc)
        return report, None


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="InoProShop 一键连接准备与诊断")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[4])
    for name in ("state-dir", "project-root", "workspace", "ino-exe", "context-out"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--profile")
    parser.add_argument("--port", type=int)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--non-interactive", action="store_true")
    args = parser.parse_args(argv)
    args.root = args.root.resolve()
    args.state_dir = (args.state_dir or Path(os.environ.get("INO_MCP_STATE", args.root / "work/mcp_p0/state"))).resolve()
    config = read_json(args.state_dir / "launcher/config.json")
    args.project_root = (args.project_root or Path(os.environ.get("INO_MCP_PROJECTS") or config.get("project_root") or args.root / "work/mcp_p0/projects")).resolve()
    args.workspace = (args.workspace or Path(config.get("workspace") or args.root / "work/mcp_p0/omp")).resolve()
    args.port = args.port if args.port is not None else int(config.get("port", 8765))
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")
    args.context_out = (args.context_out or args.state_dir / "launcher-context.json").resolve()
    return args, config


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    try:
        args, config = parse_args(argv)
        runtime = args.state_dir / "launcher"
        if args.check:
            report, context = asyncio.run(prepare(args, config))
        else:
            with startup_lock(runtime):
                try:
                    report, context = asyncio.run(prepare(args, config))
                finally:
                    # Save the owned pending IDE identity even if registration timed out.
                    config.update(schema_version=1, workspace=str(args.workspace),
                                  project_root=str(args.project_root), port=args.port)
                    atomic_json(runtime / "config.json", config)
                if context:
                    atomic_json(args.context_out, context)
                atomic_json(runtime / "last-run.json", report)
        success = all(report["stages"].get(name, {}).get("status") == "ready" for name in ("database", "mcp", "ide"))
        for name, stage in report["stages"].items():
            print(f"{name}: {stage['status']}" + (" — " + stage["message"] if stage.get("message") else ""))
        if not args.check:
            print("诊断记录：" + str(runtime / "last-run.json"))
        if success:
            print("连接准备完成。退出 ohmypi 后，IDE 与本机服务继续保留。")
        return 0 if success else 1
    except KeyboardInterrupt:
        print("启动已取消；已有组件与工程保持打开。")
        return 130
    except Exception as exc:
        failure = safe_failure(exc)
        print(f"{failure['code']}: {failure['message']}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
