"""P0 negative workflow cases. FakeHost proves orchestration, never OEM behavior."""
import copy

import pytest

from inoproshop_mcp.common import DomainError
from test_core import setup, snapshot, preview, submit, wait


def test_reused_pid_cannot_revive_previous_host_instance(setup):
    core, host, project = setup
    old = dict(host.records()[0], pid=12345)
    core._hosts = lambda: [old]
    patch = preview(core, project)
    replacement = dict(old, host_instance_id="replacement-instance")
    core._hosts = lambda: [replacement]
    host.commands.clear()
    result = wait(core, project, submit(core, project, patch))
    assert result["state"] == "REJECTED"
    assert result["error"]["code"] == "HOST_UNAVAILABLE"
    assert host.commands == []
    assert host.write_count == 0
    registered = core.ino_list_projects()["projects"]
    assert len(registered) == 1
    assert registered[0]["project_id"] != project
    assert registered[0]["host_instance_id"] == "replacement-instance"


def test_two_live_hosts_with_same_project_path_are_not_writable(setup):
    core, host, project = setup
    patch = preview(core, project)
    original = host.records()[0]
    core._hosts = lambda: [original, dict(original, host_id="second-host",
        host_instance_id="second-instance", binding_id="second-binding")]
    registered = core.ino_list_projects()["projects"]
    assert len({item["project_id"] for item in registered}) == 2
    host.commands.clear()
    result = wait(core, project, submit(core, project, patch))
    assert result["error"]["code"] == "PROJECT_BUSY"
    assert host.commands == []
    assert host.write_count == 0


def test_dependency_change_rejects_old_patch_even_when_target_unchanged(setup):
    core, host, project = setup
    dependency = {"id": "settings", "name": "Settings", "path": "Application/Settings",
                  "kind": "gvl", "application_id": "app", "parent_id": "app",
                  "declaration": "VAR_GLOBAL\n Limit : INT := 10;\nEND_VAR\n",
                  "implementation": None}
    host.nodes.append(dependency)
    patch = preview(core, project)
    target_before = copy.deepcopy(host.nodes[1])
    dependency["declaration"] = dependency["declaration"].replace("10", "20")
    result = wait(core, project, submit(core, project, patch))
    assert result["error"]["code"] == "STALE_SNAPSHOT"
    assert host.nodes[1] == target_before
    assert "20" in dependency["declaration"]
    assert host.write_count == 0


@pytest.mark.parametrize("case,expected", [
    ("existing_name", "NAME_CONFLICT"),
    ("existing_instance", "NAME_CONFLICT"),
    ("raw_xml", "UNSUPPORTED_CAPABILITY"),
    ("inout_interface", "UNSUPPORTED_CAPABILITY"),
    ("missing_output", "INVALID_BINDING"),
    ("nontextual_caller", "UNSUPPORTED_CAPABILITY"),
    ("out_of_range_position", "INVALID_ARGUMENT"),
])
def test_invalid_create_preview_has_no_native_write(setup, case, expected):
    core, host, project = setup
    change = {"type": "create_st_pou", "name": "FB_Test",
        "declaration": "FUNCTION_BLOCK FB_Test\nVAR_INPUT\n enable : BOOL;\nEND_VAR\nVAR_OUTPUT\n result : BOOL;\nEND_VAR\n",
        "implementation": "result := enable;",
        "caller": {"object_id": "pou", "instance_name": "testFB", "inputs": {"enable": "x"},
                   "outputs": {"result": "y"}, "condition": None, "insert_after_line": 1}}
    if case == "existing_name": change["name"] = "plc_prg"
    elif case == "existing_instance": change["caller"]["instance_name"] = "X"
    elif case == "raw_xml": change["declaration"] = "<pou><bad></pou>"
    elif case == "inout_interface": change["declaration"] += "VAR_IN_OUT\n other : BOOL;\nEND_VAR\n"
    elif case == "missing_output": change["caller"]["outputs"] = {}
    elif case == "nontextual_caller": host.nodes[1]["implementation"] = None
    elif case == "out_of_range_position": change["caller"]["insert_after_line"] = 99
    captured = snapshot(core, project)
    before = copy.deepcopy(host.nodes)
    host.commands.clear()
    with pytest.raises(DomainError) as caught:
        core.ino_preview_change(project, captured["snapshot_id"], change)
    assert caught.value.code == expected
    assert host.nodes == before
    assert host.commands == []
    assert host.created_count == host.write_count == 0


@pytest.mark.parametrize("response,expected", [
    ({"status": "passed", "errors": 0, "messages": []}, "unknown"),
    ({"status": "passed", "errors": 1, "messages": [{"severity": "Error"}]}, "unknown"),
    ({"status": "passed", "errors": 0, "messages": [{"severity": "Information"}], "error": "native exception"}, "unknown"),
    ({"status": "accepted", "messages": []}, "unknown"),
    ({"status": "failed", "errors": 1, "messages": [{"severity": "Error"}]}, "failed"),
])
def test_compile_completion_is_not_inferred_from_acceptance_or_empty_messages(setup, response, expected):
    core, host, project = setup
    original = host.call
    host.call = lambda command, params: copy.deepcopy(response) if command == "compile" else original(command, params)
    result = wait(core, project, core.ino_compile(project, "compile-boundary"))
    assert result["state"] == "FAILED"
    assert result["result"]["compile_status"] == expected
    stored = core.ino_get_diagnostics(project, result["operation_id"])["items"]
    assert len(stored) == 1 and stored[0]["status"] == expected
    assert host.write_count == 0
