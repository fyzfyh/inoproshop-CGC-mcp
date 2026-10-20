"""Opt-in real CGC + Neo4j contract test; skipping is never a native pass."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from inoproshop_mcp.cgc_backend import CGCNeo4jBackend
from inoproshop_mcp.common import DomainError, atomic_json
from inoproshop_mcp.index import PLCIndex

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("INOPROSHOP_RUN_CGC_TESTS") == "1", "real CGC/Neo4j test requires configured database and explicit opt-in")
class CGCIntegrationTests(unittest.TestCase):
    def test_real_thousand_object_fixture(self):
        temporary_root = ROOT / ".test-tmp-index"
        temporary_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temporary_root) as directory:
            backend = CGCNeo4jBackend()
            self.addCleanup(backend.close)
            index = PLCIndex(Path(directory), backend=backend)
            snapshot = {"project_id":"cgc-native-scale","snapshot_id":"1000-objects-v1","hash":"1000-object-fixture-v1","nodes":[{"id":str(i),"name":"ScalePOU" + str(i),"application_id":"app","declaration":"PROGRAM ScalePOU" + str(i) + "\nVAR\nvalue:INT;\nEND_VAR","implementation":"value:=value+1;"} for i in range(1000)],"relations":[]}
            start = time.perf_counter()
            built = index.build(snapshot)
            build_seconds = time.perf_counter() - start
            seen, offset = set(), 0
            start = time.perf_counter()
            while offset is not None:
                page = index.search(snapshot, "ScalePOU", limit=137, offset=offset)
                for item in page["items"]:
                    key = (item["match_type"], item.get("id"), item.get("object_id"), item.get("line"))
                    self.assertNotIn(key, seen)
                    seen.add(key)
                offset = page["next_offset"]
            self.assertEqual(len(seen), 3000)
            self.assertEqual(len(built["coverage"]), 1000)
            atomic_json(ROOT.parents[1] / "artifacts/mcp_p0/cgc_scale.json", {"status":"passed", "timestamp":datetime.now(timezone.utc).isoformat(), "fixture_objects":1000, "search_hits":3000, "duplicate_or_missing_hits":0, "build_seconds":build_seconds, "all_pages_seconds":time.perf_counter()-start, "backend":backend.health(), "publication":built["backend"], "index_revision":built["index_revision"], "scope":"synthetic scale fixture; not a native 1000-object InoProShop project"})

    def test_real_cgc_publication_query_isolation_and_atomic_failure(self):
        temporary_root = ROOT / ".test-tmp-index"
        temporary_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temporary_root) as directory:
            backend = CGCNeo4jBackend()
            self.addCleanup(backend.close)
            index = PLCIndex(Path(directory), backend=backend)
            snapshot = json.loads((ROOT / "fixtures/index/golden.json").read_text(encoding="utf-8"))
            snapshot["project_id"] = "cgc-native-golden"
            result = index.build(snapshot)
            self.assertEqual(result["backend"]["kind"], "cgc_neo4j")
            self.assertEqual(result["backend"]["evidence"], "database_transaction_verified")
            search = index.search(snapshot, "value")
            symbol = next(n for n in search["items"] if n["match_type"] == "symbol" and n.get("object_id") == "main")
            references = index.references(snapshot, symbol["id"], "READS")
            self.assertEqual(len(references["items"]), 2)
            self.assertEqual({x["object_id"] for x in index.impact(snapshot, "motor")["items"]}, {"motor", "main", "task"})
            other = copy.deepcopy(snapshot)
            other["project_id"] = "cgc-native-other-project"
            second = index.build(other)
            self.assertNotEqual(result["index_revision"], second["index_revision"])
            with self.assertRaises(DomainError):
                backend.load(other["project_id"], other["snapshot_id"], result["index_revision"])
            newer = copy.deepcopy(snapshot)
            newer["snapshot_id"] = "golden-newer"
            newer["hash"] = "changed"
            newer["nodes"][4]["implementation"] = "value := FALSE;"
            index.build(newer)
            self.assertEqual(len(index.references(snapshot, symbol["id"], "READS")["items"]), 2)
            malformed = copy.deepcopy(result["graph"])
            malformed["edges"][0]["target"] = "nonexistent-node"
            with self.assertRaises(DomainError) as caught:
                backend.publish(malformed)
            self.assertEqual(caught.exception.code, "INDEX_FAILED")
            self.assertEqual(backend.load(snapshot["project_id"], snapshot["snapshot_id"], result["index_revision"]), result["graph"])
            # Evidence distinguishes true database execution from the local IR tests.
            evidence = {"status":"passed", "timestamp":datetime.now(timezone.utc).isoformat(), "backend":backend.health(), "publication":result["backend"], "index_revision":result["index_revision"], "checks":["CGC DatabaseManager import and connection", "PLC nodes and edges persisted in Neo4j", "native symbol and reference Cypher", "project and snapshot isolation", "old revision remains queryable", "invalid edge transaction rolled back"], "fixture_sha256":__import__("hashlib").sha256((ROOT / "fixtures/index/golden.json").read_bytes()).hexdigest()}
            atomic_json(ROOT.parents[1] / "artifacts/mcp_p0/cgc_integration.json", evidence)


if __name__ == "__main__":
    unittest.main()
