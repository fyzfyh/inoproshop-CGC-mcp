"""Real subprocess exits and MCP sessions; host/graph remain explicit fakes."""
from __future__ import annotations

from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import pytest

from inoproshop_mcp.common import atomic_json


def start(root, mode, restart=False):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    root.mkdir(parents=True, exist_ok=True)
    output = (root / ("restart.log" if restart else "first.log")).open("w", encoding="utf-8")
    command = [sys.executable, str(Path(__file__).with_name("server_process_fixture.py")), str(root), mode, str(port)]
    if restart:
        command.append("--restart")
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    proc = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT, **options)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError("Owned test server exited: " + (root / ("restart.log" if restart else "first.log")).read_text(encoding="utf-8"))
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    return proc, output, f"http://127.0.0.1:{port}/mcp"
            time.sleep(0.025)
        raise AssertionError("Owned test server did not start")
    except BaseException:
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=10)
        output.close()
        raise


def stop(proc, output):
    if proc.poll() is None:
        proc.terminate()
    proc.wait(timeout=10)
    output.close()


@asynccontextmanager
async def session(root, url):
    token = (root / "state/token").read_text(encoding="ascii").strip()
    async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}, trust_env=False, timeout=10) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as client:
                await client.initialize()
                yield client


def record(mode, evidence):
    folder = os.environ.get("INO_MCP_PROCESS_EVIDENCE")
    if folder:
        atomic_json(Path(folder) / (mode + ".json"), dict(evidence, layer="actual_controller_subprocess_with_fake_host_and_local_ir", native_evidence=False))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,expected", [("write", "OUTCOME_UNKNOWN"), ("create", "OUTCOME_UNKNOWN"), ("attach", "OUTCOME_UNKNOWN"), ("apply_compile", "OUTCOME_UNKNOWN"), ("compile", "FAILED"), ("save", "OUTCOME_UNKNOWN"), ("index", "FAILED")])
async def test_hard_exit_restart_never_replays_native_call(tmp_path, mode, expected):
    root = tmp_path / mode
    first, first_output, url = start(root, mode)
    boot = json.loads((root / "boot.json").read_text(encoding="utf-8"))
    try:
        try:
            async with session(root, url) as client:
                result = await client.call_tool(boot["tool"], boot["args"])
                assert not result.isError
        except BaseException:
            # A request may have lost its response when the process terminated;
            # the durable ledger, not the transport exception, is authoritative.
            if first.poll() is None:
                first.wait(timeout=10)
            if first.returncode != 86:
                raise
        assert first.wait(timeout=10) == 86
        crash = json.loads((root / "crash.json").read_text(encoding="utf-8"))
        calls_before = (root / "fake_calls.jsonl").read_bytes() if (root / "fake_calls.jsonl").exists() else b""
        with sqlite3.connect(root / "state/ledger.sqlite3") as db:
            operation = json.loads(db.execute("SELECT value FROM operations WHERE idem='crash-once'").fetchone()[0])
        assert operation["state"] not in {"COMPLETED", "FAILED", "OUTCOME_UNKNOWN"}
        restarted, second_output, second_url = start(root, mode, restart=True)
        try:
            async with session(root, second_url) as client:
                queried = await client.call_tool("ino_get_operation", {"project_id": boot["project_id"], "operation_id": operation["operation_id"]})
                assert not queried.isError
                assert queried.structuredContent["state"] == expected
                retried = await client.call_tool(boot["tool"], boot["args"])
                assert not retried.isError
                assert retried.structuredContent["deduplicated"] is True
                assert retried.structuredContent["operation_id"] == operation["operation_id"]
            calls_after = (root / "fake_calls.jsonl").read_bytes() if (root / "fake_calls.jsonl").exists() else b""
            assert calls_after == calls_before
            if mode == "index":
                assert not list((root / "state/indexes").rglob("active.json"))
            record(mode, {"status": "passed", "crash": crash, "persisted_state_at_exit": operation["state"], "after_restart": expected,
                          "operation_id_preserved": True, "same_key_deduplicated": True, "native_calls_replayed": 0})
        finally:
            stop(restarted, second_output)
    finally:
        stop(first, first_output)


@pytest.mark.asyncio
async def test_client_disconnect_does_not_cancel_durable_job(tmp_path):
    root = tmp_path / "disconnect"
    proc, output, url = start(root, "disconnect")
    try:
        boot = json.loads((root / "boot.json").read_text(encoding="utf-8"))
        async with session(root, url) as client:
            accepted = await client.call_tool(boot["tool"], boot["args"])
            assert not accepted.isError
            operation_id = accepted.structuredContent["operation_id"]
        # The initiating HTTP/MCP session is fully closed before work resumes.
        deadline = time.monotonic() + 10
        while not (root / "blocked.json").exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        (root / "release").write_text("continue fake compile", encoding="ascii")
        async with session(root, url) as client:
            for _ in range(100):
                queried = await client.call_tool("ino_get_operation", {"project_id": boot["project_id"], "operation_id": operation_id})
                if queried.structuredContent["state"] == "COMPLETED":
                    break
                import asyncio
                await asyncio.sleep(0.02)
            assert queried.structuredContent["state"] == "COMPLETED"
            assert queried.structuredContent["result"]["compile_status"] == "passed"
        record("disconnect", {"status": "passed", "initiating_session_closed_before_completion": True,
                              "new_session_observed_completed": True, "controller_survived": proc.poll() is None})
    finally:
        stop(proc, output)
