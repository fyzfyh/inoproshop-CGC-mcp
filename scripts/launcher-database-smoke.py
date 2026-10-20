"""Opt-in Windows acceptance on a private database and port 17688.

The production port 17687 is never probed, stopped, reconfigured or used. Each
native process is suspended until assigned to a kill-on-close Windows JobObject.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/inoproshop-mcp/src"))

from inoproshop_mcp import launcher_database as db
from inoproshop_mcp.common import atomic_json, utc

PORT = 17688
URI = "bolt://127.0.0.1:" + str(PORT)
REPORT = ROOT / "artifacts/mcp_p0/launcher/fresh-database.json"


def note(message):
    print(message, flush=True)


def start_owned(home, java, runtime):
    import win32job
    job = win32job.CreateJobObject(None, "InoMcpSmoke-" + uuid.uuid4().hex)
    limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
    original_flags, child = db._hidden_flags, None
    try:
        db._hidden_flags = lambda: subprocess.CREATE_NO_WINDOW | 0x00000004  # CREATE_SUSPENDED
        child, record = db._start(home, java, runtime)
        win32job.AssignProcessToJobObject(job, child._handle)
        resume = ctypes.WinDLL("ntdll").NtResumeProcess
        resume.argtypes = [wintypes.HANDLE]
        resume.restype = ctypes.c_long
        if resume(int(child._handle)) != 0:
            raise RuntimeError("Could not resume the owned test process")
        return job, child, record
    except BaseException:
        job.Close()
        if child is not None and child.poll() is None:
            # The process has not been resumed unless it belongs to the job.
            # This handle names the exact child, never an independently found PID.
            child.terminate()
            child.wait(timeout=15)
        raise
    finally:
        db._hidden_flags = original_flags


def stop_owned(job, child):
    job.Close()
    child.wait(timeout=20)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if not db._port_open(URI):
            return
        time.sleep(0.25)
    raise RuntimeError("The reserved test port did not close after owned job cleanup")


def wait_ready(child, credentials, version):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError("The private database exited before readiness")
        result = db._probe(credentials, version)
        if result["status"] == "ready":
            return result
        if result["status"] == "blocked":
            raise db.DatabaseLauncherError(result["code"], "私有数据库认证或版本验证失败。")
        time.sleep(0.5)
    raise RuntimeError("Private database startup timed out")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    report = {"schema_version": 1, "at": utc(), "status": "running", "port": PORT,
              "production_database_touched": False, "model_invoked": False,
              "checks": [], "processes": []}
    job = child = None
    previous_java = os.environ.get("JAVA_HOME")
    previous_path = os.environ.get("PATH", "")
    try:
        if os.name != "nt":
            raise RuntimeError("This acceptance requires Windows")
        if db._port_open(URI):
            raise db.DatabaseLauncherError("TEST_PORT_OCCUPIED", "测试端口 17688 已占用；未停止或替换占用进程。")
        report["checks"].append("reserved_test_port_initially_free")
        runtime = Path(tempfile.mkdtemp(prefix="db-smoke-", dir=ROOT / "work/mcp_p0"))
        report["runtime_dir"] = str(runtime)
        os.environ.pop("JAVA_HOME", None)
        # Keep the machine's Java 24 as a real detection candidate. The launcher
        # must reject it, then provision the pinned Java 21 in this fresh directory.
        java24 = Path("D:/JDK/jdk-24/bin")
        if (java24 / "java.exe").is_file():
            os.environ["PATH"] = str(java24) + os.pathsep + previous_path
        path_java = shutil.which("java")
        report["initial_path_java_version"] = db._java_version(Path(path_java).parent.parent) if path_java else None
        java_lock = db._read_json(ROOT / "services/inoproshop-mcp/launcher-runtime-lock.json")["java"]
        java_hash = java_lock["archive_sha256"]
        cached_java = next((path for path in (ROOT / "work/mcp_p0").glob("db-smoke-*/downloads/" + java_hash + ".zip")
                            if db._hash(path) == java_hash), None)
        if cached_java:
            (runtime / "downloads").mkdir()
            shutil.copyfile(cached_java, runtime / "downloads" / (java_hash + ".zip"))
        report["java_archive_source"] = "verified_previous_smoke_download" if cached_java else "official_download"
        note("Preparing a fresh, pinned Java runtime for the isolated test…")
        java, java_version = db._ensure_java(ROOT, runtime, note)
        report.update(java_version=java_version, java_home=str(java), java_provisioned=java.is_relative_to(runtime),
                      java_downloaded=java.is_relative_to(runtime) and not bool(cached_java))
        report["checks"].append("java21_selected_and_verified")
        if report["initial_path_java_version"] and report["initial_path_java_version"].startswith("24"):
            report["checks"].append("path_java24_rejected")
        lock = db._read_json(ROOT / "services/inoproshop-mcp/cgc-runtime-lock.json")
        version = lock["neo4j_server"]
        package = db._archive(lock["neo4j_archive_url"], lock["neo4j_archive_sha256"], runtime / "downloads",
            [ROOT / "work/mcp_p0/neo4j/neo4j-complete.zip"])
        report["checks"].append("neo4j_archive_sha256_verified")
        home = runtime / "runtime" / ("neo4j-community-" + version)
        db._extract(package, home, "neo4j-community-" + version)
        (home / "conf/neo4j.conf").write_text(db.CONFIG.replace("17687", str(PORT)), encoding="utf-8")
        credentials = {"uri": URI, "username": "neo4j", "password": secrets.token_urlsafe(36)}
        db._write_secret(runtime / "credentials.json", json.dumps(credentials))
        db._initialize_password(home, java, runtime, credentials["password"])
        assert not list(runtime.glob(".initial-password-*"))
        report["checks"].append("private_password_initialization_and_secret_file_cleanup")

        note("Starting the private database on 17688 inside its own Windows job…")
        job, child, identity = start_owned(home, java, runtime)
        report["processes"].append(identity)
        wait_ready(child, credentials, version)
        report["checks"].append("real_authenticated_query_and_pinned_version")
        from neo4j import GraphDatabase
        test_id = uuid.uuid4().hex
        with GraphDatabase.driver(URI, auth=(credentials["username"], credentials["password"])) as driver:
            with driver.session() as session:
                session.run("CREATE (:LauncherSmoke {run_id:$run_id, value:'persisted'})", run_id=test_id).consume()
        report["checks"].append("isolated_test_record_committed")
        stop_owned(job, child)
        job = child = None
        report["checks"].append("first_owned_job_closed_and_test_port_released")

        note("Restarting only the private instance to verify persisted data…")
        job, child, identity = start_owned(home, java, runtime)
        report["processes"].append(identity)
        wait_ready(child, credentials, version)
        with GraphDatabase.driver(URI, auth=(credentials["username"], credentials["password"])) as driver:
            with driver.session() as session:
                result = session.run("MATCH (n:LauncherSmoke {run_id:$run_id}) RETURN n.value AS value", run_id=test_id).single()
                assert result and result["value"] == "persisted"
        report["checks"].append("persisted_record_read_after_owned_process_restart")
        report["status"] = "passed"
    except BaseException as exc:
        report.update(status="failed", error_type=type(exc).__name__, code=getattr(exc, "code", "SMOKE_FAILED"))
        frames = traceback.extract_tb(exc.__traceback__)
        if frames:
            report["error_location"] = {"file": frames[-1].filename, "line": frames[-1].lineno, "function": frames[-1].name}
        if isinstance(exc, db.DatabaseLauncherError):
            report["message"] = exc.message
    finally:
        try:
            if job is not None:
                stop_owned(job, child)
                report["checks"].append("final_owned_job_closed_and_test_port_released")
            report["test_port_closed"] = not db._port_open(URI)
            if not report["test_port_closed"]:
                report.update(status="failed", cleanup="test_port_still_occupied")
        except Exception as exc:
            report.update(status="failed", cleanup_error_type=type(exc).__name__)
        if previous_java is None:
            os.environ.pop("JAVA_HOME", None)
        else:
            os.environ["JAVA_HOME"] = previous_java
        os.environ["PATH"] = previous_path
        report["finished_at"] = utc()
        atomic_json(REPORT, report)
        note(json.dumps({"status": report["status"], "checks": len(report["checks"]), "report": str(REPORT)}, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
