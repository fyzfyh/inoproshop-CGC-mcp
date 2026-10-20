import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from inoproshop_mcp import launcher


def test_startup_lock_serializes_and_recovers_after_exception(tmp_path):
    with pytest.raises(ValueError):
        with launcher.startup_lock(tmp_path):
            with pytest.raises(launcher.LauncherError, match="另一个"):
                with launcher.startup_lock(tmp_path):
                    pytest.fail("second launcher entered")
            raise ValueError("owned startup failed")
    with launcher.startup_lock(tmp_path):
        pass


@pytest.mark.asyncio
async def test_port_occupancy_is_not_authenticated_readiness(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "port_open", lambda _: True)
    async def bad(*args):
        raise RuntimeError("Bearer secret must never escape")
    monkeypatch.setattr(launcher, "probe_controller", bad)
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **kw: pytest.fail("must not start on occupied port"))
    with pytest.raises(launcher.LauncherError) as caught:
        await launcher.ensure_controller(tmp_path, tmp_path / "state", tmp_path / "projects", {})
    assert caught.value.code == "MCP_PORT_CONFLICT_OR_AUTH"
    assert "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_reuse_requires_matching_controller_project_root(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    (state / "service-runtime.json").write_text(json.dumps({"state_dir": str(state.resolve()),
        "project_root": str(tmp_path / "wrong"), "port": 8765}))
    monkeypatch.setattr(launcher, "port_open", lambda _: True)
    async def ready(*args):
        return {"status": "ready"}
    monkeypatch.setattr(launcher, "probe_controller", ready)
    with pytest.raises(launcher.LauncherError) as caught:
        await launcher.ensure_controller(tmp_path, state, tmp_path / "projects", {})
    assert caught.value.code == "MCP_CONFIG_MISMATCH"


@pytest.mark.asyncio
async def test_check_missing_controller_is_side_effect_free(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "port_open", lambda _: False)
    result = await launcher.ensure_controller(tmp_path, tmp_path / "missing", tmp_path / "projects", {}, check_only=True)
    assert result["status"] == "missing"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_contract_probe_rejects_non_inoproshop_service(tmp_path, monkeypatch):
    @asynccontextmanager
    async def session(*a, **kw):
        class Client:
            async def list_tools(self):
                return SimpleNamespace(tools=[SimpleNamespace(name="foreign_tool")])
        yield Client()
    monkeypatch.setattr(launcher, "mcp_session", session)
    with pytest.raises(launcher.LauncherError) as caught:
        await launcher.probe_controller(tmp_path, "http://127.0.0.1:8765/mcp")
    assert caught.value.code == "MCP_CONTRACT_MISMATCH"


def install_fake_pipeline(monkeypatch, db_result=None):
    from inoproshop_mcp import launcher_database, launcher_host
    monkeypatch.setattr(launcher_database, "ensure_database", lambda *a, **kw: db_result or {
        "status": "ready", "uri": "bolt://127.0.0.1:17687", "username": "neo4j",
        "password": "NEVER_LOG_THIS", "reused": True})
    async def controller(*a, **kw):
        return {"status": "ready", "url": "http://127.0.0.1:8765/mcp", "reused": True}
    monkeypatch.setattr(launcher, "ensure_controller", controller)
    @asynccontextmanager
    async def session(*a, **kw):
        yield object()
    monkeypatch.setattr(launcher, "mcp_session", session)
    async def host(*a, **kw):
        return {"host_id": "chosen-host", "host_instance_id": "chosen-instance", "pid": 123,
                "project_path": None, "exe": "example.exe", "profile": "verified", "reused": True}
    monkeypatch.setattr(launcher_host, "ensure_host", host)


def test_check_empty_ide_success_no_state_writes_or_secrets(tmp_path, monkeypatch, capsys):
    install_fake_pipeline(monkeypatch)
    assert launcher.main(["--root", str(tmp_path), "--check"]) == 0
    text = capsys.readouterr().out
    assert "请在 IDE 打开工程" in text
    assert "NEVER_LOG_THIS" not in text
    assert list(tmp_path.iterdir()) == []


def test_check_reports_independent_stages_even_when_database_missing(tmp_path, monkeypatch, capsys):
    install_fake_pipeline(monkeypatch, {"status": "missing", "code": "DATABASE_MISSING", "message": "数据库尚未准备"})
    assert launcher.main(["--root", str(tmp_path), "--check"]) == 1
    output = capsys.readouterr().out
    assert "database: missing" in output
    assert "mcp: ready" in output and "ide: ready" in output
    assert list(tmp_path.iterdir()) == []


def test_context_keeps_selected_host_without_database_secret(tmp_path, monkeypatch):
    install_fake_pipeline(monkeypatch)
    assert launcher.main(["--root", str(tmp_path)]) == 0
    context = json.loads((tmp_path / "work/mcp_p0/state/launcher-context.json").read_text(encoding="utf-8"))
    assert context["host_id"] == "chosen-host"
    assert context["host_instance_id"] == "chosen-instance"
    for path in tmp_path.rglob("*.json"):
        assert "NEVER_LOG_THIS" not in path.read_text(encoding="utf-8")


def test_raw_transport_error_is_not_recorded():
    failure = launcher.safe_failure(ValueError("Authorization Bearer sensitive"))
    assert "sensitive" not in json.dumps(failure)


def test_nested_transport_group_preserves_only_controlled_error():
    group = ExceptionGroup("raw token sensitive", [ValueError("sensitive"),
        ExceptionGroup("inner", [launcher.LauncherError("HOST_BUSY", "请在对应 IDE 处理弹窗。")])])
    failure = launcher.safe_failure(group)
    assert failure["code"] == "HOST_BUSY"
    assert "处理弹窗" in failure["message"]
    assert "sensitive" not in json.dumps(failure)
