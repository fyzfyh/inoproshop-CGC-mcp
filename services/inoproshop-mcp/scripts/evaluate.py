"""Validate synthetic evaluation fixtures and summarize real recorded results.

This command does not invoke a model or manufacture compiler evidence.
"""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path


def elapsed(start, finish):
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) for value in (start, finish)):
        return None
    return finish - start if finish >= start else None


def timing_summary(values):
    known = [value for value in values if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0]
    return {"recorded_count": len(values), "measured_count": len(known),
            "total_seconds": sum(known) if known else None,
            "mean_seconds": sum(known) / len(known) if known else None,
            "min_seconds": min(known) if known else None, "max_seconds": max(known) if known else None}


def candidate_source_proof(candidate, case):
    """Compare actual compiler snapshot bytes with the model's candidate source.

    The local whole snapshot is read only here; output contains hashes, not any
    OEM source. Missing, ambiguous, or changed evidence never becomes zero edits.
    """
    change = candidate.get("candidate") or {}
    creation = change.get("creation")
    role = "candidate-created-pou" if creation else "program-main"
    expected = {key: creation.get(key) for key in ("declaration", "implementation")} if creation else {"implementation": change.get("implementation")}
    mapping = case.get("synthetic_object_map") or {}
    nodes = (case.get("native_compile_evidence", {}).get("source_snapshot") or {}).get("nodes", [])
    matches = [node for node in nodes if mapping.get(node.get("id")) == role]
    proof = {"comparison": "exact_utf8_source_bytes_without_normalization", "object_role": role, "fields": {},
             "manual_candidate_changed_lines": None, "status": "unproven"}
    if len(matches) != 1 or not all(isinstance(value, str) for value in expected.values()):
        return proof
    node = matches[0]
    for section, source in expected.items():
        native_source = node.get(section)
        proof["fields"][section] = {"model_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            "native_sha256": hashlib.sha256(native_source.encode("utf-8")).hexdigest() if isinstance(native_source, str) else None,
            "exact_equal": source == native_source}
    exact = all(row["exact_equal"] for row in proof["fields"].values()) and (not creation or node.get("name") == creation.get("name"))
    proof["status"] = "exact_match" if exact else "different_or_incomplete"
    proof["manual_candidate_changed_lines"] = 0 if exact else None
    return proof


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=Path, default=Path(__file__).resolve().parents[1] / "fixtures/evaluation")
    parser.add_argument("--results", type=Path)
    parser.add_argument("--native-evidence", type=Path, help="Actual native_model stage summary.json; model results remain immutable")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    source_bytes = (args.fixtures / "sources.json").read_bytes()
    task_bytes = (args.fixtures / "tasks.json").read_bytes()
    sources, spec = json.loads(source_bytes), json.loads(task_bytes)
    tasks = spec["tasks"]
    assert len(tasks) >= 20 and len({task["id"] for task in tasks}) == len(tasks)
    assert spec["data_policy"] == "synthetic_inputs_only" and spec["max_repair_rounds"] == 2
    assert sources["origin"] == "entirely_synthetic_no_customer_source"
    node_ids = {node["id"] for node in sources["nodes"]}
    for task in tasks:
        assert task["target"] in node_ids
        assert task["request"] and task["expected"]
        assert set(task.get("overrides", {})) <= node_ids
    fixture_hash = hashlib.sha256(source_bytes + b"\0" + task_bytes).hexdigest()
    records = []
    if args.results:
        paths = sorted(args.results.glob("E*/latest.json")) or sorted(args.results.glob("*.json"))
        for path in paths:
            result = json.loads(path.read_text(encoding="utf-8-sig"))
            if result.get("task_id") not in {task["id"] for task in tasks}:
                continue
            if result.get("fixture_sha256") != fixture_hash:
                raise ValueError("Result fixture hash does not match the fixed task set")
            if result.get("data_scope") != "synthetic_inputs_only":
                raise ValueError("Result does not declare synthetic-only data scope")
            if result.get("first_compile_pass") is not None and not result.get("native_compile_evidence"):
                raise ValueError("Compile metric lacks native compiler evidence")
            records.append(result)
    first_responses = []
    round_timings = []
    if args.results:
        for path in sorted(args.results.glob("E*/round_*/result.json")):
            result = json.loads(path.read_text(encoding="utf-8-sig"))
            if result.get("fixture_sha256") == fixture_hash and result.get("model_invoked") is True:
                if result.get("round") == 0:
                    first_responses.append(result)
                round_timings.append({"task_id": result["task_id"], "round": result["round"],
                    "status": result["status"], "elapsed_seconds": result.get("elapsed_seconds"),
                    "result_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    accepted = {"candidate_validated", "answer_requires_review"}
    eligible = [r for r in records if r.get("model_invoked") and r.get("first_compile_pass") is not None]
    completion_eligible = [r for r in records if r.get("model_invoked") and r.get("completed_within_two_repairs") is not None]
    native_case_ids = []
    native_timings, manual_changes = [], []
    stage_wall_seconds = None
    live_session = None
    if args.native_evidence:
        if not args.results:
            raise ValueError("Native evidence requires the original model results directory")
        stage = json.loads(args.native_evidence.read_text(encoding="utf-8-sig"))
        if stage.get("fixture_sha256") != fixture_hash or stage.get("pre_generated_real_model_candidates") is not True:
            raise ValueError("Native stage does not bind the fixed synthetic model fixture")
        stage_wall_seconds = elapsed(stage.get("started_at"), stage.get("finished_at"))
        live = stage.get("live_omp")
        if isinstance(live, dict):
            try:
                live_seconds = elapsed(datetime.fromisoformat(live["started_at"]).timestamp(), datetime.fromisoformat(live["finished_at"]).timestamp())
            except (KeyError, TypeError, ValueError):
                live_seconds = None
            live_session = {"status": live.get("status"), "model_invoked": live.get("model_invoked"),
                "model": live.get("model"), "wall_seconds": live_seconds,
                "tool_call_count": len(live.get("tool_calls", [])),
                "tool_names": [row.get("tool", row.get("name")) for row in live.get("tool_calls", [])],
                "recovery_state": (live.get("recovery") or {}).get("state"),
                "scope": "Separate live E07 tool session, excluded from no-tool generation timing and not an additional evaluation task"}
        eligible, completion_eligible = [], []
        seen = set()
        for case in stage.get("cases", []):
            task_id = case["task_id"]
            if task_id in seen or task_id not in {task["id"] for task in tasks}:
                raise ValueError("Native stage contains duplicate or unknown tasks")
            seen.add(task_id)
            candidate_path = args.results / task_id / "latest.json"
            candidate_bytes = candidate_path.read_bytes()
            candidate = json.loads(candidate_bytes)
            if hashlib.sha256(candidate_bytes).hexdigest() != case.get("candidate_result_sha256") or candidate.get("fixture_sha256") != fixture_hash or candidate.get("model_invoked") is not True or candidate.get("data_scope") != "synthetic_inputs_only":
                raise ValueError("Native evidence is not bound to the recorded actual model candidate")
            build = case.get("native_compile_evidence") or {}
            if build.get("status") not in {"passed", "failed"}:
                continue
            if not case.get("operation_id") or not {"ino_preview_change", "ino_apply_change", "ino_get_diagnostics"} <= set(case.get("public_mcp_chain", [])):
                raise ValueError("Native evidence lacks the public MCP candidate chain")
            native_case_ids.append(task_id)
            eligible.append({"first_compile_pass": case.get("first_compile_pass") is True})
            completion_eligible.append({"completed_within_two_repairs": case.get("status") == "passed" and case.get("restore_state") == "RESTORED" and candidate.get("round", 99) <= 2 and case.get("task_intent_check", {}).get("status") == "passed"})
            attempts = case.get("attempts") or [case]
            for attempt_index, attempt in enumerate(attempts):
                candidate_build = attempt.get("native_compile_evidence") or {}
                baseline_build = attempt.get("baseline_compile_evidence") or {}
                native_timings.append({"task_id": task_id, "attempt": attempt_index, "model_round": attempt.get("model_candidate_round"),
                    "candidate_compile_seconds": elapsed(candidate_build.get("started_at"), candidate_build.get("finished_at")),
                    "baseline_compile_seconds": elapsed(baseline_build.get("started_at"), baseline_build.get("finished_at")),
                    "candidate_compile_status": candidate_build.get("status")})
            proof = candidate_source_proof(candidate, case)
            completed = completion_eligible[-1]["completed_within_two_repairs"]
            manual_changes.append(dict(proof, task_id=task_id, task_completed=completed,
                business_completion_manual_changed_lines=0 if completed and proof["manual_candidate_changed_lines"] == 0 else None))
    report = {
        "fixture_status": "validated",
        "fixture_sha256": fixture_hash,
        "tasks": len(tasks),
        "categories": dict(Counter(t["category"] for t in tasks)),
        "model_invoked_by_this_command": False,
        "recorded_results": len(records),
        "result_states": dict(Counter(r.get("status", "unknown") for r in records)),
        "first_contract_response_denominator": len(first_responses),
        "first_contract_response_rate": sum(r["status"] in accepted for r in first_responses) / len(first_responses) if first_responses else None,
        "contract_within_two_repairs_denominator": len(records),
        "contract_within_two_repairs_rate": sum(r["status"] in accepted and r.get("round", 0) <= 2 for r in records) / len(records) if records else None,
        "native_compile_metric_denominator": len(eligible),
        "native_compile_task_ids": native_case_ids,
        "native_evidence": str(args.native_evidence) if args.native_evidence else None,
        "first_compile_pass_rate": sum(r["first_compile_pass"] is True for r in eligible) / len(eligible) if eligible else None,
        "two_repair_completion_denominator": len(completion_eligible),
        "two_repair_completion_rate": sum(r["completed_within_two_repairs"] is True for r in completion_eligible) / len(completion_eligible) if completion_eligible else None,
        "model_timing": {"measurement": "per-worker process wall time, including SDK startup and model response; sums are cumulative worker time, not parallel run makespan",
            "rounds": round_timings, "all_rounds": timing_summary([row["elapsed_seconds"] for row in round_timings]),
            "by_task": {task_id: timing_summary([row["elapsed_seconds"] for row in round_timings if row["task_id"] == task_id]) for task_id in sorted({row["task_id"] for row in round_timings})}},
        "native_timing": {"measurement": "native compiler finished_at minus started_at; stage wall includes fixture setup, candidate workflows, live OMP session and cleanup",
            "attempts": native_timings, "candidate_builds": timing_summary([row["candidate_compile_seconds"] for row in native_timings]),
            "baseline_builds": timing_summary([row["baseline_compile_seconds"] for row in native_timings]), "stage_wall_seconds": stage_wall_seconds},
        "manual_candidate_edits": {"cases": manual_changes,
            "zero_edit_proven_cases": sum(row["manual_candidate_changed_lines"] == 0 for row in manual_changes),
            "unproven_cases": sum(row["manual_candidate_changed_lines"] is None for row in manual_changes),
            "scope": "Model candidate implementation; for new FBs, declaration and implementation. Caller GUID replacement and deterministic call insertion are host adaptation, not manual candidate correction.",
            "fixture_adaptation_excluded": "PROGRAM PLC_PRG -> PROGRAM POU1 and inert b:BYTE host I/O placeholder; original OEM scaffold stays local.",
            "unfinished_task_business_correction": "null: semantic failure is not repaired by a compiler pass; no human correction effort was measured"},
        "live_model_session": live_session,
        "limitations": ["Fixture validation is not model execution, native compile, simulation, or device-runtime proof."]
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
