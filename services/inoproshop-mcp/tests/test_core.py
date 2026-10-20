"""Fault-injected orchestration tests; the fake host is NOT native compiler evidence."""
import copy
import json
import threading
import time
import zipfile
import hashlib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from inoproshop_mcp.common import DomainError, digest, node_hash
from inoproshop_mcp.core import Core
from inoproshop_mcp.store import Store, TERMINAL


class FakeHost:
    def __init__(self, root):
        self.path = str(root / "sample.project")
        Path(self.path).write_text("original-copy", encoding="utf-8")
        self.binding = "binding-1"
        self.nodes = [
            {"id": "app", "name": "Application", "path": "Application", "kind": "application", "application_id": "app", "parent_id": None},
            {"id": "pou", "name": "PLC_PRG", "path": "Application/PLC_PRG", "kind": "pou", "pou_type": "PROGRAM", "application_id": "app", "parent_id": "app",
             "declaration": "PROGRAM PLC_PRG\nVAR\n x : BOOL;\n y : BOOL;\nEND_VAR\n", "implementation": "y := x;\n"},
            {"id": "task", "name": "MainTask", "path": "Application/MainTask", "kind": "task", "application_id": "app", "parent_id": "app",
             "calls": [{"target_id": "pou", "name": "PLC_PRG", "resolution": "unique_program_in_application", "evidence": "task_api.pous"}]},
        ]
        self.write_count = 0
        self.created_count = 0
        self.commands = []
        self.fault = None
        self.capabilities = {k: {"status": "supported"} for k in ("delete_object", "create_st_pou", "two_object_recovery", "save_copy")}

    def records(self):
        return [{"host_id": "host", "host_instance_id": "instance", "binding_id": self.binding,
                 "project_path": self.path, "available": True}]

    def snapshot(self):
        value = {"project_path": self.path, "binding_id": self.binding, "nodes": copy.deepcopy(self.nodes),
                 "relations": [{"kind": "task_calls", "from_id": "task", "to_id": "pou", "evidence": "task_api.pous"}],
                 "active_application_id": "app", "project_dirty": bool(self.write_count), "coverage": {"objects": len(self.nodes)}}
        value["content_hash"] = digest(value)
        return value

    def call(self, command, params):
        self.commands.append(command)
        if params.get("expected_binding_id") != self.binding:
            raise DomainError("HOST_RESTARTED", "Binding changed.")
        if params.get("expected_snapshot_hash") and params["expected_snapshot_hash"] != self.snapshot()["content_hash"]:
            raise DomainError("STALE_SNAPSHOT", "Facts changed.")
        if command == "snapshot": return self.snapshot()
        if command == "capabilities": return {"capabilities": self.capabilities}
        if command == "compile":
            broken = any("BROKEN" in (n.get("implementation") or "") for n in self.nodes)
            return {"status": "failed" if broken else "passed", "errors": int(broken), "warnings": 0,
                    "messages": [{"text": "simulated compiler result", "severity": "Error" if broken else "Information"}], "test_double": True}
        if command == "replace_text":
            node = next(n for n in self.nodes if n["id"] == params["object_id"])
            if node_hash(node) != params["expected_node_hash"]: raise DomainError("STALE_PATCH", "Target changed.")
            node.update(declaration=params["declaration"], implementation=params["implementation"])
            self.write_count += 1
            if self.fault:
                code, self.fault = self.fault, None
                raise DomainError(code, "Injected failure after write.")
            return {"status": "matched"}
        if command == "import_st":
            self.created_count += 1
            node = {"id": "fb-" + str(self.created_count), "name": params["name"], "path": "Application/" + params["name"],
                    "declaration": params["declaration"], "implementation": params["implementation"], "kind": "pou", "pou_type": "FUNCTION_BLOCK",
                    "application_id": "app", "parent_id": "app"}
            self.nodes.append(node)
            return node
        if command == "delete_object":
            node = next(n for n in self.nodes if n["id"] == params["object_id"])
            assert node_hash(node) == params["expected_node_hash"]
            self.nodes.remove(node)
            return {"deleted": True}
        if command == "save_copy":
            Path(params["destination"]).write_text(json.dumps(self.nodes), encoding="utf-8")
            return {"path": params["destination"], "binding_id": self.binding}
        raise AssertionError(command)


@pytest.fixture
def setup(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    host = FakeHost(projects)
    core = Core(tmp_path / "state", projects, lambda *_: host, index_backend=False)
    core._hosts = host.records
    project_id = core.ino_list_projects()["projects"][0]["project_id"]
    yield core, host, project_id
    core.close()


def snapshot(core, project_id, indexed=True):
    value = core.ino_capture_snapshot(project_id)
    if indexed: core.index.build(core._snapshot(project_id, value["snapshot_id"]))
    return value


def preview(core, project_id, implementation="y := NOT x;\n", mode="strict"):
    captured = snapshot(core, project_id)
    return core.ino_preview_change(project_id, captured["snapshot_id"],
                                  {"type": "replace_st_implementation", "object_id": "pou", "implementation": implementation}, mode)


def submit(core, project_id, patch, key="apply"):
    auth = {k: patch[k] for k in ("project_id", "patch_id", "patch_hash")}
    return core.ino_apply_change(project_id, patch["patch_id"], key, auth)


def wait(core, project_id, accepted):
    for _ in range(500):
        op = core.ino_get_operation(project_id, accepted["operation_id"])
        if op["state"] in TERMINAL: return op
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def create_patch(core, project_id, body="result := enable;", condition=None):
    captured = snapshot(core, project_id)
    return core.ino_preview_change(project_id, captured["snapshot_id"], {
        "type": "create_st_pou", "name": "FB_Test", "declaration": "FUNCTION_BLOCK FB_Test\nVAR_INPUT\n enable : BOOL;\nEND_VAR\nVAR_OUTPUT\n result : BOOL;\nEND_VAR\n",
        "implementation": body, "caller": {"object_id": "pou", "instance_name": "testFB", "inputs": {"enable": "x"},
        "outputs": {"result": "y"}, "insert_after_line": 1, "condition": condition}})


def test_success_is_idempotent_and_sources_are_immutable(setup):
    core, host, project_id = setup
    patch = preview(core, project_id)
    op = wait(core, project_id, submit(core, project_id, patch))
    assert op["state"] == "COMPLETED", op
    assert op["result"]["compile_status"] == "passed"
    assert host.write_count == 1
    again = submit(core, project_id, patch)
    assert again["deduplicated"] and again["operation_id"] == op["operation_id"]
    assert core._snapshot(project_id, patch["snapshot_id"])["nodes"][1]["implementation"] == "y := x;\n"


def test_stale_preview_never_overwrites_manual_edit(setup):
    core, host, project_id = setup
    patch = preview(core, project_id)
    host.nodes[1]["implementation"] += "// manual\n"
    op = wait(core, project_id, submit(core, project_id, patch))
    assert op["error"]["code"] == "STALE_SNAPSHOT"
    assert host.write_count == 0


def test_negative_compile_restores_and_keeps_both_diagnostics(setup):
    core, host, project_id = setup
    patch = preview(core, project_id, "BROKEN;")
    op = wait(core, project_id, submit(core, project_id, patch))
    assert op["state"] == "RESTORED", op
    assert host.nodes[1]["implementation"] == "y := x;\n"
    reports = core.ino_get_diagnostics(project_id, op["operation_id"])["items"]
    assert {r["status"] for r in reports} == {"passed", "failed"}
    assert op["result"]["candidate_compile"]["status"] == "failed"


def test_bad_baseline_strict_rejects_repair_succeeds(setup):
    core, host, project_id = setup
    host.nodes[1]["implementation"] = "BROKEN;"
    strict = preview(core, project_id)
    rejected = wait(core, project_id, submit(core, project_id, strict, "strict"))
    assert rejected["error"]["code"] == "BASELINE_COMPILE_FAILED"
    repair = preview(core, project_id, mode="repair")
    accepted = wait(core, project_id, submit(core, project_id, repair, "repair"))
    assert accepted["state"] == "COMPLETED", accepted
    assert accepted["result"]["baseline_compile"]["status"] == "failed"


def test_unknown_write_is_frozen_until_conditional_restore(setup):
    core, host, project_id = setup
    patch = preview(core, project_id)
    host.fault = "OPERATION_OUTCOME_UNKNOWN"
    op = wait(core, project_id, submit(core, project_id, patch))
    assert op["state"] == "OUTCOME_UNKNOWN"
    assert host.write_count == 1
    another = preview(core, project_id, "y := x;\n")
    blocked = wait(core, project_id, submit(core, project_id, another, "second"))
    assert blocked["error"]["code"] == "NEEDS_RECOVERY"
    current = snapshot(core, project_id)
    restored = wait(core, project_id, core.ino_restore_checkpoint(project_id, op["checkpoint_id"], current["snapshot_id"], "restore"))
    assert restored["state"] == "RESTORED", restored
    assert host.nodes[1]["implementation"] == "y := x;\n"
    assert not core.store.unresolved(project_id), "A refused follow-up must not leave an unresolvable phantom write."


def test_restore_does_not_cover_later_manual_changes(setup):
    core, host, project_id = setup
    patch = preview(core, project_id)
    op = wait(core, project_id, submit(core, project_id, patch))
    host.nodes[1]["implementation"] += "// user adjustment\n"
    current = snapshot(core, project_id)
    result = wait(core, project_id, core.ino_restore_checkpoint(project_id, op["checkpoint_id"], current["snapshot_id"], "restore"))
    assert result["state"] == "NEEDS_RECOVERY"
    assert "user adjustment" in host.nodes[1]["implementation"]


def test_create_and_attach_compensates_both_objects(setup):
    core, host, project_id = setup
    patch = create_patch(core, project_id, "BROKEN;")
    result = wait(core, project_id, submit(core, project_id, patch))
    assert result["state"] == "RESTORED", result
    assert len(host.nodes) == 3
    assert host.nodes[1]["implementation"] == "y := x;\n"
    assert "testFB" not in host.nodes[1]["declaration"]
    assert host.commands.index("import_st") < host.commands.index("delete_object")


def test_create_gate_cannot_be_bypassed_by_model(setup):
    core, host, project_id = setup
    host.capabilities["two_object_recovery"] = {"status": "unknown"}
    patch = create_patch(core, project_id)
    result = wait(core, project_id, submit(core, project_id, patch))
    assert result["error"]["code"] == "UNSUPPORTED_CAPABILITY"
    assert host.created_count == 0


def test_save_failure_keeps_applied_state_explicit(setup):
    core, host, project_id = setup
    patch = preview(core, project_id)
    auth = {k: patch[k] for k in ("project_id", "patch_id", "patch_hash")}
    result = wait(core, project_id, core.ino_apply_change(project_id, patch["patch_id"], "save", auth, host.path))
    assert result["state"] == "FAILED", result
    assert result["result"]["edit_status"] == "applied"
    assert result["result"]["persistence_status"] == "save_failed"


def test_export_uses_fixed_snapshot_without_host_calls(setup):
    core, host, project_id = setup
    captured = snapshot(core, project_id)
    host.nodes[1]["implementation"] = "manual newer content"
    count = len(host.commands)
    exported = core.ino_export_context(project_id, captured["snapshot_id"])
    assert len(host.commands) == count
    saved = json.loads((Path(exported["directory"]) / "project.json").read_text(encoding="utf-8"))
    assert saved["nodes"][1]["implementation"] == "y := x;\n"
    assert Path(exported["zip"]).parent == Path(exported["directory"])
    with zipfile.ZipFile(exported["zip"]) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert all(hashlib.sha256(archive.read(path)).hexdigest() == expected for path, expected in manifest.items())


def test_export_archive_failure_never_publishes_output_directory(setup, monkeypatch):
    core, host, project_id = setup
    captured = snapshot(core, project_id)
    destination = core.state_dir / "exports" / "failed-export"
    def fail_verification(_stream): return "corrupt/source.st"
    monkeypatch.setattr(zipfile.ZipFile, "testzip", fail_verification)
    with pytest.raises(DomainError, match="verification"):
        core.ino_export_context(project_id, captured["snapshot_id"], str(destination))
    assert not destination.exists()
    assert destination.with_name(destination.name + ".pending").exists()


def test_cross_project_artifact_rejected(setup):
    core, host, project_id = setup
    captured = snapshot(core, project_id)
    with pytest.raises(DomainError, match="belong"):
        core._snapshot("other-project", captured["snapshot_id"])


def test_native_dirty_field_is_exposed_without_changing_source_fingerprint(setup):
    core, host, project_id = setup
    raw = host.snapshot()
    raw.pop("project_dirty")
    raw["dirty"] = True
    captured = core._snapshot_data(project_id, raw)
    assert captured["project_dirty"] is True
    raw["dirty"] = False
    assert core._snapshot_data(project_id, raw)["hash"] == captured["hash"]
    assert core._snapshot_data(project_id, raw)["project_dirty"] is False
    raw["dirty"] = "unknown"
    assert core._snapshot_data(project_id, raw)["project_dirty"] is None


def test_restart_marks_inflight_unknown_without_replay(tmp_path):
    store = Store(tmp_path)
    op, _ = store.create_operation("p", "apply", "once", {"value": 1})
    store.update_operation(op["operation_id"], state="APPLYING")
    store.close()
    restarted = Store(tmp_path)
    assert restarted.operation(op["operation_id"])["state"] == "OUTCOME_UNKNOWN"
    again, created = restarted.create_operation("p", "apply", "once", {"value": 1})
    assert not created and again["operation_id"] == op["operation_id"]
    with pytest.raises(DomainError): restarted.create_operation("p", "apply", "once", {"value": 2})
    restarted.close()


def test_parameter_statement_injection_rejected(setup):
    core, host, project_id = setup
    with pytest.raises(DomainError):
        create_patch(core, project_id, condition="TRUE THEN; BROKEN;")


def test_no_reachable_target_no_write_preview(setup):
    core, host, project_id = setup
    host.nodes[2]["calls"] = []
    original = host.snapshot
    def unlinked():
        result = original()
        result["relations"] = []
        return result
    host.snapshot = unlinked
    with pytest.raises(DomainError, match="reachable"):
        preview(core, project_id)


def test_restore_resumes_after_native_delete_response_lost(setup):
    core, host, project_id = setup
    original = host.call
    lose_once = [True]
    def losing_delete(command, params):
        result = original(command, params)
        if command == "delete_object" and lose_once[0]:
            lose_once[0] = False
            raise DomainError("OPERATION_OUTCOME_UNKNOWN", "Deletion committed; response was lost.")
        return result
    host.call = losing_delete
    patch = create_patch(core, project_id, "BROKEN;")
    failed = wait(core, project_id, submit(core, project_id, patch))
    assert failed["state"] == "OUTCOME_UNKNOWN", failed
    assert len(host.nodes) == 3
    current = snapshot(core, project_id)
    restored = wait(core, project_id, core.ino_restore_checkpoint(project_id, failed["checkpoint_id"], current["snapshot_id"], "resume-delete"))
    assert restored["state"] == "RESTORED", restored
    assert host.commands.count("delete_object") == 1
    assert not core.store.unresolved(project_id)


def test_import_guid_response_lost_never_deletes_by_name_or_reimports(setup):
    core, host, project_id = setup
    original = host.call
    def lose_created_guid(command, params):
        result = original(command, params)
        if command == "import_st":
            raise DomainError("OPERATION_OUTCOME_UNKNOWN", "Import committed but GUID response lost.")
        return result
    host.call = lose_created_guid
    patch = create_patch(core, project_id)
    failed = wait(core, project_id, submit(core, project_id, patch))
    assert failed["state"] == "OUTCOME_UNKNOWN"
    assert host.created_count == 1 and host.write_count == 0
    current = snapshot(core, project_id)
    restored = wait(core, project_id, core.ino_restore_checkpoint(project_id, failed["checkpoint_id"], current["snapshot_id"], "unknown-import"))
    assert restored["state"] == "NEEDS_RECOVERY"
    assert "ownership is unknown" in restored["error"]["message"]
    assert "delete_object" not in host.commands and host.created_count == 1
    assert submit(core, project_id, patch)["deduplicated"]
    assert core.store.unresolved(project_id)


@pytest.mark.parametrize("later_change", [None, "dirty", "source", "instance", "missing_file"])
def test_save_lost_response_reconciliation_never_replays_or_overwrites(setup, later_change):
    core, host, project_id = setup
    original_call, original_snapshot = host.call, host.snapshot
    dirty = [False]
    def snapshot_clean():
        value = original_snapshot()
        value["project_dirty"] = dirty[0]
        return value
    host.snapshot = snapshot_clean
    captured = snapshot(core, project_id)
    destination = core.project_root / "saved.project"
    def lose_save(command, params):
        value = original_call(command, params)
        if command == "save_copy":
            host.path, host.binding = params["destination"], "after-save-binding"
            raise DomainError("OPERATION_OUTCOME_UNKNOWN", "Save committed; reply lost.")
        return value
    host.call = lose_save
    accepted = core.ino_save_copy(project_id, captured["snapshot_id"], str(destination), "save-once")
    failed = wait(core, project_id, accepted)
    assert failed["state"] == "OUTCOME_UNKNOWN" and failed["save_intent_id"]
    assert host.commands.count("save_copy") == 1
    assert core.ino_list_projects()["projects"][0]["project_id"] == project_id
    assert core.ino_save_copy(project_id, captured["snapshot_id"], str(destination), "save-once")["deduplicated"]
    assert core.ino_get_operation(project_id, accepted["operation_id"])["state"] == "OUTCOME_UNKNOWN"
    if later_change == "dirty": dirty[0] = True
    if later_change == "source": host.nodes[1]["implementation"] += "// later saved edit"
    if later_change == "instance":
        core._hosts = lambda: [dict(host.records()[0], host_instance_id="restarted-instance")]
    if later_change == "missing_file": destination.unlink()
    if later_change:
        with pytest.raises(DomainError):
            core.ino_get_operation(project_id, accepted["operation_id"], reconcile=True)
        assert core.store.unresolved(project_id)
    else:
        reconciled = core.ino_get_operation(project_id, accepted["operation_id"], reconcile=True)
        assert reconciled["state"] == "COMPLETED"
        assert reconciled["result"]["persistence_status"] == "saved_copy_reconciled"
        assert reconciled["result"]["save"]["native_writes_replayed"] == 0
        assert reconciled["result"]["save"]["disk_reopen_verified"] is False
        assert core._project(project_id)["binding_id"] == host.binding
        assert not core.store.unresolved(project_id)
        snapshot(core, project_id)
    assert host.commands.count("save_copy") == 1 and host.write_count == 0


def test_cancel_queued_work_never_mutates_host(setup):
    core, host, project_id = setup
    patch = preview(core, project_id)
    with core._lock(project_id):
        accepted = submit(core, project_id, patch)
        cancelled = core.ino_cancel_operation(project_id, accepted["operation_id"])
        assert cancelled["accepted"]
    finished = wait(core, project_id, accepted)
    assert finished["state"] == "CANCELLED"
    assert host.write_count == 0


def test_index_failure_never_repeats_successful_mutation(setup):
    core, host, project_id = setup
    patch = preview(core, project_id)
    def unavailable(_snapshot):
        raise DomainError("INDEX_BACKEND_UNAVAILABLE", "Injected database outage.")
    core.index.build = unavailable
    finished = wait(core, project_id, submit(core, project_id, patch))
    assert finished["state"] == "COMPLETED", finished
    assert finished["result"]["index_status"] == "failed"
    assert host.write_count == 1
    assert submit(core, project_id, patch)["deduplicated"]
    assert host.write_count == 1


def test_new_reference_prevents_created_object_deletion(setup):
    core, host, project_id = setup
    patch = create_patch(core, project_id)
    applied = wait(core, project_id, submit(core, project_id, patch))
    assert applied["state"] == "COMPLETED", applied
    host.nodes.append({"id": "manual", "name": "Manual", "kind": "pou", "path": "Application/Manual",
                       "declaration": "PROGRAM Manual\nVAR\n instance : FB_Test;\nEND_VAR", "implementation": ""})
    current = snapshot(core, project_id)
    restored = wait(core, project_id, core.ino_restore_checkpoint(project_id, applied["checkpoint_id"], current["snapshot_id"], "reference-conflict"))
    assert restored["state"] == "NEEDS_RECOVERY"
    assert "delete_object" not in host.commands
    assert any(n["id"] == applied["result"]["created_object_id"] for n in host.nodes)


def test_changed_source_clears_current_but_retains_historical_diagnostic(setup):
    core, host, project_id = setup
    compiled = wait(core, project_id, core.ino_compile(project_id, "compile"))
    assert core.ino_get_diagnostics(project_id, current=True)["items"]
    host.nodes[1]["implementation"] += "// manual edit\n"
    snapshot(core, project_id)
    assert not core.ino_get_diagnostics(project_id, current=True)["items"]
    assert core.ino_get_diagnostics(project_id, operation_id=compiled["operation_id"])["items"]


def test_failed_compensating_delete_freezes_new_writes(setup):
    core, host, project_id = setup
    original = host.call
    def rejected_delete(command, params):
        if command == "delete_object":
            raise DomainError("NATIVE_REJECTED", "Injected object deletion failure.")
        return original(command, params)
    host.call = rejected_delete
    patch = create_patch(core, project_id, "BROKEN;")
    failed = wait(core, project_id, submit(core, project_id, patch))
    assert failed["state"] == "NEEDS_RECOVERY", failed
    assert len(host.nodes) == 4
    assert host.nodes[1]["implementation"] == "y := x;\n"
    assert core.store.unresolved(project_id)


def test_edits_during_native_compile_never_publish_passing_current_result(setup):
    core, host, project_id = setup
    original = host.call
    def change_during_build(command, params):
        result = original(command, params)
        if command == "compile":
            host.nodes[1]["implementation"] += "// manual change while build pumps messages\n"
        return result
    host.call = change_during_build
    compiled = wait(core, project_id, core.ino_compile(project_id, "concurrent-edit"))
    assert compiled["state"] == "FAILED"
    assert compiled["result"]["compile_status"] == "unknown"
    assert not core.ino_get_diagnostics(project_id, current=True)["items"]
    historical = core.ino_get_diagnostics(project_id, operation_id=compiled["operation_id"])["items"]
    assert historical[0]["native_status"] == "passed"


def test_unknown_read_only_job_does_not_freeze_project_mutation(setup):
    core, host, project_id = setup
    op, _ = core.store.create_operation(project_id, "context_index_snapshot", "index-crash", {})
    core.store.update_operation(op["operation_id"], state="OUTCOME_UNKNOWN")
    assert not core.store.unresolved(project_id)
    applied = wait(core, project_id, submit(core, project_id, preview(core, project_id)))
    assert applied["state"] == "COMPLETED"


def test_monitor_debounces_without_automatic_compile_or_save(setup):
    core, host, project_id = setup
    core.ino_set_monitor(project_id, True, poll_seconds=0.5, debounce_seconds=0.5)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        events = core.ino_get_changes(project_id)["events"]
        if any(e["kind"] == "project_changed" for e in events): break
        time.sleep(0.05)
    else: raise AssertionError("monitor did not publish stable content")
    first = len([e for e in events if e["kind"] == "project_changed"])
    time.sleep(1.2)
    later = core.ino_get_changes(project_id)["events"]
    assert len([e for e in later if e["kind"] == "project_changed"]) == first == 1
    assert "compile" not in host.commands and "save_copy" not in host.commands


def test_monitor_debounce_starts_after_slow_snapshot_response(setup):
    core, host, project_id = setup
    original, original_capture = host.call, core._capture
    observed, captures = [], []
    def slow_first_snapshot(command, params):
        if command == "snapshot" and not observed:
            time.sleep(1)
            result = original(command, params)
            observed.append(time.monotonic())
            return result
        return original(command, params)
    def record_capture(project):
        captures.append(time.monotonic())
        return original_capture(project)
    host.call, core._capture = slow_first_snapshot, record_capture
    core.ino_set_monitor(project_id, True, poll_seconds=0.5, debounce_seconds=0.5)
    deadline = time.monotonic() + 6
    while not captures and time.monotonic() < deadline:
        time.sleep(0.025)
    core.ino_set_monitor(project_id, False)
    assert captures and observed
    assert captures[0] - observed[0] >= 0.5
    core.ino_set_monitor(project_id, False)


@pytest.mark.parametrize("tool,state,checkpoint_id,expected", [
    ("ino_apply_change", "COMPILING", None, "CANCELLED"),
    ("ino_apply_change", "COMPILING", "persisted", "OUTCOME_UNKNOWN"),
    ("ino_apply_change", "CHECKPOINTED", "persisted", "CANCELLED"),
    ("context_index_snapshot", "INDEXING", None, "FAILED"),
    ("ino_compile", "COMPILING", None, "FAILED"),
    ("ino_save_copy", "SAVING", None, "OUTCOME_UNKNOWN"),
])
def test_restart_distinguishes_prewrite_from_unknown_mutation(tmp_path, tool, state, checkpoint_id, expected):
    store = Store(tmp_path)
    op, _ = store.create_operation("project", tool, "restart", {})
    store.update_operation(op["operation_id"], state=state, checkpoint_id=checkpoint_id)
    store.close()
    restarted = Store(tmp_path)
    assert restarted.operation(op["operation_id"])["state"] == expected
    restarted.close()


def test_concurrent_discovery_creates_one_project_identity(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    host = FakeHost(projects)
    core = Core(tmp_path / "state", projects, lambda *_: host, index_backend=False)
    core._hosts = host.records
    original = core.store.all
    def delayed_read(kind):
        result = original(kind)
        if kind == "project": time.sleep(0.02)
        return result
    core.store.all = delayed_read
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            records = list(pool.map(lambda _: core.ino_list_projects()["projects"][0], range(8)))
        assert len({r["project_id"] for r in records}) == 1
        assert len(original("project")) == 1
        core._writable(records[0]["project_id"])
    finally:
        core.close()


def test_historical_context_remains_queryable_with_explicit_staleness(setup):
    core, host, project_id = setup
    before = snapshot(core, project_id)
    host.nodes[1]["implementation"] += "// later version\n"
    snapshot(core, project_id)
    found = core.context_search(project_id, before["snapshot_id"], "x")
    assert found["items"]
    assert found["stale"] is False, "Index still matches its immutable requested snapshot."
    assert found["current_project_stale"] is True
    assert "live_host_not_checked" in found["freshness_basis"]
