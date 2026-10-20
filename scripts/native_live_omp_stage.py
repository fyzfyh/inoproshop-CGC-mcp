"""One real OMP tool-driven synthetic edit; called only by the native owner."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import uuid

from inoproshop_mcp.common import DomainError, atomic_json

ROOT = Path(__file__).resolve().parents[1]


async def run_live_omp_stage(*, tool, wait, capture, native, native_replace, check,
                             project_id, caller_id, declaration, main, result_root,
                             model="zhipu-coding-plan/glm-5.3-flash"):
    """Caller owns fixture lifetime. Return after public checkpoint recovery.

    Any unknown write raises a DomainError understood by the native owner's
    cleanup guard; never use fixture cleanup to overwrite an unknown operation.
    """
    result_root = Path(result_root) / "live_omp_tools"
    result_root.mkdir(parents=True, exist_ok=False)
    now = next(node for node in native("snapshot")["nodes"] if node["id"] == caller_id)
    native_replace(now, declaration, main["implementation"])
    input_path, output_path = result_root / "input.json", result_root / "report.json"
    workspace = result_root / "empty_model_workspace"
    workspace.mkdir()
    desired = main["implementation"].replace("xRun := xStart AND NOT xStop;", "xRun := xStart AND xPermit AND NOT xStop;")
    if desired == main["implementation"]:
        raise ValueError("Fixed synthetic E07 source not found")
    atomic_json(input_path, {"task_id": "E07-live-tools", "data_scope": "synthetic_inputs_only",
        "project_id": project_id, "object_id": caller_id, "model": model,
        "state_dir": str(ROOT / "work/mcp_p0/state"), "url": "http://127.0.0.1:8765/mcp",
        "workspace": str(workspace), "idempotency_key": "live-omp-" + uuid.uuid4().hex,
        "synthetic_declaration": main["declaration"], "synthetic_implementation": main["implementation"],
        "expected_native_declaration": declaration, "authorized_implementation": desired})
    worker = ROOT / "services/inoproshop-mcp/scripts/omp-native-tool-worker.ts"
    process = await asyncio.create_subprocess_exec("bun", str(worker), "--input", str(input_path), "--output", str(output_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
    timeout = False
    try:
        await asyncio.wait_for(process.communicate(), timeout=300)
    except asyncio.TimeoutError:
        timeout = True
        process.kill()
        await process.wait()
    try:
        evidence = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DomainError("OPERATION_OUTCOME_UNKNOWN", "Live worker report unavailable; inspect the ledger before fixture cleanup.") from exc
    evidence["worker_exit_code"] = process.returncode
    evidence["timed_out"] = timeout
    if evidence.get("write_dispatched") and not evidence.get("operation_id"):
        atomic_json(output_path, evidence)
        raise DomainError("OPERATION_OUTCOME_UNKNOWN", "Live model's native write dispatch has no operation reply; preserve fixture.")
    operation = None
    if evidence.get("operation_id"):
        operation = await wait({"operation_id": evidence["operation_id"], "project_id": project_id})
        if operation["state"] in ("OUTCOME_UNKNOWN", "NEEDS_RECOVERY"):
            raise DomainError("OPERATION_OUTCOME_UNKNOWN", "Live OMP write needs reconciliation; preserve fixture.")
        if operation["state"] == "COMPLETED":
            current = await capture()
            restored = await wait(await tool("ino_restore_checkpoint", project_id=project_id,
                checkpoint_id=operation["checkpoint_id"], expected_snapshot_id=current["snapshot_id"],
                idempotency_key="restore-live-omp-" + uuid.uuid4().hex))
            evidence["recovery"] = {"operation_id": restored["operation_id"], "state": restored["state"]}
            if restored["state"] != "RESTORED":
                atomic_json(output_path, evidence)
                raise DomainError("OPERATION_OUTCOME_UNKNOWN", "Live OMP checkpoint recovery incomplete; preserve fixture.")
        else:
            evidence["recovery"] = {"state": operation["state"]}
    atomic_json(output_path, evidence)
    check("real_omp_model_invoked_projected_mcp_tools", evidence["status"] == "passed" and
          operation is not None and operation["state"] == "COMPLETED" and
          evidence.get("recovery", {}).get("state") == "RESTORED", evidence)
    return evidence
