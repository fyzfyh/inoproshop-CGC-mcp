"""Provision and reuse the launcher's private, authenticated Neo4j instance.

Returned connection dictionaries contain a password for the controller's child
environment. Never serialize them into diagnostics or launcher process records.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from urllib.parse import urlparse
from urllib.request import urlopen
import zipfile

from .common import atomic_json

LOCAL_URI = "bolt://127.0.0.1:17687"
CONFIG = """server.default_listen_address=127.0.0.1
server.bolt.enabled=true
server.bolt.listen_address=127.0.0.1:17687
server.bolt.advertised_address=127.0.0.1:17687
server.http.enabled=false
server.https.enabled=false
server.memory.heap.initial_size=256m
server.memory.heap.max_size=512m
server.memory.pagecache.size=128m
dbms.security.auth_enabled=true
dbms.usage_report.enabled=false
"""


class DatabaseLauncherError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _fail(code, message):
    raise DatabaseLauncherError(code, message)


def _read_json(path: Path):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError):
        _fail("DATABASE_CONFIGURATION_INVALID", "数据库配置文件无法读取；请检查文件格式和访问权限。")


def restrict_access(path: Path):
    """Restrict a file/directory before writing any secret into it."""
    if os.name != "nt":
        path.chmod(0o700 if path.is_dir() else 0o600)
        return
    import win32api
    import win32con
    import win32security
    try:
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        finally:
            token.Close()
        acl = win32security.ACL()
        flags = 3 if path.is_dir() else 0  # OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE
        for principal in (sid, win32security.CreateWellKnownSid(win32security.WinLocalSystemSid)):
            acl.AddAccessAllowedAceEx(win32security.ACL_REVISION, flags, win32con.GENERIC_ALL, principal)
        win32security.SetNamedSecurityInfo(str(path), win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, acl, None)
    except Exception:
        _fail("DATABASE_CREDENTIALS_PERMISSIONS", "无法限制数据库凭据访问权限；未继续启动。")


def _write_secret(path: Path, text: str):
    fd, temporary = tempfile.mkstemp(prefix=".credential-", dir=path.parent)
    os.close(fd)
    pending = Path(temporary)
    try:
        restrict_access(pending)
        pending.write_text(text, encoding="utf-8")
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


def _credentials(runtime_dir: Path):
    path = runtime_dir / "credentials.json"
    if path.exists():
        value = _read_json(path)
        external = False
    elif any(os.environ.get(key) for key in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD")):
        value = {"uri": os.environ.get("NEO4J_URI"),
                 "username": os.environ.get("NEO4J_USERNAME", "neo4j"),
                 "password": os.environ.get("NEO4J_PASSWORD")}
        external = True
    else:
        return None, False
    if not all(isinstance(value.get(key), str) and value[key] for key in ("uri", "username", "password")):
        _fail("DATABASE_CREDENTIALS_INVALID", "数据库连接配置不完整；请恢复已有凭据，密码不会自动重置。")
    try:
        parsed = urlparse(value["uri"])
        if parsed.scheme not in {"bolt", "bolt+s", "bolt+ssc", "neo4j", "neo4j+s", "neo4j+ssc"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError()
        parsed.port
    except ValueError:
        _fail("DATABASE_CREDENTIALS_INVALID", "数据库连接地址格式无效。")
    return {key: value[key] for key in ("uri", "username", "password")}, external


def _port_open(uri: str) -> bool:
    parsed = urlparse(uri)
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 7687), timeout=1):
            return True
    except OSError as exc:
        if getattr(exc, "winerror", None) == 10013 or exc.errno in {1, 13, 10013}:
            _fail("DATABASE_NETWORK_DENIED", "当前环境拒绝本机数据库网络访问；不能据此判断数据库已停止。")
        return False


def _probe(credentials: dict, version: str, timeout=4):
    """Read-only authentication and version checks; does not create CGC constraints."""
    try:
        if not _port_open(credentials["uri"]):
            return {"status": "unavailable", "code": "DATABASE_UNAVAILABLE"}
    except DatabaseLauncherError as exc:
        return {"status": "blocked", "code": exc.code}
    from neo4j import GraphDatabase, Query
    from neo4j.exceptions import AuthError, ClientError
    try:
        with GraphDatabase.driver(credentials["uri"], auth=(credentials["username"], credentials["password"]),
                connection_timeout=timeout, connection_acquisition_timeout=timeout,
                max_transaction_retry_time=0) as driver:
            with driver.session(database=os.environ.get("NEO4J_DATABASE")) as session:
                record = session.run(Query("RETURN 1 AS ready", timeout=timeout)).single()
                if record is None or record["ready"] != 1:
                    return {"status": "unavailable", "code": "DATABASE_QUERY_FAILED"}
                component = session.run(Query("CALL dbms.components() YIELD versions RETURN versions", timeout=timeout)).single()
                actual = component["versions"][0] if component else None
                if actual != version:
                    return {"status": "blocked", "code": "DATABASE_VERSION_MISMATCH"}
        return {"status": "ready", "version": actual}
    except AuthError:
        return {"status": "blocked", "code": "DATABASE_AUTH_FAILED"}
    except ClientError:
        return {"status": "blocked", "code": "DATABASE_ACCESS_FAILED"}
    except Exception:
        # Driver exception text may include credentials, URI or database contents.
        return {"status": "unavailable", "code": "DATABASE_UNAVAILABLE"}


def _hash(path: Path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _archive(url: str, expected_hash: str, cache: Path, candidates=(), *, offline=False):
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash) or not url.startswith("https://"):
        _fail("DATABASE_RUNTIME_LOCK_INVALID", "运行时下载锁必须包含 HTTPS 地址和 SHA-256。")
    destination = cache / (expected_hash.lower() + ".zip")
    for candidate in (destination, *candidates):
        candidate = Path(candidate)
        if candidate.is_file() and _hash(candidate).lower() == expected_hash.lower():
            return candidate
    if offline:
        _fail("OFFLINE_ARCHIVE_MISSING", "离线运行文件缺失或校验失败，请完整重新解压发布包；不会联网下载。")
    cache.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="download-", suffix=".part", dir=cache)
    os.close(fd)
    pending = Path(name)
    try:
        with urlopen(url, timeout=60) as response, pending.open("wb") as target:
            if not response.geturl().startswith("https://"):
                _fail("DATABASE_DOWNLOAD_FAILED", "运行时下载被重定向到非 HTTPS 地址。")
            shutil.copyfileobj(response, target)
        if _hash(pending).lower() != expected_hash.lower():
            _fail("DATABASE_ARCHIVE_HASH_MISMATCH", "运行时下载不完整或校验失败；未安装该文件。")
        os.replace(pending, destination)
        return destination
    except DatabaseLauncherError:
        raise
    except Exception:
        _fail("DATABASE_DOWNLOAD_FAILED", "运行时下载失败；请检查网络后重试，现有数据未被修改。")
    finally:
        pending.unlink(missing_ok=True)


def _extract(archive: Path, destination: Path, archive_root: str):
    if destination.exists():
        _fail("DATABASE_RUNTIME_INCOMPLETE", "运行时目录已存在但不完整；请保留数据并检查安装目录。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="extract-", dir=destination.parent) as temporary:
        staging = Path(temporary)
        try:
            with zipfile.ZipFile(archive) as package:
                if sum(item.file_size for item in package.infolist()) > 2_000_000_000:
                    raise ValueError()
                for item in package.infolist():
                    name = item.filename.replace("\\", "/")
                    target = (staging / name).resolve()
                    if not target.is_relative_to(staging.resolve()) or ":" in name or (item.external_attr >> 16) & 0o170000 == 0o120000:
                        raise ValueError()
                package.extractall(staging)
            source = (staging / archive_root).resolve()
            if not source.is_relative_to(staging.resolve()) or not source.is_dir():
                raise ValueError()
            os.replace(source, destination)
        except (OSError, ValueError, zipfile.BadZipFile):
            _fail("DATABASE_ARCHIVE_INVALID", "运行时压缩包结构不符合固定版本要求。")


def _hidden_flags():
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _java_version(home: Path):
    executable = home / "bin" / ("java.exe" if os.name == "nt" else "java")
    if not executable.is_file():
        return None
    try:
        result = subprocess.run([str(executable), "-version"], capture_output=True,
                                text=True, timeout=15, creationflags=_hidden_flags())
        match = re.search(r'version "([^"\r\n]+)"', result.stderr + result.stdout)
        return match.group(1) if result.returncode == 0 and match else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _find_java(runtime_dir: Path):
    candidates = []
    local_lock = runtime_dir / "runtime-lock.json"
    if local_lock.exists():
        old = _read_json(local_lock)
        if old.get("java_home"):
            candidates.append(Path(old["java_home"]))
    if os.environ.get("JAVA_HOME"):
        candidates.append(Path(os.environ["JAVA_HOME"]))
    on_path = shutil.which("java")
    if on_path:
        candidates.append(Path(on_path).parent.parent)
    candidates.extend(path.parent.parent for path in (runtime_dir / "java").glob("*/bin/java.exe"))
    for candidate in dict.fromkeys(candidates):
        version = _java_version(candidate)
        if version and version.split(".", 1)[0] == "21":
            return candidate.resolve(), version
    return None, None


def _ensure_java(root: Path, runtime_dir: Path, progress):
    offline = (root / "offline").is_dir()
    if offline:
        # Offline distributions use their pinned JRE, independent of system Java.
        lock = _read_json(root / "services/inoproshop-mcp/launcher-runtime-lock.json").get("java", {})
        home = runtime_dir / "java" / lock.get("archive_root", "missing")
        version = _java_version(home)
        if version and version == lock.get("version", "").split("+", 1)[0]:
            return home, version
    else:
        home, version = _find_java(runtime_dir)
        if home:
            return home, version
    lock = _read_json(root / "services/inoproshop-mcp/launcher-runtime-lock.json").get("java", {})
    if not all(lock.get(key) for key in ("archive_url", "archive_sha256", "archive_root")):
        _fail("DATABASE_RUNTIME_LOCK_INVALID", "Java 21 下载锁缺失或不完整。")
    progress("正在准备数据库所需的 Java 21…")
    package = _archive(lock["archive_url"], lock["archive_sha256"], runtime_dir / "downloads",
                       candidates=[root / "offline/archives/java.zip"], offline=offline)
    home = runtime_dir / "java" / lock["archive_root"]
    _extract(package, home, lock["archive_root"])
    version = _java_version(home)
    if not version or version.split(".", 1)[0] != "21":
        _fail("DATABASE_JAVA_VERSION", "下载的 Java 运行时未通过 Java 21 检查。")
    return home, version


def _has_data(home: Path):
    databases = home / "data/databases"
    return databases.exists() and any(databases.iterdir())


def _validate_configuration(home: Path):
    try:
        text = (home / "conf/neo4j.conf").read_text(encoding="utf-8-sig")
        config = dict(line.split("=", 1) for line in text.splitlines()
                      if "=" in line and not line.lstrip().startswith("#"))
        config = {key.strip(): value.strip() for key, value in config.items()}
    except (OSError, ValueError):
        _fail("DATABASE_CONFIGURATION_INVALID", "无法读取专用数据库配置。")
    expected = {"server.default_listen_address": "127.0.0.1", "server.bolt.enabled": "true",
                "server.bolt.listen_address": "127.0.0.1:17687", "server.http.enabled": "false",
                "server.https.enabled": "false", "dbms.security.auth_enabled": "true"}
    if any(config.get(key) != value for key, value in expected.items()) or any(
            key.startswith("server.directories.") for key in config):
        _fail("DATABASE_CONFIGURATION_UNMANAGED", "专用数据库配置已改变；启动器不会覆盖现有配置或访问其他数据目录。")


def _command(home: Path, java_home: Path, admin=False):
    # Same invocation used by Neo4j's bundled Invoke-Neo4jUtility.ps1, without
    # cmd/PowerShell argument re-parsing. A password never appears in this list.
    # Preserve @file in the bootloader's original args; otherwise the Java
    # launcher could expand the password before Neo4j forks its admin worker.
    # Neo4j 5.26's PATH parser escapeJava() corrupts non-ASCII paths supplied
    # through basedir/NEO4J_HOME/NEO4J_CONF. Its native fallback resolves the
    # parent of cwd without that string parser; both callers run from home/bin.
    return [str(java_home / "bin/java.exe"), "--disable-@files", "-cp", str(home / "lib/*"),
            "org.neo4j.server.startup." + ("Neo4jAdminCommand" if admin else "Neo4jCommand")]


def _environment(home: Path, java_home: Path):
    env = dict(os.environ)
    for key in ("JAVA_OPTS", "JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS",
                "HEAP_SIZE", "NEO4J_DEBUG", "NEO4J_PASSWORD", "INOAI_MCP_AUTHORIZATION",
                "NEO4J_HOME", "NEO4J_CONF"):
        env.pop(key, None)
    env.update(JAVA_HOME=str(java_home))
    return env


def _initialize_password(home: Path, java_home: Path, runtime_dir: Path, password: str):
    # Neo4j admin supports Picocli @files. Keep secrets out of child argv and logs.
    fd, name = tempfile.mkstemp(prefix=".initial-password-", dir=runtime_dir)
    os.close(fd)
    arguments = Path(name)
    try:
        restrict_access(arguments)
        arguments.write_text(json.dumps(password) + "\n", encoding="utf-8")
        result = subprocess.run(_command(home, java_home, admin=True) + ["dbms", "set-initial-password", "@" + str(arguments)],
            env=_environment(home, java_home), cwd=home / "bin", capture_output=True, timeout=60, creationflags=_hidden_flags())
        if result.returncode:
            _fail("DATABASE_PASSWORD_INITIALIZATION", "数据库首次密码初始化失败；已保留凭据供再次尝试。")
    except (OSError, subprocess.TimeoutExpired):
        _fail("DATABASE_PASSWORD_INITIALIZATION", "数据库首次密码初始化失败；请检查 Java 运行环境。")
    finally:
        arguments.unlink(missing_ok=True)


def process_identity(pid: int):
    """Return live Windows process identity, not merely a reusable numeric PID."""
    if os.name != "nt":
        return None
    import win32api
    import win32process
    import ctypes
    from ctypes import wintypes
    try:
        handle = win32api.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
        try:
            if win32process.GetExitCodeProcess(handle) != 259:
                return None
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            query = ctypes.windll.kernel32.QueryFullProcessImageNameW
            query.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
            if not query(int(handle), 0, buffer, ctypes.byref(size)):
                return None
            return {"pid": int(pid), "executable": buffer.value,
                    "created": win32process.GetProcessTimes(handle)["CreationTime"].isoformat()}
        finally:
            handle.Close()
    except Exception:
        return None


def process_matches(record: dict):
    actual = process_identity(record.get("pid", 0))
    return bool(actual and actual.get("created") == record.get("created") and
                os.path.normcase(actual["executable"]) == os.path.normcase(record.get("executable", "")))


def _start(home: Path, java_home: Path, runtime_dir: Path):
    with (runtime_dir / "launcher-stdout.log").open("ab") as stdout, (runtime_dir / "launcher-stderr.log").open("ab") as stderr:
        child = subprocess.Popen(_command(home, java_home) + ["console"], cwd=home / "bin",
            env=_environment(home, java_home), stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
            creationflags=_hidden_flags(), close_fds=True)
    record = process_identity(child.pid)
    if not record:
        # The Popen handle still refers to this exact child; safe even if PID reused.
        if child.poll() is None:
            child.terminate()
        _fail("DATABASE_PROCESS_IDENTITY", "无法确认新数据库进程身份；未留下未识别的后台启动进程。")
    record.update(neo4j_home=str(home), java_home=str(java_home))
    atomic_json(runtime_dir / "launcher-process.json", record)
    return child, record


@contextlib.contextmanager
def _launch_lock(runtime_dir: Path):
    runtime_dir.mkdir(parents=True, exist_ok=True)
    with (runtime_dir / "bootstrap.lock").open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            if not stream.read(1):
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                _fail("DATABASE_START_IN_PROGRESS", "另一个启动器正在准备数据库，请稍后重试。")
        yield


def _ensure(root, runtime_dir, check_only, timeout, progress):
    lock = _read_json(root / "services/inoproshop-mcp/cgc-runtime-lock.json")
    version = lock["neo4j_server"]
    home = runtime_dir / "runtime" / ("neo4j-community-" + version)
    credentials, external = _credentials(runtime_dir)
    probe = _probe(credentials, version) if credentials else {"status": "missing", "code": "DATABASE_CREDENTIALS_MISSING"}
    if probe["status"] == "ready":
        java, _ = _find_java(runtime_dir)
        result = {**credentials, "status": "ready", "reused": True, "managed": False,
                  "java_home": str(java) if java else None, "neo4j_home": str(home) if home.exists() else None}
        process_file = runtime_dir / "launcher-process.json"
        if process_file.exists():
            record = _read_json(process_file)
            if process_matches(record) and Path(record.get("neo4j_home", "")).resolve() == home.resolve():
                result.update(managed=True, pid=record["pid"])
        if not check_only and (runtime_dir / "credentials.json").exists():
            restrict_access(runtime_dir / "credentials.json")
        return result
    if check_only:
        return {**probe, "message": "数据库未就绪；检查模式不会安装或启动组件。"}
    if probe["status"] == "blocked":
        _fail(probe["code"], "数据库认证、权限或版本检查失败；不会自动重置已有数据库。")
    if external or credentials and credentials["uri"] != LOCAL_URI:
        _fail("DATABASE_EXTERNAL_UNAVAILABLE", "已配置数据库无法访问；请启动原实例，启动器不会替换该连接。")
    if _port_open(LOCAL_URI):
        _fail("DATABASE_PORT_CONFLICT", "本机端口 17687 已占用，但数据库验证未通过；未停止或替换占用进程。")
    if credentials is None and _has_data(home):
        _fail("DATABASE_CREDENTIALS_MISSING", "已有数据库数据但凭据缺失；请恢复 credentials.json，启动器不会重置密码或数据。")
    process_file = runtime_dir / "launcher-process.json"
    if process_file.exists() and process_matches(_read_json(process_file)):
        _fail("DATABASE_START_IN_PROGRESS", "已有受管数据库进程尚未就绪；请检查日志后重试。")
    java, java_version = _ensure_java(root, runtime_dir, progress)
    if not (home / "lib" / ("neo4j-" + version + ".jar")).is_file():
        progress("正在准备数据库运行文件…")
        package = _archive(lock["neo4j_archive_url"], lock["neo4j_archive_sha256"], runtime_dir / "downloads",
            candidates=[root / "offline/archives/neo4j.zip", *runtime_dir.glob("*.zip")],
            offline=(root / "offline").is_dir())
        _extract(package, home, "neo4j-community-" + version)
        (home / "conf/neo4j.conf").write_text(CONFIG, encoding="utf-8")
    _validate_configuration(home)
    if credentials is None:
        credentials = {"uri": LOCAL_URI, "username": "neo4j", "password": secrets.token_urlsafe(36)}
        _write_secret(runtime_dir / "credentials.json", json.dumps(credentials, ensure_ascii=True))
    else:
        restrict_access(runtime_dir / "credentials.json")
    if not _has_data(home):
        _initialize_password(home, java, runtime_dir, credentials["password"])
    atomic_json(runtime_dir / "runtime-lock.json", {"java_home": str(java), "java_version": java_version,
                "neo4j_version": version, "cgc_version": lock["codegraphcontext"],
                "neo4j_driver": lock["neo4j_python_driver"], "uri": LOCAL_URI})
    progress("正在启动数据库并验证连接…")
    child, record = _start(home, java, runtime_dir)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child.poll() is not None:
            _fail("DATABASE_START_FAILED", "数据库启动进程已退出；请检查数据库启动日志。")
        state = _probe(credentials, version, timeout=min(4, max(1, deadline - time.monotonic())))
        if state["status"] == "ready":
            return {**credentials, "status": "ready", "reused": False, "managed": True,
                    "java_home": str(java), "neo4j_home": str(home), "pid": record["pid"]}
        if state["status"] == "blocked":
            _fail(state["code"], "数据库已启动但认证或版本验证失败；进程记录与日志已保留。")
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    _fail("DATABASE_START_TIMEOUT", "数据库启动等待超时；进程记录与日志已保留，请检查后重试。")


def ensure_database(root: Path, runtime_dir: Path, check_only=False, timeout=120, progress=print):
    """Ensure database readiness without resetting data, credentials or other servers.

    Check mode never creates directories, downloads, changes permissions, starts
    a service or writes files. It returns an explicit status instead of repairing.
    """
    root, runtime_dir = Path(root).resolve(), Path(runtime_dir).resolve()
    try:
        if check_only:
            return _ensure(root, runtime_dir, True, timeout, progress)
        with _launch_lock(runtime_dir):
            return _ensure(root, runtime_dir, False, timeout, progress)
    except DatabaseLauncherError as exc:
        if check_only:
            return {"status": "blocked", "code": exc.code, "message": exc.message}
        raise
