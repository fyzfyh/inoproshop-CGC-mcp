"""A01: execute the real adapter's version gate without an IDE or PLC.

Only project/system discovery and the captured software identity are fixtures.
Evidence loading, bridge source hashing, binding checks and capability decisions
are the production implementation. These are software tests, not native proof.
"""
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "capability_gate_adapter", ROOT / "adapters/inoproshop/bridge/adapter.py"
)
ADAPTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ADAPTER)

NATIVE_CAPABILITIES = {
    "snapshot", "compile", "replace_text", "import_st", "create_st_pou",
    "delete_object", "save_copy", "open_project", "two_object_recovery",
}


@pytest.fixture
def adapter_gate(tmp_path):
    project = SimpleNamespace(path=str(tmp_path / "synthetic.project"), primary=True)
    software = {"product": "Synthetic OEM", "version": "1.0", "profile": "Synthetic"}
    captures = []

    def capture(current):
        assert current is project
        captures.append(current)
        return {"software": copy.deepcopy(software)}

    evidence_path = tmp_path / "verification.json"
    adapter = ADAPTER.NativeAdapter(
        SimpleNamespace(primary=project), object(),
        {"native_evidence": str(evidence_path), "project_root": str(tmp_path)},
        capture, None,
    )
    evidence = {
        "status": "passed", "bridge_version": copy.deepcopy(adapter.version),
        "software": copy.deepcopy(software),
        "checks": {name: {"status": "passed"} for name in NATIVE_CAPABILITIES},
    }
    return adapter, evidence_path, evidence, captures


def capabilities(adapter_gate, evidence):
    adapter, evidence_path, _, captures = adapter_gate
    if evidence is not None:
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    result = adapter.capabilities({"expected_binding_id": adapter.binding_id})
    assert len(captures) == 1
    assert result["binding_id"] == adapter.binding_id
    assert result["capabilities"]["graphical_readback"]["status"] == "blocked"
    assert result["capabilities"]["graphical_readback"]["reason"]
    return result["capabilities"]


def test_matching_oem_and_bridge_evidence_supports_only_verified_capabilities(adapter_gate):
    adapter, _, evidence, _ = adapter_gate
    assert set(adapter.version["sources"]) == {"adapter.py", "entry.py"}
    result = capabilities(adapter_gate, evidence)
    assert {name for name, value in result.items() if value["status"] == "supported"} == NATIVE_CAPABILITIES
    assert all(result[name]["reason"] for name in NATIVE_CAPABILITIES)


@pytest.mark.parametrize("failure", [
    "oem_version", "bridge_hash", "missing_file", "missing_configuration",
    "malformed_json", "nonobject_json", "nonobject_checks", "nonobject_check",
])
def test_unverified_adapter_evidence_reports_unknown_with_reason(adapter_gate, failure):
    adapter, evidence_path, evidence, _ = adapter_gate
    if failure == "oem_version":
        evidence["software"]["version"] = "another-version"
    elif failure == "bridge_hash":
        evidence["bridge_version"]["sources"]["adapter.py"] = "0" * 64
    elif failure == "missing_file":
        evidence = None
    elif failure == "missing_configuration":
        adapter.config.pop("native_evidence")
        evidence = None
    elif failure == "malformed_json":
        evidence_path.write_text('{"status": "passed",', encoding="utf-8")
        evidence = None
    elif failure == "nonobject_json":
        evidence = ["passed"]
    elif failure == "nonobject_checks":
        evidence["checks"] = None
    elif failure == "nonobject_check":
        evidence["checks"]["create_st_pou"] = "passed"
    result = capabilities(adapter_gate, evidence)
    assert all(result[name]["status"] == "unknown" for name in NATIVE_CAPABILITIES)
    assert all("verification" in result[name]["reason"].lower() for name in NATIVE_CAPABILITIES)


def test_matching_report_does_not_authorize_an_unverified_individual_action(adapter_gate):
    evidence = adapter_gate[2]
    evidence["checks"].pop("delete_object")
    evidence["checks"]["two_object_recovery"] = {"status": "failed"}
    result = capabilities(adapter_gate, evidence)
    for name in ("delete_object", "two_object_recovery"):
        assert result[name]["status"] == "unknown"
        assert result[name]["reason"]
    assert result["create_st_pou"]["status"] == "supported"
