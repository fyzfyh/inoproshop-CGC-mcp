"""Bounded model generation over 24 synthetic tasks, using the real OMP SDK.

No native host operations are performed by this runner. Candidate compile
results must be supplied by the separate native fixture evaluator. A repair
invocation requires those diagnostics and is hard limited to rounds 1 and 2.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import copy
from datetime import datetime, timezone
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time


def write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_candidate(text, task):
    value = text.strip()
    if value.startswith("```json") and value.endswith("```"):
        value = value[7:-3].strip()
    elif value.startswith("```") and value.endswith("```"):
        value = value[3:-3].strip()
    candidate = json.loads(value)
    required = {"task_id", "status", "answer", "citations", "implementation", "creation", "proposed_tools", "limitations"}
    if not isinstance(candidate, dict) or set(candidate) != required:
        raise ValueError("Candidate fields do not match the response contract")
    if candidate["task_id"] != task["id"] or candidate["status"] not in {"answered", "candidate", "refused"}:
        raise ValueError("Candidate identity/status mismatch")
    if not isinstance(candidate["answer"], str) or not isinstance(candidate["citations"], list):
        raise ValueError("Candidate answer/citations have wrong types")
    if not all(isinstance(item, str) for item in candidate["proposed_tools"] + candidate["limitations"]):
        raise ValueError("Candidate list fields have wrong types")
    if task["category"] == "create":
        from inoproshop_mcp.server import CreateST
        CreateST.model_validate(candidate["creation"])
        if candidate["status"] != "candidate" or candidate["implementation"] is not None:
            raise ValueError("Create task did not return an isolated typed creation")
    elif task["mutation_allowed"]:
        if candidate["status"] != "candidate" or not isinstance(candidate["implementation"], str):
            raise ValueError("Mutation task did not return an ST implementation candidate")
    elif candidate["implementation"] is not None:
        raise ValueError("Read-only task returned a source modification")
    return candidate


def synthetic_diagnostics(diagnostics, fixture, previous):
    """Whitelist source-bound diagnostics; never forward an entire report.

    The native driver maps only known synthetic fixture GUIDs. Compiler
    reports may contain a whole OEM-template snapshot, so all nested source,
    paths, snapshots and arbitrary error-details are deliberately discarded.
    """
    allowed = {node["id"] for node in fixture["nodes"]}
    creation = (previous.get("candidate") or {}).get("creation")
    if creation:
        allowed.add("candidate-created-pou")
    mapping = diagnostics.get("synthetic_object_map", {})
    if not isinstance(mapping, dict) or any(value not in allowed for value in mapping.values()):
        raise ValueError("Native GUID mapping is not bound to synthetic fixture objects")
    sanitized = []
    for item in diagnostics.get("native_compile_evidence", []):
        messages = item.get("messages", [item]) if isinstance(item, dict) else []
        for message in messages:
            if not isinstance(message, dict):
                continue
            object_id = message.get("object_id") or message.get("location", {}).get("object_id") or message.get("location", {}).get("node_id")
            synthetic_id = object_id if object_id in allowed else mapping.get(object_id)
            if synthetic_id not in allowed:
                continue
            fields = {key: message[key] for key in ("severity", "number", "message", "text", "position_text") if key in message and isinstance(message[key], (str, int, float, bool, type(None)))}
            location = message.get("location", {})
            if isinstance(location, dict):
                fields["location"] = {key: location[key] for key in ("section", "line", "column") if key in location and isinstance(location[key], (str, int, type(None)))}
            fields["object_id"] = synthetic_id
            sanitized.append(fields)
    if not sanitized:
        raise ValueError("No compiler diagnostics are bound to the synthetic fixture; refusing to send the full report")
    return sanitized


def main():
    parser = argparse.ArgumentParser()
    root = Path(__file__).resolve().parents[1]
    parser.add_argument("--fixtures", type=Path, default=root / "fixtures/evaluation")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="zhipu-coding-plan/glm-5.3-flash")
    parser.add_argument("--tasks", help="Comma-separated fixed task IDs; omitted runs all fixed tasks")
    parser.add_argument("--workers", type=int, choices=[1, 2, 3], default=2)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--repair-round", type=int, choices=[0, 1, 2], default=0)
    parser.add_argument("--diagnostics-dir", type=Path)
    parser.add_argument("--prepare-contract-feedback", action="store_true", help="Write deterministic schema rejection feedback for the selected repair round; do not call a model")
    args = parser.parse_args()
    if not 15 <= args.timeout_seconds <= 600:
        parser.error("Timeout must be 15..600 seconds")
    if args.repair_round and (not args.diagnostics_dir or not args.tasks):
        parser.error("Repair requires --diagnostics-dir and explicit --tasks")
    source_bytes = (args.fixtures / "sources.json").read_bytes()
    task_bytes = (args.fixtures / "tasks.json").read_bytes()
    source, spec = json.loads(source_bytes), json.loads(task_bytes)
    if spec["data_policy"] != "synthetic_inputs_only" or source["origin"] != "entirely_synthetic_no_customer_source":
        raise ValueError("Synthetic-only fixture policy failed")
    fixture_hash = hashlib.sha256(source_bytes + b"\0" + task_bytes).hexdigest()
    task_ids = set(args.tasks.split(",")) if args.tasks else {t["id"] for t in spec["tasks"]}
    if task_ids - {t["id"] for t in spec["tasks"]}:
        raise ValueError("Unknown fixed task IDs")
    tasks = [t for t in spec["tasks"] if t["id"] in task_ids]
    args.output.mkdir(parents=True, exist_ok=True)
    if args.prepare_contract_feedback:
        if not args.repair_round:
            parser.error("Contract feedback must target repair round 1 or 2")
        for task in tasks:
            previous_dir = args.output / task["id"] / f"round_{args.repair_round - 1}"
            previous = json.loads((previous_dir / "result.json").read_text(encoding="utf-8"))
            if previous["status"] != "invalid_model_output":
                raise ValueError("Contract feedback requires an actual rejected model output")
            response_path = previous_dir / "response.json"
            response = json.loads(response_path.read_text(encoding="utf-8"))
            try:
                parse_candidate(response["response_text"], task)
            except (ValueError, TypeError, KeyError) as exc:
                write_json(args.diagnostics_dir / f'{task["id"]}.json', {"kind": "output_contract", "task_id": task["id"], "fixture_sha256": fixture_hash, "round": args.repair_round - 1, "response_sha256": hashlib.sha256(response_path.read_bytes()).hexdigest(), "errors": [str(exc)], "required": "编辑/修复/创建题status必须为candidate；只提出候选，不声称已经写入。创建题完整填写creation并令implementation=null；其他编辑题creation=null。保留所有必填字段与正确源码。"})
            else:
                raise ValueError("The model output is valid; contract repair is not justified")
        print(json.dumps({"status": "feedback_prepared", "tasks": len(tasks), "model_invoked": False}))
        return

    def run_task(task):
        task_dir = args.output / task["id"]
        task_dir.mkdir(exist_ok=True)
        round_dir = task_dir / f"round_{args.repair_round}"
        workspace = round_dir / "isolated_workspace"
        snapshot = copy.deepcopy(source)
        for node in snapshot["nodes"]:
            node.update(task.get("overrides", {}).get(node["id"], {}))
        prompt = {"task_id": task["id"], "request": task["request"], "target_object_id": task["target"], "mutation_allowed": task["mutation_allowed"], "snapshot": snapshot, "data_scope": "synthetic_inputs_only"}
        diagnostics = None
        if args.repair_round:
            if not task["mutation_allowed"]:
                raise ValueError("Only mutation tasks can enter repair rounds")
            previous_dir = task_dir / f"round_{args.repair_round - 1}"
            previous = json.loads((previous_dir / "result.json").read_text(encoding="utf-8"))
            diagnostics = json.loads((args.diagnostics_dir / f'{task["id"]}.json').read_text(encoding="utf-8-sig"))
            if diagnostics.get("fixture_sha256") != fixture_hash or diagnostics.get("task_id") != task["id"]:
                raise ValueError("Native diagnostics do not match this fixed task fixture")
            if diagnostics.get("round") != args.repair_round - 1:
                raise ValueError("Native diagnostics belong to a different candidate round")
            if diagnostics.get("kind") == "output_contract":
                response_path = previous_dir / "response.json"
                if previous["status"] != "invalid_model_output" or diagnostics.get("response_sha256") != hashlib.sha256(response_path.read_bytes()).hexdigest():
                    raise ValueError("Contract feedback does not match the rejected response")
                prompt["previous_response"] = json.loads(response_path.read_text(encoding="utf-8"))["response_text"]
                prompt["contract_errors"] = diagnostics["errors"]
                prompt["response_requirement"] = diagnostics["required"]
            else:
                if diagnostics.get("compile_status") != "failed" or not diagnostics.get("native_compile_evidence"):
                    raise ValueError("Repair requires an actual native compiler rejection")
                prompt["previous_candidate"] = previous.get("candidate")
                prompt["native_diagnostics"] = synthetic_diagnostics(diagnostics, snapshot, previous)
            prompt["remaining_repairs"] = 3 - args.repair_round
        round_dir.mkdir(exist_ok=False)  # Never overwrite or silently repeat a generation.
        workspace.mkdir()
        payload = {"task_id": task["id"], "model": args.model, "workspace": str(workspace.resolve()), "timeout_seconds": args.timeout_seconds, "data_scope": "synthetic_inputs_only", "prompt": json.dumps(prompt, ensure_ascii=False)}
        write_json(round_dir / "input.json", payload)
        output = round_dir / "response.json"
        started = time.monotonic()
        env = {key: value for key, value in os.environ.items() if key not in {"INOAI_MCP_AUTHORIZATION", "NEO4J_PASSWORD", "INO_MCP_TOKEN"}}
        command = ["bun", str(root / "scripts/omp-model-worker.ts"), "--input", str((round_dir / "input.json").resolve()), "--output", str(output.resolve())]
        try:
            completed = subprocess.run(command, cwd=workspace, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=args.timeout_seconds + 20)
            # Only a worker-authored, known-field report is persisted. stderr
            # might contain provider headers, credentials or unrelated logs.
            response = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {"status": "worker_failed", "model_invoked": False}
            if not isinstance(response, dict) or not isinstance(response.get("status"), str):
                raise ValueError("Worker report is not a structured status")
            exit_code = completed.returncode
        except subprocess.TimeoutExpired:
            response = {"status": "timeout", "model_invoked": None}
            exit_code = None
        except (OSError, ValueError, TypeError) as exc:
            response = {"status": "worker_failed", "model_invoked": None, "error_type": type(exc).__name__}
            exit_code = None
        record = {"schema_version": "1.0", "task_id": task["id"], "fixture_sha256": fixture_hash, "data_scope": "synthetic_inputs_only", "model": response.get("model", args.model), "omp_version": response.get("omp_version"), "model_invoked": response.get("model_invoked"), "round": args.repair_round, "max_repair_rounds": 2, "status": response["status"], "tool_calls": response.get("tool_calls"), "elapsed_seconds": time.monotonic() - started, "usage": response.get("usage"), "exit_code": exit_code, "native_compile_evidence": [], "first_compile_pass": None, "completed_within_two_repairs": None, "manual_changed_lines": None, "compile_status": "not_run_native_fixture_pending", "operation_ids": [], "assertions": []}
        if response.get("status") == "response_received":
            try:
                candidate = parse_candidate(response["response_text"], task)
                record.update(status="candidate_validated" if task["mutation_allowed"] else "answer_requires_review", candidate=candidate)
                record["assertions"].append({"name": "response_schema", "passed": True})
                if task["mutation_allowed"]:
                    original = next(n["implementation"] for n in snapshot["nodes"] if n["id"] == task["target"])
                    generated = candidate["creation"]["implementation"] if task["category"] == "create" else candidate["implementation"]
                    difference = "".join(difflib.unified_diff(("" if task["category"] == "create" else original).splitlines(True), generated.splitlines(True), fromfile="synthetic_baseline", tofile=f"candidate_round_{args.repair_round}"))
                    (round_dir / "candidate.diff").write_text(difference, encoding="utf-8")
                    for expected in ([task["expected"].get("implementation_line")] if task["expected"].get("implementation_line") else []):
                        record["assertions"].append({"name": "gold_line_present", "passed": expected in generated, "kind": "text_only_not_compile"})
                if diagnostics:
                    record["repair_input_diagnostics"] = {"kind": diagnostics.get("kind", "native_compile"), "errors": prompt.get("contract_errors"), "native_diagnostics": prompt.get("native_diagnostics"), "round": diagnostics["round"]}
            except (ValueError, TypeError, KeyError) as exc:
                record.update(status="invalid_model_output", exit_reason="candidate_contract_rejected", contract_error=str(exc))
        record["worker_error_type"] = response.get("error_type")
        record["worker_stop_reason"] = response.get("stop_reason")
        record["exit_reason"] = record.get("exit_reason") or (
            "await_native_fixture_compile" if record["status"] == "candidate_validated" else
            "generation_complete_no_tools" if record["status"] == "answer_requires_review" else
            "model_worker_" + record["status"])
        write_json(round_dir / "result.json", record)
        write_json(task_dir / "latest.json", record)
        print(json.dumps({"task_id": task["id"], "status": record["status"], "round": args.repair_round, "seconds": round(record["elapsed_seconds"], 2)}), flush=True)
        return record

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        records = list(executor.map(run_task, tasks))
    summary = {"at": datetime.now(timezone.utc).isoformat(), "fixture_sha256": fixture_hash, "round": args.repair_round, "tasks": len(records), "model_invoked_tasks": sum(r["model_invoked"] is True for r in records), "valid_response_tasks": sum(r["status"] in {"candidate_validated", "answer_requires_review"} for r in records), "native_compile_tasks": 0, "first_compile_pass_rate": None, "two_repair_completion_rate": None, "data_scope": "synthetic_inputs_only", "tool_calls": sum(r.get("tool_calls") or 0 for r in records), "records": [{"task_id": r["task_id"], "status": r["status"]} for r in records], "limits": {"max_repairs": 2, "native_execution": "separate_driver_required"}}
    write_json(args.output / f"summary_round_{args.repair_round}.json", summary)
    print(json.dumps({"output": str(args.output), "tasks": len(records), "valid_responses": summary["valid_response_tasks"]}))


if __name__ == "__main__":
    main()
