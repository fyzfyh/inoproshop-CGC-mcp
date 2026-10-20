"""Evidence boundaries for reported model edit counts and elapsed time."""
import copy
import importlib.util
from pathlib import Path


path = Path(__file__).resolve().parents[1] / "scripts/evaluate.py"
spec = importlib.util.spec_from_file_location("evaluation_metrics", path)
metrics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metrics)


def test_candidate_zero_edits_requires_exact_native_source():
    candidate = {"candidate": {"implementation": "xRun := xStart;"}}
    case = {"synthetic_object_map": {"one": "program-main"},
            "native_compile_evidence": {"source_snapshot": {"nodes": [{"id": "one", "implementation": "xRun := xStart;"}]}}}
    assert metrics.candidate_source_proof(candidate, case)["manual_candidate_changed_lines"] == 0
    changed = copy.deepcopy(case)
    changed["native_compile_evidence"]["source_snapshot"]["nodes"][0]["implementation"] = "xRun := NOT xStart;"
    assert metrics.candidate_source_proof(candidate, changed)["manual_candidate_changed_lines"] is None
    assert metrics.candidate_source_proof(candidate, {})["manual_candidate_changed_lines"] is None
    ambiguous = copy.deepcopy(case)
    ambiguous["native_compile_evidence"]["source_snapshot"]["nodes"] *= 2
    assert metrics.candidate_source_proof(candidate, ambiguous)["manual_candidate_changed_lines"] is None


def test_new_fb_declaration_is_part_of_manual_edit_proof():
    creation = {"name": "FB_Test", "declaration": "FUNCTION_BLOCK FB_Test", "implementation": "x := TRUE;"}
    case = {"synthetic_object_map": {"new": "candidate-created-pou"},
            "native_compile_evidence": {"source_snapshot": {"nodes": [dict(creation, id="new")]}}}
    assert metrics.candidate_source_proof({"candidate": {"creation": creation}}, case)["manual_candidate_changed_lines"] == 0
    case["native_compile_evidence"]["source_snapshot"]["nodes"][0]["declaration"] += "\n"
    proof = metrics.candidate_source_proof({"candidate": {"creation": creation}}, case)
    assert proof["manual_candidate_changed_lines"] is None
    assert proof["fields"]["declaration"]["exact_equal"] is False


def test_missing_or_invalid_time_never_reports_zero_elapsed():
    assert metrics.elapsed(2.0, 5.5) == 3.5
    for start, finish in ((None, 5), (8, 5), (float("nan"), 5), (True, 5)):
        assert metrics.elapsed(start, finish) is None
    summary = metrics.timing_summary([None, 3.5, -1, float("nan")])
    assert summary["recorded_count"] == 4
    assert summary["measured_count"] == 1
    assert summary["total_seconds"] == 3.5
    assert metrics.timing_summary([None])["total_seconds"] is None
