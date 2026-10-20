"""Reuse must prove the running controller owns the requested configuration."""
import json
import os

import pytest

from inoproshop_mcp.__main__ import database_fingerprint
from inoproshop_mcp import launcher, launcher_database


@pytest.fixture
def manifest(tmp_path, monkeypatch):
    state = tmp_path / "state"
    projects = tmp_path / "projects"
    state.mkdir()
    token = "MCP-private-token-" + "x" * 40
    (state / "token").write_text(token)
    monkeypatch.delenv("NEO4J_DATABASE", raising=False)
    database = {"status": "ready", "uri": "bolt://127.0.0.1:17687", "username": "neo4j", "password": "db-secret-one"}
    environment = {"NEO4J_URI": database["uri"], "NEO4J_USERNAME": database["username"],
                   "NEO4J_PASSWORD": database["password"], "NEO4J_DATABASE": None}
    process = {"pid": 123, "created": "2026-10-19T01:02:03", "executable": "C:/runtime/python.exe"}
    value = {"pid": 123, "state_dir": str(state.resolve()), "project_root": str(projects.resolve()), "port": 8765,
             "process_identity": process, "database_fingerprint": database_fingerprint(token, environment)}
    (state / "service-runtime.json").write_text(json.dumps(value))
    monkeypatch.setattr(launcher_database, "process_identity", lambda pid: dict(process))
    return state, projects, database, value


def write_manifest(state, value):
    (state / "service-runtime.json").write_text(json.dumps(value))


def test_same_live_controller_configuration_can_be_reused(manifest):
    state, projects, database, value = manifest
    assert launcher.check_controller_identity(state, projects, 8765, database) == value
    raw = (state / "service-runtime.json").read_text()
    assert database["password"] not in raw
    assert (state / "token").read_text() not in raw


@pytest.mark.parametrize("key,value", [("uri", "bolt://127.0.0.1:27687"), ("username", "different"), ("password", "changed-password")])
def test_old_controller_database_settings_cannot_appear_ready(manifest, key, value):
    state, projects, database, _ = manifest
    database[key] = value
    with pytest.raises(launcher.LauncherError) as caught:
        launcher.check_controller_identity(state, projects, 8765, database)
    assert caught.value.code == "MCP_DATABASE_CONFIG_MISMATCH"
    assert database["password"] not in caught.value.message


def test_named_database_change_requires_restart(manifest, monkeypatch):
    state, projects, database, _ = manifest
    monkeypatch.setenv("NEO4J_DATABASE", "different_database")
    with pytest.raises(launcher.LauncherError) as caught:
        launcher.check_controller_identity(state, projects, 8765, database)
    assert caught.value.code == "MCP_DATABASE_CONFIG_MISMATCH"


@pytest.mark.parametrize("field", ["process_identity", "database_fingerprint", "file"])
def test_legacy_service_requires_normal_restart(manifest, field):
    state, projects, database, value = manifest
    if field == "file":
        (state / "service-runtime.json").unlink()
    else:
        value.pop(field)
        write_manifest(state, value)
    with pytest.raises(launcher.LauncherError) as caught:
        launcher.check_controller_identity(state, projects, 8765, database)
    assert caught.value.code == "MCP_LEGACY_RESTART_REQUIRED"


def test_stale_process_manifest_does_not_prove_controller_identity(manifest, monkeypatch):
    state, projects, database, value = manifest
    recycled = dict(value["process_identity"], created="2026-10-19T09:09:09")
    monkeypatch.setattr(launcher_database, "process_identity", lambda pid: recycled)
    with pytest.raises(launcher.LauncherError) as caught:
        launcher.check_controller_identity(state, projects, 8765, database)
    assert caught.value.code == "MCP_PROCESS_IDENTITY_MISMATCH"


def test_mismatched_manifest_pid_is_rejected(manifest):
    state, projects, database, value = manifest
    value["pid"] += 1
    write_manifest(state, value)
    with pytest.raises(launcher.LauncherError) as caught:
        launcher.check_controller_identity(state, projects, 8765, database)
    assert caught.value.code == "MCP_PROCESS_IDENTITY_MISMATCH"


def test_unavailable_database_check_still_verifies_live_controller(manifest):
    state, projects, _, value = manifest
    assert launcher.check_controller_identity(state, projects, 8765, {"status": "unavailable"}) == value
    with pytest.raises(launcher.LauncherError) as caught:
        launcher.check_controller_identity(state, projects / "wrong", 8765, {"status": "unavailable"})
    assert caught.value.code == "MCP_CONFIG_MISMATCH"


def test_fingerprint_is_keyed_and_canonical():
    env = {"NEO4J_URI": "bolt://127.0.0.1:17687", "NEO4J_PASSWORD": "private", "NEO4J_USERNAME": "neo4j"}
    reordered = {key: env[key] for key in reversed(env)}
    fingerprint = database_fingerprint("token-one", env)
    assert fingerprint == database_fingerprint("token-one", reordered)
    assert fingerprint != database_fingerprint("token-two", env)
    assert len(fingerprint) == 64
    assert "private" not in fingerprint
