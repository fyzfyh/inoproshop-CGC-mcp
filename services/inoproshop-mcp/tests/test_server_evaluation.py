"""Evaluation isolation contracts: no actual model or host calls."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


path = Path(__file__).resolve().parents[1] / "scripts/run-model-evaluation.py"
spec = importlib.util.spec_from_file_location("model_evaluation_runner", path)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_model_repair_drops_oem_snapshot_and_unmapped_diagnostics():
    fixture = {"nodes": [{"id": "program-main"}]}
    diagnostics = {"synthetic_object_map": {"native-main-guid": "program-main"}, "native_compile_evidence": [{"source_snapshot": {"private_customer_code": "NEVER_SEND"}, "messages": [{"object_id": "other-oem-guid", "text": "PRIVATE_OTHER_PROGRAM"}, {"object_id": "native-main-guid", "text": "Undeclared synthetic identifier", "source_snapshot": "NEVER_SEND", "location": {"section": "implementation", "line": 2, "column": 1, "path": "PRIVATE_PATH", "source_line": "PRIVATE_SOURCE"}}]}]}
    result = runner.synthetic_diagnostics(diagnostics, fixture, {})
    assert result == [{"object_id": "program-main", "text": "Undeclared synthetic identifier", "location": {"section": "implementation", "line": 2, "column": 1}}]


def test_model_repair_requires_fixture_guid_mapping():
    with pytest.raises(ValueError, match="No compiler diagnostics"):
        runner.synthetic_diagnostics({"native_compile_evidence": [{"object_id": "unknown-guid", "text": "not synthetic"}]}, {"nodes": [{"id": "program-main"}]}, {})


def test_model_repair_rejects_mapping_to_an_object_outside_fixture():
    with pytest.raises(ValueError, match="not bound"):
        runner.synthetic_diagnostics({"synthetic_object_map": {"guid": "customer-poU"}}, {"nodes": [{"id": "program-main"}]}, {})


@pytest.mark.parametrize("failure", ["network", "context_overflow", "timeout", "malformed_report"])
def test_fake_worker_failure_is_durable_and_never_native_proof(tmp_path, monkeypatch, failure):
    """Deterministic worker boundary failures; no actual model/provider calls."""
    output = tmp_path / failure
    calls = []
    def fake_worker(command, **kwargs):
        calls.append(command)
        destination = Path(command[command.index("--output") + 1])
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 1, output="PRIVATE_PROVIDER_HEADER")
        if failure == "malformed_report":
            destination.write_text("{truncated", encoding="utf-8")
        else:
            destination.write_text(json.dumps({"status": "model_failed", "model_invoked": True,
                "error_type": "ConnectionError" if failure == "network" else "ContextOverflowError",
                "stop_reason": "error", "tool_calls": 0}), encoding="utf-8")
        return SimpleNamespace(returncode=1, stderr="PRIVATE_PROVIDER_HEADER", stdout="")
    monkeypatch.setattr(runner.subprocess, "run", fake_worker)
    monkeypatch.setattr(sys, "argv", [str(path), "--output", str(output), "--tasks", "E07", "--workers", "1"])
    runner.main()
    result_path = output / "E07/round_0/result.json"
    record = json.loads(result_path.read_text(encoding="utf-8"))
    assert record["status"] in {"model_failed", "timeout", "worker_failed"}
    assert record["exit_reason"].startswith("model_worker_")
    assert record["first_compile_pass"] is None and record["native_compile_evidence"] == []
    assert record["compile_status"] == "not_run_native_fixture_pending"
    assert (output / "E07/round_0/input.json").exists()
    assert "PRIVATE_PROVIDER_HEADER" not in result_path.read_text(encoding="utf-8")
    assert len(calls) == 1
    with pytest.raises(FileExistsError):
        runner.main()
    assert len(calls) == 1  # An existing round cannot silently call again.


def test_repair_round_three_rejected_before_any_worker(tmp_path, monkeypatch):
    def forbidden_worker(*args, **kwargs):
        pytest.fail("Round three reached a model worker")
    monkeypatch.setattr(runner.subprocess, "run", forbidden_worker)
    monkeypatch.setattr(sys, "argv", [str(path), "--output", str(tmp_path), "--tasks", "E07", "--repair-round", "3"])
    with pytest.raises(SystemExit) as result:
        runner.main()
    assert result.value.code == 2
