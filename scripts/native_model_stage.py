"""Synthetic model candidate -> public MCP -> native compiler acceptance.

Called by the owned-host native runner. Initial candidates are pre-generated;
native rejection can invoke the isolated synthetic-only runner up to round 2.
Only test-fixture setup/cleanup uses the private adapter; candidate preview,
application, compilation, diagnostics, and checkpoint recovery use MCP.
"""
from __future__ import annotations

import copy
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

from inoproshop_mcp.common import atomic_json, node_hash

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "services/inoproshop-mcp/fixtures/evaluation"
MODEL_RUN = Path(os.environ.get("INO_MCP_MODEL_RESULTS", ROOT / "artifacts/mcp_p0/model_evaluation_v1_20261012"))
INTENT_REVIEW_PATH = Path(os.environ.get("INO_MCP_SEMANTIC_REVIEW", ROOT / "artifacts/mcp_p0/model_semantic_review_20261012/review.json"))


async def run_model_stage(tool, wait, apply, capture, native, native_replace, check,
                          project_id, run_id, report, report_path):
    """Use the existing owned IDE and authenticated MCP helpers; start no host.

    Helpers follow native_core_verify.py: tool(name, **args), wait(operation),
    capture(), native(command, **args), native_replace(node, decl, impl), and
    check(name, condition, detail=None). `apply` is accepted for compatibility;
    this stage expands preview/apply itself to preserve the exact patch hash.
    """
    source_bytes = (FIXTURES / "sources.json").read_bytes()
    task_bytes = (FIXTURES / "tasks.json").read_bytes()
    sources, task_spec = json.loads(source_bytes), json.loads(task_bytes)
    fixture_hash = hashlib.sha256(source_bytes + b"\0" + task_bytes).hexdigest()
    by_id = {node["id"]: node for node in sources["nodes"]}
    tasks = {task["id"]: task for task in task_spec["tasks"]}
    review_bytes = INTENT_REVIEW_PATH.read_bytes() if INTENT_REVIEW_PATH.exists() else None
    intent_reviews = {case["task_id"]: case for case in json.loads(review_bytes)["cases"]} if review_bytes else {}
    result_root = Path(report_path).parent / "model_native"
    result_root.mkdir(parents=True, exist_ok=True)
    stage = {"status": "running", "fixture_sha256": fixture_hash,
             "model_results": str(MODEL_RUN), "model_invoked_by_stage": False,
             "pre_generated_real_model_candidates": True, "data_sent_to_model_by_stage": None,
             "cases": [], "setup": {}, "cleanup": {}, "started_at": time.time(),
             "max_model_repair_round": 2, "repair_calls": [],
             "status_dimension": "native_compile_and_recovery_workflow_not_task_semantics"}
    stage["semantic_review"] = {"path": str(INTENT_REVIEW_PATH), "sha256": hashlib.sha256(review_bytes).hexdigest() if review_bytes else None}
    report["model_native"] = stage
    raw = native("snapshot")
    caller = next(node for node in raw["nodes"] if node["name"] == "POU1" and node.get("implementation") is not None)
    original = copy.deepcopy(caller)
    # Preserve the OEM template's fixed byte I/O names with inert synthetic
    # declarations. No original program logic enters a model prompt/feedback.
    secondary = next((node for node in raw["nodes"] if node["name"] == "POU2" and node.get("implementation") is not None), None)
    original_secondary = copy.deepcopy(secondary)
    gvl_source = by_id["gvl-settings"]
    fixture_gvls = [node for node in raw["nodes"] if node["name"] == gvl_source["name"] and isinstance(node.get("declaration"), str)]
    gvl_ready = len(fixture_gvls) == 1 and fixture_gvls[0]["declaration"].replace("\r\n", "\n").strip() == gvl_source["declaration"].replace("\r\n", "\n").strip()
    if not gvl_ready:
        stage.update(status="blocked_fixture_incomplete", exit_reason="FIXTURE_INCOMPLETE: exact synthetic GVL_Settings must be preloaded before testing model candidates")
        atomic_json(report_path, report)
        atomic_json(result_root / "summary.json", stage)
    check("model_complete_synthetic_gvl_present", gvl_ready, {"required": "GVL_Settings", "fixture_only": True})
    check("model_fixture_names_available", not any(node["name"].lower() in {"fb_gate", "fb_permit", "fb_inhibit"} for node in raw["nodes"]))
    gate_id = None
    caller_seeded = False
    secondary_seeded = False
    uncertain_write = False

    async def compile_public():
        snapshot = await capture()
        return await wait(await tool("ino_compile", project_id=project_id,
                                    expected_snapshot_id=snapshot["snapshot_id"],
                                    idempotency_key="model-compile-" + uuid.uuid4().hex))

    def projected_messages(build, mapping):
        # Compile results can contain whole source_snapshot. Only allow mapped
        # synthetic object diagnostics into files eligible for model feedback.
        rows = []
        for message in (build or {}).get("messages", []):
            object_id = message.get("object_id") or (message.get("location") or {}).get("object_id") or (message.get("location") or {}).get("node_id")
            if object_id not in mapping:
                continue
            row = {key: message[key] for key in ("severity", "number", "text", "position_text", "object_id") if key in message and isinstance(message[key], (str, int, float, bool, type(None)))}
            location = message.get("location") or {}
            row["location"] = {key: location[key] for key in ("section", "line", "column") if key in location}
            row["object_id"] = object_id
            rows.append(row)
        return rows

    async def repair_candidate(task_id, previous):
        next_round = previous["round"] + 1
        if next_round > 2:
            raise AssertionError("Hard model repair limit reached")
        runner = ROOT / "services/inoproshop-mcp/scripts/run-model-evaluation.py"
        arguments = [sys.executable, str(runner), "--output", str(MODEL_RUN), "--tasks", task_id,
                     "--model", previous["model"], "--workers", "1", "--repair-round", str(next_round),
                     "--diagnostics-dir", str(MODEL_RUN / "native_feedback")]
        environment = {key: value for key, value in os.environ.items() if key not in {"INOAI_MCP_AUTHORIZATION", "NEO4J_PASSWORD", "INO_MCP_TOKEN"}}
        if previous["status"] == "invalid_model_output":
            prepare = await asyncio.create_subprocess_exec(*arguments, "--prepare-contract-feedback", env=environment, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            await asyncio.wait_for(prepare.communicate(), timeout=30)
            if prepare.returncode:
                raise AssertionError("Contract feedback preparation failed")
        process = await asyncio.create_subprocess_exec(*arguments, env=environment, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            await asyncio.wait_for(process.communicate(), timeout=180)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise
        stage["model_invoked_by_stage"] = True
        stage["data_sent_to_model_by_stage"] = "synthetic_sources_and_guid_whitelisted_diagnostics_only"
        stage["repair_calls"].append({"task_id": task_id, "round": next_round, "exit_code": process.returncode})
        if process.returncode:
            raise AssertionError("Isolated model repair runner failed")
        return json.loads((MODEL_RUN / task_id / "latest.json").read_text(encoding="utf-8"))

    try:
        gate = by_id["fb-gate"]
        imported = native("import_st", name=gate["name"], declaration=gate["declaration"],
                          implementation=gate["implementation"], application_id=raw["active_application_id"],
                          expected_snapshot_hash=raw["content_hash"])
        gate_id = imported["object_id"]
        if secondary:
            native_replace(secondary, "PROGRAM POU2\nVAR\n B : BYTE;\nEND_VAR", "// Synthetic fixture isolation; B only preserves the host's I/O mapping")
            secondary_seeded = True
        main = by_id["program-main"]
        declaration = main["declaration"].replace("PROGRAM PLC_PRG", "PROGRAM POU1", 1).replace("END_VAR", " b : BYTE; // Synthetic placeholder for the host I/O mapping\nEND_VAR", 1)
        caller = next(node for node in native("snapshot")["nodes"] if node["id"] == caller["id"])
        native_replace(caller, declaration, main["implementation"])
        caller_seeded = True
        mapping = {caller["id"]: "program-main", gate_id: "fb-gate", fixture_gvls[0]["id"]: "gvl-settings"}
        stage["synthetic_object_map"] = mapping
        stage["setup"] = {"physical_program": "POU1", "fixture_program": "PLC_PRG",
                          "declaration_remap": "PROGRAM PLC_PRG -> PROGRAM POU1; inert b:BYTE host I/O placeholder",
                          "task_entry_retained": True, "private_setup_only": True,
                          "guid_map": mapping, "unrelated_oem_objects_sent_to_model": False}
        stage["setup"]["unrelated_pou2_locally_isolated"] = secondary_seeded
        baseline = await compile_public()
        stage["setup"]["baseline_operation_id"] = baseline["operation_id"]
        check("model_fixture_native_baseline", baseline["state"] == "COMPLETED" and baseline["result"]["compile_status"] == "passed", baseline)
        public_snapshot = await capture()
        readback = await tool("ino_read_object", project_id=project_id, snapshot_id=public_snapshot["snapshot_id"], object_id=caller["id"], limit=500)
        check("model_fixture_public_source_read", "\n".join(readback["sources"]["implementation"]["items"]) == main["implementation"], {"snapshot_id": public_snapshot["snapshot_id"], "object_id": caller["id"]})

        for task_id in ("E07", "E08", "E09", "E10", "E11", "E12", "E13", "E25", "E26"):
            task = tasks[task_id]
            task_baseline = dict(main, **task.get("overrides", {}).get("program-main", {}))
            now = next(node for node in native("snapshot")["nodes"] if node["id"] == caller["id"])
            native_replace(now, declaration, task_baseline["implementation"])
            mode = task["expected"].get("mode", "strict")
            if task["category"] == "repair":
                mode = "repair"
            result_path = MODEL_RUN / task_id / "latest.json"
            model_result = json.loads(result_path.read_text(encoding="utf-8"))
            reviewed = intent_reviews.get(task_id, {})
            review_matches = reviewed.get("evidence", {}).get("latest_pointer", {}).get("sha256") == hashlib.sha256(result_path.read_bytes()).hexdigest()
            case = {"task_id": task_id, "status": "running", "attempts": [], "mode": mode,
                    "task_override_loaded": bool(task.get("overrides")), "fixture_sha256": fixture_hash,
                    "task_intent_check": {"status": reviewed.get("task_intent", "pending_review") if review_matches else "pending_review",
                                          "reason": reviewed.get("summary") if review_matches else "No independent semantic review binds this candidate hash.",
                                          "basis": "independent_agent_source_review_separate_from_compiler",
                                          "reviewed_model_round": model_result["round"], "review_candidate_hash_matches": review_matches}}
            stage["cases"].append(case)
            for native_attempt in range(3):
                check("model_" + task_id + "_provenance_round_" + str(model_result["round"]),
                      model_result.get("fixture_sha256") == fixture_hash and model_result.get("model_invoked") is True and model_result.get("status") == "candidate_validated" and model_result.get("tool_calls") == 0 and model_result.get("data_scope") == "synthetic_inputs_only" and model_result["round"] <= 2,
                      {"result_path": str(result_path), "model": model_result.get("model"), "round": model_result.get("round")})
                candidate = model_result["candidate"]
                if task["category"] != "create":
                    change = {"type": "replace_st_implementation", "object_id": caller["id"], "implementation": candidate["implementation"]}
                else:
                    change = copy.deepcopy(candidate["creation"])
                    check("model_" + task_id + "_explicit_mapping", change["caller"] == task["expected"]["caller"], change["caller"])
                    change["caller"]["object_id"] = caller["id"]
                captured = await capture()
                indexed = await wait(await tool("context_index_snapshot", project_id=project_id, snapshot_id=captured["snapshot_id"]))
                check("model_" + task_id + "_real_graph", indexed["state"] == "COMPLETED", indexed)
                preview = await tool("ino_preview_change", project_id=project_id, snapshot_id=captured["snapshot_id"], change=change, mode=mode)
                auth = dict({key: preview[key] for key in ("project_id", "patch_id", "patch_hash")}, source="user_task")
                operation = await wait(await tool("ino_apply_change", project_id=project_id, patch_id=preview["patch_id"], idempotency_key="model-apply-" + run_id + "-" + task_id + "-" + str(model_result["round"]), authorization=auth))
                result = operation.get("result") or {}
                compile_report = result.get("compile") or result.get("candidate_compile") or {}
                succeeded = operation["state"] == "COMPLETED" and result.get("compile_status") == "passed"
                uncertain_write = operation["state"] in ("OUTCOME_UNKNOWN", "NEEDS_RECOVERY")
                task_mapping = dict(mapping)
                if result.get("created_object_id"):
                    task_mapping[result["created_object_id"]] = "candidate-created-pou"
                elif task["category"] == "create":
                    # A failed create may already have removed the object.
                    # Inspect the LOCAL compiler snapshot only to recover a
                    # unique exact candidate GUID; never forward that snapshot.
                    def same_source(left, right):
                        return isinstance(left, str) and left.replace("\r\n", "\n").strip() == right.replace("\r\n", "\n").strip()
                    candidates = [node for node in (compile_report.get("source_snapshot") or {}).get("nodes", [])
                                  if node.get("name") == change["name"] and same_source(node.get("declaration"), change["declaration"])
                                  and same_source(node.get("implementation"), change["implementation"])]
                    if len(candidates) == 1:
                        task_mapping[candidates[0]["id"]] = "candidate-created-pou"
                diagnoses = await tool("ino_get_diagnostics", project_id=project_id, operation_id=operation["operation_id"], limit=500)
                attempt = {"task_id": task_id, "status": "passed" if succeeded else "failed", "model": model_result["model"], "omp_version": model_result["omp_version"],
                           "model_candidate_round": model_result["round"], "candidate_result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
                           "candidate_result_path": str(MODEL_RUN / task_id / ("round_" + str(model_result["round"])) / "result.json"), "data_scope": "synthetic_inputs_only",
                           "snapshot_id": captured["snapshot_id"], "patch_id": preview["patch_id"], "patch_hash": preview["patch_hash"],
                           "operation_id": operation["operation_id"], "compile_status": compile_report.get("status"), "operation_state": operation["state"],
                           "native_compile_evidence": compile_report, "baseline_compile_evidence": result.get("baseline_compile"), "diagnostics": diagnoses,
                           "public_mcp_chain": ["ino_capture_snapshot", "context_index_snapshot", "ino_preview_change", "ino_apply_change", "ino_get_operation", "ino_get_diagnostics"],
                           "synthetic_object_map": task_mapping, "fixed_driver_not_model_tool_autonomy": True}
                case["attempts"].append(attempt)
                case.update(attempt)
                case["first_compile_pass"] = case["attempts"][0]["compile_status"] == "passed"
                atomic_json(result_root / (task_id + ".json"), case)
                atomic_json(report_path, report)
                if succeeded:
                    if mode == "repair":
                        check("model_" + task_id + "_actual_bad_baseline", (result.get("baseline_compile") or {}).get("status") == "failed", result.get("repair"))
                    after = await capture()
                    restore = await wait(await tool("ino_restore_checkpoint", project_id=project_id, checkpoint_id=result["checkpoint_id"], expected_snapshot_id=after["snapshot_id"], idempotency_key="model-restore-" + run_id + "-" + task_id))
                    case.update(restore_operation_id=restore["operation_id"], restore_state=restore["state"], exit_reason="native_compile_passed_and_checkpoint_restored")
                    uncertain_write = restore["state"] in ("OUTCOME_UNKNOWN", "NEEDS_RECOVERY")
                    check("model_" + task_id + "_public_checkpoint_restore", restore["state"] == "RESTORED", restore)
                    if task["category"] == "create":
                        check("model_" + task_id + "_created_object_removed", not any(node["name"] == change["name"] for node in native("snapshot")["nodes"]))
                    break
                if uncertain_write or compile_report.get("status") != "failed" or operation["state"] != "RESTORED":
                    case["exit_reason"] = "native_outcome_unavailable_or_recovery_incomplete"
                    break
                if model_result["round"] >= 2:
                    case["exit_reason"] = "hard_repair_limit_reached"
                    break
                feedback = {"kind": "native_compile", "task_id": task_id, "fixture_sha256": fixture_hash,
                            "round": model_result["round"], "compile_status": "failed", "synthetic_object_map": task_mapping,
                            "native_compile_evidence": projected_messages(compile_report, task_mapping)}
                if not feedback["native_compile_evidence"]:
                    case["exit_reason"] = "no_synthetic_bound_diagnostics_refuse_model_feedback"
                    break
                atomic_json(MODEL_RUN / "native_feedback" / (task_id + ".json"), feedback)
                model_result = await repair_candidate(task_id, model_result)
                case["task_intent_check"] = {"status": "pending_review", "reason": "A new compiler-driven candidate requires another independent semantic review."}
                while model_result["status"] == "invalid_model_output" and model_result["round"] < 2:
                    model_result = await repair_candidate(task_id, model_result)
                if model_result["status"] != "candidate_validated":
                    case["exit_reason"] = "repair_output_rejected_at_hard_limit"
                    break
            case["task_completed"] = case["status"] == "passed" and case.get("restore_state") == "RESTORED" and case["task_intent_check"]["status"] == "passed"
            atomic_json(result_root / (task_id + ".json"), case)
            atomic_json(report_path, report)
            check("model_" + task_id + "_native_compile", case["status"] == "passed" and case.get("restore_state") == "RESTORED",
                  {"operation_id": case.get("operation_id"), "state": case.get("operation_state"), "compile_status": case.get("compile_status"), "exit_reason": case.get("exit_reason")})
        from native_live_omp_stage import run_live_omp_stage
        stage["live_omp"] = await run_live_omp_stage(tool=tool, wait=wait, capture=capture, native=native,
                                                    native_replace=native_replace, check=check, project_id=project_id,
                                                    caller_id=caller["id"], declaration=declaration, main=main,
                                                    result_root=result_root)
        stage["status"] = "passed"
        stage["native_compile_tasks"] = len(stage["cases"])
        stage["task_intent_passed"] = sum(case["task_intent_check"]["status"] == "passed" for case in stage["cases"])
        stage["task_completion_is_separate_from_compile"] = True
    except Exception as exc:
        stage["status"] = "failed"
        if getattr(exc, "code", "") in {"OPERATION_OUTCOME_UNKNOWN", "OUTCOME_UNKNOWN", "HOST_UNAVAILABLE", "HOST_DISCONNECTED", "HOST_INSTANCE_MISMATCH", "BINDING_MISMATCH"} or isinstance(exc, (TimeoutError, ConnectionError)):
            uncertain_write = True
        raise
    finally:
        active_error = sys.exc_info()[0] is not None
        cleanup_error = None
        if uncertain_write:
            stage["cleanup"] = {"status": "blocked_unknown_write", "reason": "Preserve host state for reconciliation; do not replay cleanup."}
        else:
            try:
                if caller_seeded:
                    now = next(node for node in native("snapshot")["nodes"] if node["id"] == original["id"])
                    native_replace(now, original["declaration"], original["implementation"])
                if secondary_seeded:
                    now = next(node for node in native("snapshot")["nodes"] if node["id"] == original_secondary["id"])
                    native_replace(now, original_secondary["declaration"], original_secondary["implementation"])
                if gate_id:
                    now = native("snapshot")
                    gate_node = next((node for node in now["nodes"] if node["id"] == gate_id), None)
                    if gate_node:
                        native("delete_object", object_id=gate_id, expected_node_hash=node_hash(gate_node), expected_snapshot_hash=now["content_hash"])
                recovery = await compile_public()
                stage["cleanup"] = {"status": "passed" if recovery["state"] == "COMPLETED" and recovery["result"]["compile_status"] == "passed" else "failed", "operation_id": recovery["operation_id"], "private_fixture_cleanup": True}
                check("model_fixture_cleanup_native_compile", stage["cleanup"]["status"] == "passed", stage["cleanup"])
            except Exception as exc:
                stage["cleanup"] = {"status": "failed", "error_type": type(exc).__name__}
                stage["status"] = "failed"
                cleanup_error = exc
        stage["finished_at"] = time.time()
        atomic_json(report_path, report)
        atomic_json(result_root / "summary.json", stage)
        if cleanup_error is not None and not active_error:
            raise cleanup_error
    from native_coverage_stage import run_coverage_stage
    stage["native_coverage"] = await run_coverage_stage(
        tool=tool, wait=wait, apply=apply, capture=capture, native=native,
        native_replace=native_replace, check=check, project_id=project_id,
        caller_id=caller["id"])
    atomic_json(report_path, report)
    atomic_json(result_root / "summary.json", stage)
    return stage
