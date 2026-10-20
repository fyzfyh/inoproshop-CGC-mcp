"""Owned-copy acceptance: native compile coverage and automatic two-object compensation.

Never starts an IDE or sends source to a model. Only fixture setup/cleanup uses
the private adapter; compile and failed creation orchestration use public MCP.
"""
from __future__ import annotations

import copy
import uuid

from inoproshop_mcp.common import DomainError, node_hash


async def run_coverage_stage(tool, wait, apply, capture, native, native_replace, check, project_id, caller_id):
    async def build():
        snap = await capture()
        return await wait(await tool("ino_compile", project_id=project_id,
            expected_snapshot_id=snap["snapshot_id"], idempotency_key="coverage-" + uuid.uuid4().hex))

    def current_caller():
        return next(n for n in native("snapshot")["nodes"] if n["id"] == caller_id)

    def errors(report):
        return [m for m in report.get("messages", []) if m.get("severity", "").lower() in ("error", "fatalerror")]

    baseline = await build()
    check("coverage_passing_baseline", baseline["state"] == "COMPLETED", baseline)
    original = copy.deepcopy(current_caller())
    name = "FB_Coverage_" + uuid.uuid4().hex[:8]
    instance = "coverage_" + uuid.uuid4().hex[:8]
    declaration = "FUNCTION_BLOCK " + name + "\nVAR\nEND_VAR\n"
    broken = "MCP_UNDECLARED_COVERAGE_VARIABLE := 1;\n"
    before = native("snapshot")
    imported = native("import_st", name=name, declaration=declaration, implementation=broken,
        application_id=before["active_application_id"], expected_snapshot_hash=before["content_hash"])
    created_id = imported["object_id"]
    unreferenced = await build()
    unreferenced_report = unreferenced.get("result", {}).get("compile", {})
    check("mcp_unreferenced_bad_fb_not_claimed_covered",
        unreferenced_report.get("status") == "passed" and unreferenced_report.get("target_coverage") == "unknown"
        and imported.get("target_coverage") == "not_covered", {"created_object_id": created_id,
        "operation_id": unreferenced["operation_id"], "compile": unreferenced_report,
        "interpretation": "Application passed; this unreferenced bad FB was not proven compiled."})
    attached_declaration = original["declaration"].rstrip() + "\nVAR\n " + instance + " : " + name + ";\nEND_VAR\n"
    attached_implementation = original["implementation"].rstrip() + "\n" + instance + "();\n"
    native_replace(current_caller(), attached_declaration, attached_implementation)
    referenced = await build()
    referenced_report = referenced.get("result", {}).get("compile", {})
    located = [m for m in errors(referenced_report)
        if (m.get("object_id") or (m.get("location") or {}).get("node_id")) == created_id
        and (m.get("location") or {}).get("status") == "located"]
    check("mcp_referenced_bad_fb_is_compiled_and_located",
        referenced_report.get("status") == "failed" and bool(located),
        {"created_object_id": created_id, "operation_id": referenced["operation_id"], "compile": referenced_report})
    now = current_caller()
    check("coverage_fixture_caller_unmodified_by_others",
        now["declaration"] == attached_declaration and now["implementation"] == attached_implementation)
    native_replace(now, original["declaration"], original["implementation"])
    raw = native("snapshot")
    owned = next(n for n in raw["nodes"] if n["id"] == created_id)
    check("coverage_owned_fb_unchanged", owned["declaration"] == declaration and owned["implementation"] == broken)
    native("delete_object", object_id=created_id, expected_node_hash=node_hash(owned), expected_snapshot_hash=raw["content_hash"])
    restored = await build()
    check("coverage_fixture_restored", restored["state"] == "COMPLETED"
        and node_hash(current_caller()) == node_hash(original), restored)

    # A separate FB is created by Core, fails compilation, and must be restored
    # by Core itself. No private cleanup or manual candidate changes can count.
    name = "FB_AutoRollback_" + uuid.uuid4().hex[:8]
    change = {"type": "create_st_pou", "name": name,
        "declaration": "FUNCTION_BLOCK " + name + "\nVAR\nEND_VAR\n", "implementation": broken,
        "caller": {"object_id": caller_id, "instance_name": "autoRollback_" + uuid.uuid4().hex[:8],
                   "inputs": {}, "outputs": {}, "condition": None,
                   "insert_after_line": len(original["implementation"].splitlines())}}
    captured = await capture()
    indexed = await wait(await tool("context_index_snapshot", project_id=project_id, snapshot_id=captured["snapshot_id"]))
    check("auto_two_object_baseline_index", indexed["state"] == "COMPLETED", indexed)
    patch = await tool("ino_preview_change", project_id=project_id, snapshot_id=captured["snapshot_id"], change=change)
    authorization = dict({key: patch[key] for key in ("project_id", "patch_id", "patch_hash")}, source="user_task")
    key = "auto-rollback-" + uuid.uuid4().hex
    operation = await wait(await tool("ino_apply_change", project_id=project_id, patch_id=patch["patch_id"],
        idempotency_key=key, authorization=authorization))
    result = operation.get("result") or {}
    candidate = result.get("candidate_compile") or {}
    candidate_nodes = (candidate.get("source_snapshot") or {}).get("nodes", [])
    created_nodes = [n for n in candidate_nodes if n.get("name") == name and n.get("implementation") == broken]
    now = native("snapshot")
    check("mcp_create_failure_automatically_restores_both_objects",
        operation["state"] == "RESTORED" and candidate.get("status") == "failed" and len(created_nodes) == 1
        and not any(n["id"] == created_nodes[0]["id"] for n in now["nodes"])
        and node_hash(current_caller()) == node_hash(original), operation)
    diagnoses = await tool("ino_get_diagnostics", project_id=project_id, operation_id=operation["operation_id"], limit=500)
    check("mcp_create_failure_preserves_failed_and_recovery_diagnostics",
        {item.get("status") for item in diagnoses["items"]} >= {"passed", "failed"},
        {"operation_id": operation["operation_id"], "diagnostic_ids": [item.get("diagnostic_id") for item in diagnoses["items"]]})
    again = await tool("ino_apply_change", project_id=project_id, patch_id=patch["patch_id"],
        idempotency_key=key, authorization=authorization)
    check("mcp_failed_create_idempotency_does_not_recreate",
        again["deduplicated"] and again["operation_id"] == operation["operation_id"]
        and native("snapshot")["content_hash"] == now["content_hash"], again)
    return {"status": "passed", "unreferenced_operation_id": unreferenced["operation_id"],
        "referenced_operation_id": referenced["operation_id"], "automatic_recovery_operation_id": operation["operation_id"],
        "created_object_id": created_nodes[0]["id"], "failed_creation_deleted_by_core": True,
        "model_invoked": False, "native_host_started_by_helper": False}
