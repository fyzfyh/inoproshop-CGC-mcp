"""A15 on the real AM402 copy, exclusively through public MCP tools.

Imported by the single native owner; never starts or closes an IDE. No model
receives this business project, its source, or its native diagnostics.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import uuid

ROOT = Path(__file__).resolve().parents[1]


def repair_trigger_outputs(implementation):
    """The two previously documented R_TRIG-instance-to-BOOL errors only."""
    candidate = implementation
    for operator, instance in (("S", "FB_启动按钮Rtrig"), ("R", "FB_停止按钮Rtrig")):
        expression = r"(\b" + operator + r"\s*=\s*" + re.escape(instance) + r")(\s*;)"
        candidate, count = re.subn(expression, r"\1.Q\2", candidate, flags=re.I)
        if count != 1:
            raise ValueError("AM402 source no longer matches the documented two-error fixture: " + operator)
    return candidate


async def run_am402_stage(*, tool, wait, check, host, project_root, run_id, source=None):
    source = Path(source or ROOT / "inoproshop/未命名1/未命名1.project").resolve()
    project_root = Path(project_root).resolve()
    check("am402_source_available", source.is_file(), {"source": str(source), "model_invoked": False})
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()

    async def operation(name, **params):
        return await wait(await tool(name, idempotency_key="am402-" + uuid.uuid4().hex, **params))

    async def open_copy(from_path, name):
        opened = await operation("ino_open_project", source=str(from_path), destination=str(project_root / name),
                                 host_id=host["host_id"], host_instance_id=host["host_instance_id"])
        check("am402_open_" + ("repaired" if "Reopened" in name else "baseline"), opened["state"] == "COMPLETED", opened)
        return opened["project_id"]

    async def read_source(project_id, snapshot_id, object_id, section):
        lines, offset = [], 0
        while offset is not None:
            response = await tool("ino_read_object", project_id=project_id, snapshot_id=snapshot_id,
                                  object_id=object_id, section=section, offset=offset, limit=500)
            data = response["sources"][section]
            if data["status"] != "read":
                raise ValueError("AM402 source section unavailable: " + section)
            lines.extend(data["items"])
            offset = data["next_offset"]
        return "\n".join(lines)

    async def apply_patch(project_id, patch):
        return await operation("ino_apply_change", project_id=project_id, patch_id=patch["patch_id"],
            authorization=dict({key: patch[key] for key in ("project_id", "patch_id", "patch_hash")}, source="user_task"))

    try:
        project_id = await open_copy(source, "AM402_" + run_id + ".project")
        captured = await tool("ino_capture_snapshot", project_id=project_id)
        nodes, offset = [], 0
        while offset is not None:
            tree = await tool("ino_read_object", project_id=project_id, snapshot_id=captured["snapshot_id"], offset=offset, limit=500)
            nodes.extend(tree["items"])
            offset = tree["next_offset"]
        candidates = [node for node in nodes if node["name"] == "PLC_PRG" and node.get("kind") == "pou"
                      and node.get("application_id") == captured["active_application_id"]]
        check("am402_real_project_tree", len(candidates) == 1 and len(nodes) >= 66,
              {"nodes": len(nodes), "target_ids": [node["id"] for node in candidates], "source_sha256": original_hash})
        target_id = candidates[0]["id"]
        declaration = await read_source(project_id, captured["snapshot_id"], target_id, "declaration")
        implementation = await read_source(project_id, captured["snapshot_id"], target_id, "implementation")
        repaired = repair_trigger_outputs(implementation)
        baseline = await operation("ino_compile", project_id=project_id, expected_snapshot_id=captured["snapshot_id"])
        built = (baseline.get("result") or {}).get("compile") or {}
        check("am402_known_bad_baseline", baseline["state"] == "FAILED" and built.get("status") == "failed" and built.get("errors") == 2, baseline)
        indexed = await operation("context_index_snapshot", project_id=project_id, snapshot_id=captured["snapshot_id"])
        check("am402_bad_baseline_index", indexed["state"] == "COMPLETED", indexed)
        change = {"type": "replace_st_implementation", "object_id": target_id, "implementation": repaired}
        strict_preview = await tool("ino_preview_change", project_id=project_id, snapshot_id=captured["snapshot_id"], change=change, mode="strict")
        strict = await apply_patch(project_id, strict_preview)
        unchanged = await tool("ino_capture_snapshot", project_id=project_id)
        check("am402_strict_mode_refuses_without_edit", (strict.get("error") or {}).get("code") == "BASELINE_COMPILE_FAILED" and unchanged["hash"] == captured["hash"], strict)
        preview = await tool("ino_preview_change", project_id=project_id, snapshot_id=captured["snapshot_id"], change=change, mode="repair")
        result = await apply_patch(project_id, preview)
        outcome = result.get("result") or {}
        first_build = outcome.get("candidate_compile") or {}
        if result["state"] == "RESTORED" and first_build.get("status") == "unknown" and first_build.get("native_status") == "passed":
            # AM402 creates an empty persistence object on its first successful
            # build. Core correctly rejected that changed snapshot and restored
            # the candidate. Review the new complete baseline once; never replay
            # the old patch or weaken source-current checking.
            changed = await tool("ino_capture_snapshot", project_id=project_id)
            observed, offset = [], 0
            while offset is not None:
                tree = await tool("ino_read_object", project_id=project_id, snapshot_id=changed["snapshot_id"], offset=offset, limit=500)
                observed.extend(tree["items"])
                offset = tree["next_offset"]
            old_by_id = {node["id"]: node for node in nodes}
            current_by_id = {node["id"]: node for node in observed}
            added = [node for node in observed if node["id"] not in old_by_id]
            safe = all(current_by_id.get(key) == node for key, node in old_by_id.items()) and len(added) == 1
            if safe:
                system_node = added[0]
                safe = system_node["name"] == "PersistentVars" and system_node["parent_id"] == changed["active_application_id"]
                if safe:
                    system_declaration = await read_source(project_id, changed["snapshot_id"], system_node["id"], "declaration")
                    safe = system_declaration.strip() == "{attribute 'qualified_only'}\nVAR_GLOBAL PERSISTENT RETAIN\nEND_VAR"
            check("am402_first_success_added_only_empty_native_persistence", safe,
                  {"first_operation": result, "new_snapshot_id": changed["snapshot_id"], "added_objects": added,
                   "action": "new_preview_of_verified_restored_baseline_once; no_replay"})
            indexed = await operation("context_index_snapshot", project_id=project_id, snapshot_id=changed["snapshot_id"])
            check("am402_reindex_verified_new_baseline", indexed["state"] == "COMPLETED", indexed)
            preview = await tool("ino_preview_change", project_id=project_id, snapshot_id=changed["snapshot_id"], change=change, mode="repair")
            result = await apply_patch(project_id, preview)
            outcome = result.get("result") or {}
        check("am402_repair_compiles", result["state"] == "COMPLETED" and outcome.get("compile_status") == "passed" and outcome.get("readback_status") == "matched", result)
        delta = outcome.get("repair") or {}
        check("am402_repair_error_accounting", len(delta.get("original", [])) == 2 and len(delta.get("target", [])) == 2 and not delta.get("new") and not delta.get("remaining") and delta.get("overall_passed"), delta)
        after = await tool("ino_capture_snapshot", project_id=project_id)
        check("am402_declaration_unchanged", await read_source(project_id, after["snapshot_id"], target_id, "declaration") == declaration)
        check("am402_two_q_outputs_read_back", await read_source(project_id, after["snapshot_id"], target_id, "implementation") == repaired)
        saved_path = project_root / ("AM402_Repaired_" + run_id + ".project")
        saved = await operation("ino_save_copy", project_id=project_id, expected_snapshot_id=after["snapshot_id"], destination=str(saved_path))
        check("am402_save_repaired_copy", saved["state"] == "COMPLETED" and saved_path.is_file(), saved)
        project_id = await open_copy(saved_path, "AM402_Reopened_" + run_id + ".project")
        reopened = await tool("ino_capture_snapshot", project_id=project_id)
        reopened_source = await read_source(project_id, reopened["snapshot_id"], target_id, "implementation")
        reopened_compile = await operation("ino_compile", project_id=project_id, expected_snapshot_id=reopened["snapshot_id"])
        check("am402_save_reopen_compile", reopened_source == repaired and (reopened_compile.get("result") or {}).get("compile_status") == "passed", reopened_compile)
        return project_id
    finally:
        check("am402_original_file_unchanged", hashlib.sha256(source.read_bytes()).hexdigest() == original_hash,
              {"source_sha256": original_hash, "source": str(source), "model_invoked": False})
