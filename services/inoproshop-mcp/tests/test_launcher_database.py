"""Launcher DB lifecycle boundaries; no native service or real downloads."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from inoproshop_mcp import launcher_database as db


@pytest.fixture
def setup(tmp_path, monkeypatch):
    for key in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    root = tmp_path / "root"
    service = root / "services/inoproshop-mcp"
    service.mkdir(parents=True)
    lock = {"neo4j_server": "5.26.12", "neo4j_archive_url": "https://example.invalid/neo4j.zip",
            "neo4j_archive_sha256": "a" * 64, "codegraphcontext": "0.6.13", "neo4j_python_driver": "5.28.2"}
    (service / "cgc-runtime-lock.json").write_text(json.dumps(lock))
    runtime = root / "runtime"
    return root, runtime


def credentials(runtime, uri=db.LOCAL_URI):
    runtime.mkdir(parents=True, exist_ok=True)
    value = {"uri": uri, "username": "neo4j", "password": "a-private-test-password"}
    (runtime / "credentials.json").write_text(json.dumps(value))
    return value


def native_home(runtime):
    home = runtime / "runtime/neo4j-community-5.26.12"
    (home / "lib").mkdir(parents=True)
    (home / "lib/neo4j-5.26.12.jar").touch()
    (home / "conf").mkdir()
    (home / "conf/neo4j.conf").write_text(db.CONFIG)
    return home


def forbidden(*args, **kwargs):
    raise AssertionError("Unexpected side effect")


def test_check_missing_does_not_create_runtime(setup, monkeypatch):
    root, runtime = setup
    monkeypatch.setattr(db, "_start", forbidden)
    monkeypatch.setattr(db, "_archive", forbidden)
    result = db.ensure_database(root, runtime, check_only=True)
    assert result["status"] == "missing"
    assert not runtime.exists()


def test_check_auth_failure_never_changes_password(setup, monkeypatch):
    root, runtime = setup
    credentials(runtime)
    before = (runtime / "credentials.json").read_bytes()
    monkeypatch.setattr(db, "_probe", lambda *a, **k: {"status": "blocked", "code": "DATABASE_AUTH_FAILED"})
    monkeypatch.setattr(db, "restrict_access", forbidden)
    monkeypatch.setattr(db, "_initialize_password", forbidden)
    result = db.ensure_database(root, runtime, check_only=True)
    assert result["code"] == "DATABASE_AUTH_FAILED"
    assert (runtime / "credentials.json").read_bytes() == before
    assert not (runtime / "bootstrap.lock").exists()


def test_healthy_instance_is_reused_without_provisioning(setup, monkeypatch):
    root, runtime = setup
    creds = credentials(runtime)
    monkeypatch.setattr(db, "_probe", lambda *a, **k: {"status": "ready"})
    monkeypatch.setattr(db, "_find_java", lambda *a: (None, None))
    monkeypatch.setattr(db, "_start", forbidden)
    monkeypatch.setattr(db, "_ensure_java", forbidden)
    monkeypatch.setattr(db, "restrict_access", lambda *a: None)
    result = db.ensure_database(root, runtime)
    assert result["reused"] is True and result["managed"] is False
    assert result["password"] == creds["password"]


def test_unavailable_external_environment_is_not_replaced(setup, monkeypatch):
    root, runtime = setup
    for key, value in {"NEO4J_URI": "bolt://db.example.invalid:7687", "NEO4J_USERNAME": "user", "NEO4J_PASSWORD": "private"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(db, "_probe", lambda *a, **k: {"status": "unavailable"})
    monkeypatch.setattr(db, "_ensure_java", forbidden)
    with pytest.raises(db.DatabaseLauncherError) as caught:
        db.ensure_database(root, runtime)
    assert caught.value.code == "DATABASE_EXTERNAL_UNAVAILABLE"
    assert not (runtime / "credentials.json").exists()


def test_port_conflict_does_not_start_or_replace(setup, monkeypatch):
    root, runtime = setup
    monkeypatch.setattr(db, "_port_open", lambda uri: True)
    monkeypatch.setattr(db, "_start", forbidden)
    monkeypatch.setattr(db, "_ensure_java", forbidden)
    with pytest.raises(db.DatabaseLauncherError) as caught:
        db.ensure_database(root, runtime)
    assert caught.value.code == "DATABASE_PORT_CONFLICT"


def test_existing_data_without_credentials_is_preserved(setup, monkeypatch):
    root, runtime = setup
    home = native_home(runtime)
    data = home / "data/databases/system"
    data.mkdir(parents=True)
    (data / "important-store").write_text("keep")
    monkeypatch.setattr(db, "_port_open", lambda uri: False)
    monkeypatch.setattr(db, "_initialize_password", forbidden)
    with pytest.raises(db.DatabaseLauncherError) as caught:
        db.ensure_database(root, runtime)
    assert caught.value.code == "DATABASE_CREDENTIALS_MISSING"
    assert (data / "important-store").read_text() == "keep"
    assert not (runtime / "credentials.json").exists()


def test_cache_uses_verified_bytes_instead_of_filename(tmp_path, monkeypatch):
    incomplete = tmp_path / "neo4j.zip"
    incomplete.write_bytes(b"truncated")
    complete = tmp_path / "neo4j-complete.zip"
    complete.write_bytes(b"complete-package")
    monkeypatch.setattr(db, "urlopen", forbidden)
    expected = hashlib.sha256(complete.read_bytes()).hexdigest()
    assert db._archive("https://example.invalid/file.zip", expected, tmp_path / "cache", [incomplete, complete]) == complete
    assert not (tmp_path / "cache").exists()


def test_failed_download_hash_never_promotes_archive(tmp_path, monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def geturl(self): return "https://example.invalid/file.zip"
        def read(self, size=-1):
            if getattr(self, "sent", False): return b""
            self.sent = True
            return b"bad-download"
    monkeypatch.setattr(db, "urlopen", lambda *a, **k: Response())
    with pytest.raises(db.DatabaseLauncherError) as caught:
        db._archive("https://example.invalid/file.zip", "a" * 64, tmp_path)
    assert caught.value.code == "DATABASE_ARCHIVE_HASH_MISMATCH"
    assert not list(tmp_path.iterdir())


def test_archive_cannot_escape_extraction_directory(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("../outside", "bad")
        package.writestr("root/file", "okay")
    with pytest.raises(db.DatabaseLauncherError) as caught:
        db._extract(archive, tmp_path / "runtime", "root")
    assert caught.value.code == "DATABASE_ARCHIVE_INVALID"
    assert not (tmp_path / "outside").exists()
    assert not (tmp_path / "runtime").exists()


def test_java_24_on_path_does_not_override_recorded_21(setup, monkeypatch):
    root, runtime = setup
    runtime.mkdir()
    (runtime / "runtime-lock.json").write_text(json.dumps({"java_home": str(root / "java21")}))
    monkeypatch.setenv("JAVA_HOME", str(root / "java24"))
    monkeypatch.setattr(db.shutil, "which", lambda name: str(root / "java24/bin/java.exe"))
    monkeypatch.setattr(db, "_java_version", lambda path: "21.0.6" if path.name == "java21" else "24")
    home, version = db._find_java(runtime)
    assert home == (root / "java21").resolve()
    assert version == "21.0.6"


def test_recycled_pid_is_not_owned(monkeypatch):
    record = {"pid": 42, "created": "old-start-time", "executable": "java.exe"}
    monkeypatch.setattr(db, "process_identity", lambda pid: {**record, "created": "new-start-time"})
    assert db.process_matches(record) is False


def test_initial_password_uses_private_argument_file_not_argv(tmp_path, monkeypatch):
    private = "secret-that-must-not-appear-in-arguments"
    checks = []
    monkeypatch.setattr(db, "restrict_access", lambda path: checks.append(("acl", path)))
    def run(argv, **kwargs):
        assert private not in repr(argv)
        assert "--disable-@files" in argv
        assert not any(item.startswith("-Dbasedir=") for item in argv)
        assert kwargs["cwd"] == tmp_path / "bin"
        assert "NEO4J_HOME" not in kwargs["env"] and "NEO4J_CONF" not in kwargs["env"]
        path = Path(argv[-1][1:])
        assert checks == [("acl", path)]
        assert private in path.read_text()
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(db.subprocess, "run", run)
    db._initialize_password(tmp_path, tmp_path, tmp_path, private)
    assert not list(tmp_path.glob(".initial-password-*"))


def test_start_preserves_credentials_and_ignores_stale_pid(setup, monkeypatch):
    root, runtime = setup
    creds = credentials(runtime)
    home = native_home(runtime)
    (home / "data/databases/system").mkdir(parents=True)
    (runtime / "launcher-process.json").write_text(json.dumps({"pid": 77, "created": "stale", "executable": "old.exe"}))
    probes = iter([{"status": "unavailable"}, {"status": "ready"}])
    monkeypatch.setattr(db, "_probe", lambda *a, **k: next(probes))
    monkeypatch.setattr(db, "_port_open", lambda *a: False)
    monkeypatch.setattr(db, "process_matches", lambda *a: False)
    monkeypatch.setattr(db, "_ensure_java", lambda *a: (root / "java21", "21.0.6"))
    monkeypatch.setattr(db, "restrict_access", lambda *a: None)
    monkeypatch.setattr(db, "_initialize_password", forbidden)
    monkeypatch.setattr(db, "_start", lambda *a: (SimpleNamespace(poll=lambda: None), {"pid": 88}))
    messages = []
    result = db.ensure_database(root, runtime, progress=messages.append)
    assert result["pid"] == 88 and result["reused"] is False
    assert result["password"] == creds["password"]
    assert creds["password"] not in repr(messages)
    assert creds["password"] not in (runtime / "runtime-lock.json").read_text()


def test_start_timeout_keeps_owned_process_record_for_diagnosis(setup, monkeypatch):
    root, runtime = setup
    credentials(runtime)
    home = native_home(runtime)
    (home / "data/databases/system").mkdir(parents=True)
    monkeypatch.setattr(db, "_probe", lambda *a, **k: {"status": "unavailable"})
    monkeypatch.setattr(db, "_port_open", lambda *a: False)
    monkeypatch.setattr(db, "_ensure_java", lambda *a: (root / "java21", "21.0.6"))
    monkeypatch.setattr(db, "restrict_access", lambda *a: None)
    def start(*args):
        record = {"pid": 88, "created": "known", "executable": "java.exe"}
        (runtime / "launcher-process.json").write_text(json.dumps(record))
        return SimpleNamespace(poll=lambda: None), record
    monkeypatch.setattr(db, "_start", start)
    with pytest.raises(db.DatabaseLauncherError) as caught:
        db.ensure_database(root, runtime, timeout=0, progress=lambda text: None)
    assert caught.value.code == "DATABASE_START_TIMEOUT"
    assert json.loads((runtime / "launcher-process.json").read_text())["pid"] == 88


@pytest.mark.skipif(os.name != "nt", reason="Windows identity API")
def test_process_identity_recognizes_current_process():
    record = db.process_identity(os.getpid())
    assert record and record["pid"] == os.getpid()
    assert db.process_matches(record)


def test_probe_authentication_and_version_are_real_query_requirements(monkeypatch):
    import neo4j
    queries = []
    class Session:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def run(self, query):
            queries.append(str(query))
            return SimpleNamespace(single=lambda: {"ready": 1} if "RETURN 1" in str(query) else {"versions": ["5.26.11"]})
    class Driver:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def session(self, **kwargs): return Session()
    monkeypatch.setattr(neo4j.GraphDatabase, "driver", lambda *a, **k: Driver())
    monkeypatch.setattr(db, "_port_open", lambda uri: True)
    result = db._probe({"uri": db.LOCAL_URI, "username": "neo4j", "password": "private"}, "5.26.12")
    assert result["code"] == "DATABASE_VERSION_MISMATCH"
    assert len(queries) == 2 and all("CREATE" not in query for query in queries)


def test_network_access_denied_is_never_treated_as_free_port(monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError(13, "network denied")
    monkeypatch.setattr(db.socket, "create_connection", denied)
    with pytest.raises(db.DatabaseLauncherError) as caught:
        db._port_open(db.LOCAL_URI)
    assert caught.value.code == "DATABASE_NETWORK_DENIED"
    result = db._probe({"uri": db.LOCAL_URI, "username": "neo4j", "password": "private"}, "5.26.12")
    assert result == {"status": "blocked", "code": "DATABASE_NETWORK_DENIED"}
