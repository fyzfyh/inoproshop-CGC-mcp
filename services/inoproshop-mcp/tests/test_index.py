from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from inoproshop_mcp.common import DomainError
from inoproshop_mcp.index import PLCIndex, extract_graph, reachable_objects, tokenize

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]


def fixture():
    return json.loads((ROOT / "fixtures/index/golden.json").read_text(encoding="utf-8"))


class IndexTests(unittest.TestCase):
    def setUp(self):
        temporary_root = ROOT / ".test-tmp-index"
        temporary_root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=temporary_root)
        self.addCleanup(self.temporary.cleanup)
        self.index = PLCIndex(Path(self.temporary.name), backend=False)
        self.snapshot = fixture()

    def test_golden_scopes_instances_and_sources(self):
        result = self.index.build(self.snapshot)
        graph = result["graph"]
        self.assertEqual(graph["issues"], [])
        nodes = {n["id"]: n for n in graph["nodes"]}
        local = next(n for n in nodes.values() if n.get("object_id") == "main" and n["name"] == "value")
        global_value = next(n for n in nodes.values() if n.get("object_id") == "globals" and n["name"] == "value")
        local_reads = self.index.references(self.snapshot, local["id"], "READS")["items"]
        self.assertEqual({e["evidence"]["line"] for e in local_reads}, {3, 4})
        global_reads = self.index.references(self.snapshot, global_value["id"], "READS")["items"]
        self.assertEqual({e["evidence"]["line"] for e in global_reads}, {5})
        members = [n for n in nodes.values() if n["kind"] == "InstanceMember"]
        self.assertEqual({n["name"] for n in members}, {"a.active", "b.active"})
        self.assertEqual(len({n["instance_id"] for n in members}), 2)
        calls = [e for e in graph["edges"] if e["kind"] == "CALLS"]
        self.assertEqual({nodes[e["target"]]["name"] for e in calls}, {"a", "b"})
        self.assertEqual(reachable_objects(self.snapshot), {"main", "motor"})
        impact = self.index.impact(self.snapshot, "motor")
        self.assertEqual({x["object_id"] for x in impact["items"]}, {"motor", "main", "task"})
        self.assertTrue(all(e["evidence"].get("snapshot_id") == "golden-snapshot" for e in graph["edges"]))

    def test_revision_project_snapshot_isolation_and_stable_symbols(self):
        first = self.index.build(self.snapshot)
        changed = copy.deepcopy(self.snapshot)
        changed["snapshot_id"] = "second"
        changed["hash"] = "changed"
        changed["nodes"][4]["declaration"] = "// inserted comment\n" + changed["nodes"][4]["declaration"]
        second = self.index.build(changed)
        first_ids = {n["id"] for n in first["graph"]["nodes"]}
        second_ids = {n["id"] for n in second["graph"]["nodes"]}
        self.assertEqual(first_ids, second_ids)
        self.assertNotEqual(first["index_revision"], second["index_revision"])
        with self.assertRaises(DomainError) as caught:
            self.index.search(changed, "value", index_revision=first["index_revision"])
        self.assertEqual(caught.exception.code, "INDEX_NOT_FOUND")
        changed["project_id"] = "other-project"
        self.index.build(changed)
        with self.assertRaises(DomainError):
            self.index.search(changed, "value", index_revision=second["index_revision"])

    def test_unknown_library_and_invalid_baseline_are_retained(self):
        node = self.snapshot["nodes"][4]
        node["declaration"] = "PROGRAM Main\nVAR\nFB_按钮:R_TRIG;\nx:BOOL;\nEND_VAR"
        node["implementation"] = "FB_按钮(CLK:=ib_启动,Q=>);\nx S= FB_按钮;\nx R= FB_按钮.Q;"
        graph = self.index.build(self.snapshot)["graph"]
        reasons = {issue["reason"] for issue in graph["issues"]}
        self.assertIn("type_definition_unavailable", reasons)
        self.assertIn("external_call_effects_unknown", reasons)
        self.assertIn("member_definition_unavailable", reasons)
        self.assertEqual(len([e for e in graph["edges"] if e["kind"] == "CALLS"]), 1)
        self.assertEqual(reachable_objects(self.snapshot), {"main"})
        self.assertEqual(len([e for e in graph["edges"] if e["kind"] == "WRITES" and e.get("source_object_id") == "main"]), 2)

    def test_ambiguous_globals_and_unsupported_structures_not_guessed(self):
        self.snapshot["nodes"].append({"id":"globals2","name":"Other","application_id":"app","declaration":"VAR_GLOBAL\nresult:BOOL;\nEND_VAR"})
        self.snapshot["nodes"][4]["implementation"] = "result := TRUE;\nFOR i:=0 TO 3 DO result := FALSE; END_FOR;"
        graph = self.index.build(self.snapshot)["graph"]
        self.assertTrue(any(i["state"] == "ambiguous" for i in graph["issues"]))
        self.assertTrue(any(i["state"] == "unsupported" for i in graph["issues"]))
        self.assertFalse(any(e["kind"] == "WRITES" and e.get("source_object_id") == "main" for e in graph["edges"]))

    def test_comments_strings_and_line_columns(self):
        tokens = tokenize("(* outer (* nested *) *)\n\t中文 := 'value // missing'; // hidden\n")
        self.assertEqual(tokens[0].value, "中文")
        self.assertEqual((tokens[0].line, tokens[0].column), (2, 2))
        self.assertEqual([t.value for t in tokens if t.kind == "id"], ["中文"])

    def test_incomplete_lexical_input_never_claims_complete_coverage(self):
        self.snapshot["nodes"][4]["implementation"] = "value := TRUE; (* unfinished\nresult := value;"
        graph = extract_graph(self.snapshot)
        self.assertTrue(any(i["reason"] == "invalid_or_incomplete_token" for i in graph["issues"]))
        self.assertEqual(next(c for c in graph["coverage"] if c["object_id"] == "main")["state"], "unsupported")
        writes = [e for e in graph["edges"] if e["kind"] == "WRITES" and e.get("source_object_id") == "main"]
        self.assertEqual(len(writes), 1)

    def test_function_result_is_a_variable_not_a_write_to_the_pou(self):
        self.snapshot["nodes"].append({"id":"invert","name":"Invert","application_id":"app","declaration":"FUNCTION Invert : BOOL\nVAR_INPUT\n input : BOOL;\nEND_VAR","implementation":"Invert := NOT input;"})
        self.snapshot["nodes"][4]["implementation"] = "value := Invert(input := ib_启动);"
        graph = extract_graph(self.snapshot)
        nodes = {n["id"]: n for n in graph["nodes"]}
        writes = [e for e in graph["edges"] if e["kind"] == "WRITES" and e.get("source_object_id") == "invert"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(nodes[writes[0]["target"]]["scope"], "FUNCTION_RESULT")
        self.assertEqual(reachable_objects(self.snapshot), {"main", "invert"})
        self.assertEqual(graph["issues"], [])

    def test_explicit_local_evidence_paging_and_no_index_are_distinct(self):
        with self.assertRaises(DomainError) as caught:
            self.index.search(self.snapshot, "missing")
        self.assertEqual(caught.exception.code, "INDEX_NOT_FOUND")
        result = self.index.build(self.snapshot)
        self.assertEqual(result["backend"]["evidence"], "deterministic_only")
        page = self.index.search(self.snapshot, "value", limit=2)
        second = self.index.search(self.snapshot, "value", limit=2, offset=page["next_offset"])
        self.assertEqual(len(page["items"]), 2)
        self.assertNotEqual(page["items"], second["items"])
        self.assertEqual(self.index.search(self.snapshot, "not_present")["total"], 0)
        with self.assertRaises(DomainError):
            self.index.search(self.snapshot, "value", limit=0)
        with self.assertRaises(DomainError):
            self.index.references(self.snapshot, "value")

    def test_failed_publication_does_not_replace_active_revision(self):
        first = self.index.build(self.snapshot)
        class FailingBackend:
            def publish(self, graph):
                raise DomainError("INDEX_FAILED", "injected transaction failure")
        self.index.backend = FailingBackend()
        with self.assertRaises(DomainError):
            self.index.build(self.snapshot)
        self.index.backend = False
        self.assertEqual(self.index.search(self.snapshot, "value")["index_revision"], first["index_revision"])

    def test_immutable_snapshot_hash_mismatch_rejected(self):
        self.index.build(self.snapshot)
        self.snapshot["hash"] = "unexpected-mutated-content"
        with self.assertRaises(DomainError) as caught:
            self.index.search(self.snapshot, "value")
        self.assertEqual(caught.exception.code, "STALE_SNAPSHOT")

    def test_source_mutation_cannot_reuse_a_snapshot_even_with_same_claimed_hash(self):
        self.index.build(self.snapshot)
        self.snapshot["nodes"][4]["implementation"] += "\nvalue := FALSE;"
        with self.assertRaises(DomainError) as caught:
            self.index.search(self.snapshot, "value")
        self.assertEqual(caught.exception.code, "STALE_SNAPSHOT")
        with self.assertRaises(DomainError) as caught:
            self.index.build(self.snapshot)
        self.assertEqual(caught.exception.code, "STALE_SNAPSHOT")

    def test_thousand_object_fixture_pagination_and_rebuild(self):
        snapshot = {"project_id":"large","snapshot_id":"one","hash":"large-v1","nodes":[{"id":str(i),"name":"Program" + str(i),"application_id":"app","declaration":"PROGRAM Program" + str(i) + "\nVAR\nvalue:INT;\nEND_VAR","implementation":"value:=value+1;"} for i in range(1000)],"relations":[]}
        first = self.index.build(snapshot)
        self.assertEqual(len(first["coverage"]), 1000)
        ids = []
        offset = 0
        while offset is not None:
            result = self.index.search(snapshot, "Program", limit=137, offset=offset)
            ids.extend((item.get("match_type"), item.get("id"), item.get("object_id"), item.get("line")) for item in result["items"])
            offset = result["next_offset"]
        # One POU symbol, one qualified variable symbol, one source-line hit.
        self.assertEqual(len(ids), 3000)
        self.assertEqual(len(set(ids)), len(ids))
        self.assertEqual(first["index_revision"], self.index.build(snapshot)["index_revision"])

    def test_historical_am600_and_am402_source_fixtures(self):
        am600 = WORKSPACE / "artifacts/v2/ai_package_verification/ai_packages/AI_20260915075653_7f7d94b023/project.json"
        am402 = WORKSPACE / "artifacts/example_open_test/20261010_unnamed1/project_snapshot.json"
        # Historical OEM examples are local evidence, never release fixtures.
        # The synthetic golden and 1000-object tests remain mandatory everywhere.
        if not am600.is_file() or not am402.is_file():
            self.skipTest("Historical AM600/AM402 local evidence is not bundled; synthetic golden coverage remains mandatory.")
        for label, path in [("am600", am600), ("am402", am402)]:
            with self.subTest(label=label):
                snapshot = json.loads(path.read_text(encoding="utf-8"))
                snapshot.update(project_id=label, snapshot_id="historical")
                graph = extract_graph(snapshot)
                self.assertFalse([i for i in graph["issues"] if i["state"] == "unsupported"])
                if label == "am600":
                    self.assertEqual(graph["issues"], [])
                    self.assertEqual(len(reachable_objects(snapshot)), 2)
                else:
                    self.assertEqual(len([e for e in graph["edges"] if e["kind"] == "CALLS"]), 5)
                    self.assertTrue(any(i["reason"] == "external_call_effects_unknown" for i in graph["issues"]))


if __name__ == "__main__":
    unittest.main()
