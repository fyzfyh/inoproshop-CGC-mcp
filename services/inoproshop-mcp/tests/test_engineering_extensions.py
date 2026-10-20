"""Engineering extension boundaries. Synthetic hosts are not native IDE evidence."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil

import pytest

from inoproshop_mcp.common import DomainError
from inoproshop_mcp.core import Core
from inoproshop_mcp.server import PreviewChange
from inoproshop_mcp.templates import TemplateCatalog, verify_controller
from test_core import FakeHost, setup, snapshot, submit, wait


def sections(core, project, **changes):
    captured = snapshot(core, project)
    return core.ino_preview_change(project, captured["snapshot_id"],
        {"type": "replace_st_sections", "object_id": "pou", **changes})


def test_declaration_only_preserves_implementation_and_uses_existing_recovery(setup):
    core, host, project = setup
    before = copy.deepcopy(host.nodes[1])
    declaration = before["declaration"].replace("END_VAR", " timer1 : TON;\nEND_VAR")
    patch = sections(core, project, declaration=declaration)
    assert host.write_count == 0
    assert patch["changes"][0]["diff"]["declaration"]
    assert not patch["changes"][0]["diff"]["implementation"]
    assert patch["declaration_analysis"]["basis"] == "candidate_static_analysis_not_compiler"
    applied = wait(core, project, submit(core, project, patch))
    assert applied["state"] == "COMPLETED", applied
    assert host.nodes[1]["implementation"] == before["implementation"]
    current = core.ino_capture_snapshot(project)
    restored = wait(core, project, core.ino_restore_checkpoint(project, applied["checkpoint_id"], current["snapshot_id"], "restore-declaration"))
    assert restored["state"] == "RESTORED"
    assert host.nodes[1] == before
    assert submit(core, project, patch)["deduplicated"]
    assert host.write_count == 2


def test_declaration_and_body_compile_failure_restores_both_and_keeps_diagnostics(setup):
    core, host, project = setup
    before = copy.deepcopy(host.nodes[1])
    patch = sections(core, project, declaration=before["declaration"].replace("BOOL", "INT"), implementation="BROKEN;")
    finished = wait(core, project, submit(core, project, patch))
    assert finished["state"] == "RESTORED", finished
    assert host.nodes[1] == before
    records = core.ino_get_diagnostics(project, finished["operation_id"])["items"]
    assert any(item["status"] == "failed" for item in records)
    assert finished["result"]["recovery"]["compile"]["status"] == "passed"
    assert core.ino_get_diagnostics(project, current=True)["items"][0]["status"] == "passed"


def test_declaration_compile_error_alone_restores_original(setup):
    core, host, project = setup
    before = copy.deepcopy(host.nodes[1])
    original = host.call
    def call(command, params):
        if command == "compile" and "BROKEN_DECL" in host.nodes[1]["declaration"]:
            return {"status": "failed", "errors": 1, "messages": [{"severity": "Error", "text": "synthetic declaration error"}]}
        return original(command, params)
    host.call = call
    patch = sections(core, project, declaration=before["declaration"].replace("BOOL", "BROKEN_DECL"))
    assert wait(core, project, submit(core, project, patch))["state"] == "RESTORED"
    assert host.nodes[1] == before


@pytest.mark.parametrize("change,code", [
    ({}, "INVALID_ARGUMENT"), ({"declaration": None}, "INVALID_ARGUMENT"),
    ({"declaration": ""}, "INVALID_DECLARATION"),
    ({"declaration": "PROGRAM Other\nVAR x : BOOL; END_VAR"}, "OBJECT_IDENTITY_CHANGE"),
    ({"declaration": "FUNCTION_BLOCK PLC_PRG\nVAR x : BOOL; END_VAR"}, "OBJECT_IDENTITY_CHANGE"),
    ({"declaration": "PROGRAM PLC_PRG\nPROGRAM Injected"}, "INVALID_DECLARATION"),
])
def test_invalid_declaration_change_never_writes(setup, change, code):
    core, host, project = setup
    with pytest.raises(DomainError) as caught:
        sections(core, project, **change)
    assert caught.value.code == code
    assert host.write_count == 0


def test_header_words_in_comments_and_literals_are_not_object_headers(setup):
    core, host, project = setup
    declaration = "(* FUNCTION_BLOCK Other *)\nPROGRAM PLC_PRG\nVAR\n msg : STRING := 'PROGRAM Another';\n x : BOOL; y : BOOL;\nEND_VAR"
    assert sections(core, project, declaration=declaration)["edited_sections"] == ["declaration"]


def test_stale_declaration_patch_and_manual_recovery_conflict_are_rejected(setup):
    core, host, project = setup
    patch = sections(core, project, declaration=host.nodes[1]["declaration"] + "VAR z : INT; END_VAR\n")
    host.nodes[1]["declaration"] += "// manual"
    failed = wait(core, project, submit(core, project, patch))
    assert failed["error"]["code"] == "STALE_SNAPSHOT" and host.write_count == 0
    patch = sections(core, project, implementation="y := NOT x;")
    applied = wait(core, project, submit(core, project, patch, "new-patch"))
    host.nodes[1]["declaration"] += "\n// manual after applying"
    current = core.ino_capture_snapshot(project)
    restored = wait(core, project, core.ino_restore_checkpoint(project, applied["checkpoint_id"], current["snapshot_id"], "conflict"))
    assert restored["state"] == "NEEDS_RECOVERY"
    assert "manual after applying" in host.nodes[1]["declaration"]


def test_section_schema_omission_empty_string_and_explicit_null():
    args = {"project_id": "p", "snapshot_id": "s", "change": {"type": "replace_st_sections", "object_id": "o", "implementation": ""}}
    parsed = PreviewChange.model_validate(args).model_dump()
    assert parsed["change"] == args["change"]
    args["change"]["declaration"] = None
    with pytest.raises(ValueError):
        PreviewChange.model_validate(args)


def test_text_search_needs_neither_graph_nor_live_host_and_keeps_old_sources(setup):
    core, host, project = setup
    host.nodes[1]["implementation"] = "// 中文电机\r\ny := x;\r\n// 中文电机\r\n"
    captured = snapshot(core, project, indexed=False)
    host.nodes[1]["implementation"] = "manual new content"
    calls = len(host.commands)
    core._hosts = lambda: []
    first = core.context_search_text(project, captured["snapshot_id"], "中文电机", limit=1, context_lines=1)
    second = core.context_search_text(project, captured["snapshot_id"], "中文电机", offset=1, limit=1)
    assert len(host.commands) == calls and core._index_instance is None
    assert first["total"] == 2 and first["next_offset"] == 1
    assert first["items"][0]["line"] == 1 and first["items"][0]["column"] == 4
    assert first["items"][0]["context"]["lines"] == ["// 中文电机", "y := x;"]
    assert second["items"][0]["line"] == 3 and second["next_offset"] is None
    assert first["coverage_scope"] == "available_text_only" and first["graph_required"] is False
    assert first["semantic_references"] is False
    assert first["freshness_basis"] == "last_captured_snapshot; live_host_not_checked"


def test_text_search_literal_filters_unreadable_and_cross_project(setup):
    core, host, project = setup
    host.nodes[1]["implementation"] = "// [.*]\n// AbC\n"
    host.nodes.append({"id": "graphical", "name": "LD", "path": "LD", "kind": "pou", "implementation": None,
                       "source_status": {"implementation": "not_textual"}})
    captured = snapshot(core, project, indexed=False)
    literal = core.context_search_text(project, captured["snapshot_id"], "[.*]", section="implementation", object_id="pou")
    assert literal["total"] == 1
    assert core.context_search_text(project, captured["snapshot_id"], "abc")["total"] == 1
    assert core.context_search_text(project, captured["snapshot_id"], "abc", case_sensitive=True)["total"] == 0
    assert any(c["status"] == "not_textual" for c in core.context_search_text(project, captured["snapshot_id"], "missing")["coverage"])
    with pytest.raises(DomainError, match="Object"):
        core.context_search_text(project, captured["snapshot_id"], "x", object_id="absent")
    with pytest.raises(DomainError):
        core.context_search_text("another-project", captured["snapshot_id"], "x")


@pytest.mark.parametrize("kwargs", [{"query": ""}, {"query": "a\nb"}, {"query": "x", "context_lines": 6},
                                   {"query": "x", "limit": 501}, {"query": "x", "section": "all"}])
def test_text_search_rejects_invalid_boundaries(setup, kwargs):
    core, host, project = setup
    captured = snapshot(core, project, indexed=False)
    with pytest.raises(DomainError):
        core.context_search_text(project, captured["snapshot_id"], **kwargs)
    assert core._index_instance is None


CONTROLLER = {"type": 4102, "id": "0000 0001", "version": "1.0.0.0"}


class TemplateHost(FakeHost):
    def __init__(self, root):
        super().__init__(root)
        self.open_count, self.lose_reply = 0, False
        self.nodes.insert(0, {"id": "controller", "name": "Device", "path": "Device", "kind": "device", "parent_id": None,
            "device_identity": dict(CONTROLLER, status="read")})
        self.nodes[1]["parent_id"] = "controller"

    def call(self, command, params):
        if command != "open_project":
            return super().call(command, params)
        self.commands.append(command)
        if params["expected_binding_id"] != self.binding:
            raise DomainError("HOST_RESTARTED", "binding changed")
        if Path(params["destination"]).exists():
            raise DomainError("PATH_EXISTS", "no overwrite")
        if hashlib.sha256(Path(params["source"]).read_bytes()).hexdigest() != params["expected_source_sha256"]:
            raise DomainError("TEMPLATE_HASH_MISMATCH", "source changed")
        shutil.copy2(params["source"], params["destination"])
        self.path = params["destination"]
        self.open_count += 1
        self.binding = "template-binding-" + str(self.open_count)
        if self.lose_reply:
            raise DomainError("OPERATION_OUTCOME_UNKNOWN", "reply lost")
        return {"project_path": self.path, "binding_id": self.binding}


@pytest.fixture
def templates(tmp_path):
    directory, projects = tmp_path / "templates", tmp_path / "projects"
    directory.mkdir(); projects.mkdir()
    source = directory / "reference.project"
    source.write_bytes(b"synthetic template, not an engineering project")
    entry = {"template_id": "am600", "name": "synthetic AM600", "model": "synthetic model",
             "project_file": source.name, "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "controller": CONTROLLER}
    catalog = directory / "catalog.json"
    catalog.write_text(json.dumps({"schema_version": "1.0", "templates": [entry]}), encoding="utf-8")
    host = TemplateHost(projects)
    core = Core(tmp_path / "state", projects, lambda *_: host, index_backend=False, template_root=directory)
    core._hosts = host.records
    yield core, host, entry, source, catalog
    core.close()


def create(core, entry, key="create", destination="new.project"):
    selected = core.ino_list_project_templates()["templates"][0]
    return core.ino_create_project(entry["template_id"], selected["template_hash"],
        str(core.project_root / destination), "host", "instance", key)


def test_template_create_verifies_native_root_and_is_idempotent(templates):
    core, host, entry, source, catalog = templates
    accepted = create(core, entry)
    result = wait(core, accepted["project_id"], accepted)
    assert result["state"] == "COMPLETED", result
    assert result["result"]["controller_verification"]["status"] == "passed"
    assert host.open_count == 1 and source.read_bytes() == Path(host.path).read_bytes()
    assert any(s["state"] == "VERIFYING_CONTROLLER" for s in result["stages"])
    assert create(core, entry)["deduplicated"] and host.open_count == 1
    core._writable(accepted["project_id"])
    host.nodes[0]["device_identity"]["id"] = "another-controller"
    with pytest.raises(DomainError) as caught:
        core._writable(accepted["project_id"])
    assert caught.value.code == "CONTROLLER_MISMATCH"
    assert host.write_count == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows path identity and native path casing")
def test_native_lowercase_destination_does_not_change_retry_identity(templates):
    core, host, entry, source, catalog = templates
    original = host.call
    def lower_destination(command, params):
        if command == "open_project":
            params = dict(params, destination=os.path.normcase(params["destination"]))
        return original(command, params)
    host.call = lower_destination
    first = create(core, entry, destination="MixedCaseProject.project")
    assert wait(core, first["project_id"], first)["state"] == "COMPLETED"
    again = create(core, entry, destination="MixedCaseProject.project")
    assert again["deduplicated"] and again["operation_id"] == first["operation_id"]
    assert host.open_count == 1


@pytest.mark.parametrize("failure", ["wrong_type", "missing_identity", "multiple_roots"])
def test_controller_failure_leaves_inspectable_copy_but_blocks_writes(templates, failure):
    core, host, entry, source, catalog = templates
    if failure == "wrong_type": host.nodes[0]["device_identity"]["type"] = 123
    elif failure == "missing_identity": host.nodes[0]["device_identity"] = {"status": "unavailable"}
    else: host.nodes.append(dict(host.nodes[0], id="second-root"))
    accepted = create(core, entry)
    result = wait(core, accepted["project_id"], accepted)
    assert result["state"] == "FAILED", result
    assert result["error"]["code"] in {"CONTROLLER_MISMATCH", "CONTROLLER_NOT_VERIFIED"}
    assert core.ino_capture_snapshot(accepted["project_id"])["snapshot_id"]
    with pytest.raises(DomainError) as caught:
        core._writable(accepted["project_id"])
    assert caught.value.code == "CONTROLLER_NOT_VERIFIED"
    assert host.write_count == 0


def test_unknown_create_reply_cannot_gain_unrestricted_identity(templates):
    core, host, entry, source, catalog = templates
    host.lose_reply = True
    accepted = create(core, entry)
    result = wait(core, accepted["project_id"], accepted)
    assert result["state"] == "OUTCOME_UNKNOWN"
    rediscovered = core.ino_list_projects()["projects"][0]
    assert rediscovered["project_id"] == accepted["project_id"]
    assert core.ino_capture_snapshot(rediscovered["project_id"])["snapshot_id"]
    with pytest.raises(DomainError): core._writable(rediscovered["project_id"])
    assert core.store.unresolved(rediscovered["project_id"])
    assert create(core, entry)["deduplicated"] and host.open_count == 1


@pytest.mark.parametrize("original_passed", [True, False])
def test_template_provenance_survives_new_ide_instance(templates, original_passed):
    core, host, entry, source, catalog = templates
    if not original_passed:
        host.nodes[0]["device_identity"]["type"] = 123
    accepted = create(core, entry)
    result = wait(core, accepted["project_id"], accepted)
    assert result["state"] == ("COMPLETED" if original_passed else "FAILED")
    host.binding = "new-ide-binding"
    core._hosts = lambda: [dict(host.records()[0], host_instance_id="new-ide-instance")]
    discovered = core.ino_list_projects()["projects"][0]
    assert discovered["project_id"] != accepted["project_id"]
    assert discovered["template"]["template_id"] == entry["template_id"]
    if original_passed:
        core._writable(discovered["project_id"])
        assert core._project(discovered["project_id"])["template_verification"]["status"] == "passed"
    else:
        with pytest.raises(DomainError) as caught:
            core._writable(discovered["project_id"])
        assert caught.value.code == "CONTROLLER_NOT_VERIFIED"
    assert host.write_count == 0


def test_unknown_origin_remains_blocked_after_new_ide_instance(templates):
    core, host, entry, source, catalog = templates
    host.lose_reply = True
    accepted = create(core, entry)
    assert wait(core, accepted["project_id"], accepted)["state"] == "OUTCOME_UNKNOWN"
    host.binding = "new-ide-binding"
    core._hosts = lambda: [dict(host.records()[0], host_instance_id="new-ide-instance")]
    discovered = core.ino_list_projects()["projects"][0]
    assert discovered["template_origin_project_id"] == accepted["project_id"]
    with pytest.raises(DomainError) as caught:
        core._writable(discovered["project_id"])
    assert caught.value.code == "NEEDS_RECOVERY"
    assert host.open_count == 1 and host.write_count == 0


def test_catalog_content_and_selection_hashes_prevent_stale_creation(templates):
    core, host, entry, source, catalog = templates
    selected = core.ino_list_project_templates()["templates"][0]
    entry["model"] = "changed specification"
    catalog.write_text(json.dumps({"schema_version": "1.0", "templates": [entry]}), encoding="utf-8")
    with pytest.raises(DomainError) as caught:
        core.ino_create_project(entry["template_id"], selected["template_hash"], str(core.project_root / "stale.project"), "host", "instance", "stale")
    assert caught.value.code == "STALE_TEMPLATE"
    source.write_bytes(b"changed bytes")
    assert core.ino_list_project_templates()["templates"][0]["status"] == "hash_mismatch"
    with pytest.raises(DomainError): create(core, entry)
    source.unlink()
    assert core.ino_list_project_templates()["templates"][0]["status"] == "missing"
    assert host.open_count == 0


@pytest.mark.parametrize("failure", ["escape", "duplicate", "invalid_identity", "absolute"])
def test_catalog_rejects_unsafe_or_ambiguous_entries(templates, failure):
    core, host, entry, source, catalog = templates
    entries = [entry]
    if failure == "escape": entry["project_file"] = "../outside.project"
    elif failure == "absolute": entry["project_file"] = str(source)
    elif failure == "duplicate": entries.append(dict(entry))
    else: entry["controller"] = {"type": "4102", "id": "x", "version": "v"}
    catalog.write_text(json.dumps({"schema_version": "1.0", "templates": entries}), encoding="utf-8")
    with pytest.raises(DomainError) as caught:
        core.ino_list_project_templates()
    assert caught.value.code == "TEMPLATE_CATALOG_INVALID" and host.open_count == 0


def test_subdevices_are_not_mistaken_for_root_controller(templates):
    core, host, entry, source, catalog = templates
    host.nodes.append({"id": "ethercat", "kind": "device", "parent_id": "controller", "device_identity": {"status": "unavailable"}})
    assert verify_controller(host.snapshot(), CONTROLLER)["status"] == "passed"
    capture = host.snapshot()
    capture["issues"] = [{"field": "children", "error": "synthetic unreadable tree"}]
    assert verify_controller(capture, CONTROLLER)["status"] == "unknown"


def test_missing_catalog_is_explicit(tmp_path):
    assert TemplateCatalog(tmp_path).list() == {"templates": [], "catalog_status": "not_configured"}
