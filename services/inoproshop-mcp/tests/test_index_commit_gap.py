"""Crash the test worker between database commit and local index publication.

These tests never start or contact an IDE/controller. Only the opt-in test
connects to Neo4j; it uses a unique synthetic project identity for every case.
The worker exits without cleanup, so rebuilding verifies persisted state rather
than relying on an exception handler or an in-memory cached graph.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from inoproshop_mcp import index as index_module
from inoproshop_mcp.cgc_backend import CGCNeo4jBackend
from inoproshop_mcp.common import DomainError, atomic_json
from inoproshop_mcp.index import PLCIndex


ROOT = Path(__file__).resolve().parents[1]
CRASH_EXIT_CODE = 73

# Only this dedicated test child is terminated. Credentials remain inherited
# environment variables and never appear in argv, stdout, stderr or reports.
WORKER = r'''
import json, os, sys
from pathlib import Path
from inoproshop_mcp import index as module
from inoproshop_mcp.cgc_backend import CGCNeo4jBackend
from inoproshop_mcp.transport import PipeClient

state, source, marker, mode, phase, parser_version = sys.argv[1:]
snapshot = json.loads(Path(source).read_text(encoding="utf-8"))
module.PARSER_VERSION = parser_version
native_attempts = []
def forbid_native(*args, **kwargs):
    native_attempts.append("unexpected_bridge_call")
    raise AssertionError("Index-only crash worker must never call the host")
PipeClient.call = forbid_native
backend = CGCNeo4jBackend() if mode == "cgc" else False
index = module.PLCIndex(Path(state), backend=backend)
original_atomic = module._atomic

def interrupted_atomic(path, value):
    at_boundary = (phase == "before_revision" and path.name.startswith("idx_")) or (
        phase == "before_active" and path.name == "active.json")
    if at_boundary:
        revision = value["index_revision"]
        database_committed = False
        if backend is not False:
            committed = backend.load(snapshot["project_id"], snapshot["snapshot_id"], revision)
            assert committed["index_revision"] == revision
            assert committed["parser_version"] == parser_version
            database_committed = True
        with Path(marker).open("w", encoding="utf-8") as stream:
            json.dump({"phase": phase, "index_revision": revision,
                       "database_committed": database_committed,
                       "native_commands_attempted": native_attempts}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os._exit(73)
    return original_atomic(path, value)

module._atomic = interrupted_atomic
index.build(snapshot)
raise AssertionError("Crash boundary was not reached")
'''


def exercise_commit_gap(root, monkeypatch, *, real_database, phase, has_previous):
    mode = "cgc" if real_database else "local"
    root.mkdir(parents=True)
    snapshot = json.loads((ROOT / "fixtures/index/golden.json").read_text(encoding="utf-8"))
    snapshot["project_id"] = "commit-gap-" + uuid.uuid4().hex
    source = root / "snapshot.json"
    source.write_text(json.dumps(snapshot), encoding="utf-8")
    marker = root / "crash.json"
    state = root / "state"
    backend = CGCNeo4jBackend() if real_database else False
    original = PLCIndex(state, backend=backend)
    before = original.build(snapshot) if has_previous else None
    old_search = original.search(snapshot, "value") if has_previous else None
    prior_parser = index_module.PARSER_VERSION
    upgraded_parser = prior_parser + "+crash-acceptance"
    if backend is not False:
        backend.close()

    worker_env = dict(os.environ)
    worker_env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + worker_env.get("PYTHONPATH", "")
    exited = subprocess.run(
        [sys.executable, "-c", WORKER, str(state), str(source), str(marker), mode, phase, upgraded_parser],
        cwd=ROOT, env=worker_env, capture_output=True, timeout=60,
    )
    assert exited.returncode == CRASH_EXIT_CODE, (
        f"{mode}/{phase}/prior={has_previous}: isolated worker did not reach the crash boundary "
        f"(exit={exited.returncode}); child output intentionally omitted"
    )
    crash = json.loads(marker.read_text(encoding="utf-8"))
    assert crash["database_committed"] is real_database
    assert crash["native_commands_attempted"] == []

    # A new manager and PLCIndex emulate controller restart. No old Python
    # object or successful build result from the crashed child is reused.
    restarted_backend = CGCNeo4jBackend() if real_database else False
    restarted = PLCIndex(state, backend=restarted_backend)
    host_attempts = []
    from inoproshop_mcp.transport import PipeClient

    def forbid_native(*_args, **_kwargs):
        host_attempts.append("unexpected_bridge_call")
        raise AssertionError("Index recovery must not call the host")

    try:
        with monkeypatch.context() as patched:
            patched.setattr(PipeClient, "call", forbid_native)
            if has_previous:
                assert restarted.search(snapshot, "value") == old_search
                assert restarted.search(snapshot, "value", index_revision=before["index_revision"]) == old_search
                assert crash["index_revision"] != before["index_revision"]
            else:
                with pytest.raises(DomainError) as missing:
                    restarted.search(snapshot, "value")
                assert missing.value.code == "INDEX_NOT_FOUND"

            if real_database:
                orphan = restarted_backend.load(snapshot["project_id"], snapshot["snapshot_id"], crash["index_revision"])
                assert orphan["parser_version"] == upgraded_parser

            # A complete revision artifact may be queried explicitly even if
            # active publication did not happen. DB-only revisions are hidden.
            if phase == "before_revision":
                with pytest.raises(DomainError) as unpublished:
                    restarted.search(snapshot, "value", index_revision=crash["index_revision"])
                assert unpublished.value.code == "INDEX_NOT_FOUND"
            else:
                assert restarted.search(snapshot, "value", index_revision=crash["index_revision"])["total"] > 0

            patched.setattr(index_module, "PARSER_VERSION", upgraded_parser)
            recovered = restarted.build(snapshot)
            assert recovered["index_revision"] == crash["index_revision"]
            assert restarted.search(snapshot, "value")["index_revision"] == crash["index_revision"]
            repeated = restarted.build(snapshot)
            assert repeated["index_revision"] == recovered["index_revision"]
            assert repeated["graph"] == recovered["graph"]
            if has_previous:
                assert restarted.search(snapshot, "value", index_revision=before["index_revision"]) == old_search

            if real_database:
                params = restarted_backend._params(snapshot["project_id"], snapshot["snapshot_id"], recovered["index_revision"])
                counts = restarted_backend._query(
                    "MATCH (n:PLCIRNode {revision_key:$revision_key}) "
                    "OPTIONAL MATCH (n)-[e:PLC_RELATION {revision_key:$revision_key}]->() "
                    "RETURN count(DISTINCT n) AS nodes, count(e) AS edges", **params
                )[0]
                assert counts == {"nodes": len(recovered["graph"]["nodes"]), "edges": len(recovered["graph"]["edges"])}
            assert host_attempts == []

        return {
            "phase": phase, "had_previous_active": has_previous,
            "database_commit_observed_before_exit": crash["database_committed"],
            "worker_exit_code": exited.returncode,
            "default_read_after_restart": "previous_active" if has_previous else "INDEX_NOT_FOUND",
            "historical_revision_preserved": has_previous,
            "rebuild_converged": True, "repeat_build_no_duplicate_nodes_or_edges": real_database,
            "native_commands_attempted": host_attempts,
            "project_id": snapshot["project_id"], "recovered_index_revision": recovered["index_revision"],
        }
    finally:
        if restarted_backend is not False:
            restarted_backend.close()


def test_local_publication_crash_preserves_old_active_and_rebuild_converges(tmp_path, monkeypatch):
    for phase in ("before_revision", "before_active"):
        for has_previous in (False, True):
            exercise_commit_gap(tmp_path / f"{phase}_{has_previous}", monkeypatch,
                                real_database=False, phase=phase, has_previous=has_previous)


@pytest.mark.skipif(os.environ.get("INOPROSHOP_RUN_CGC_TESTS") != "1",
                    reason="real CGC/Neo4j test requires configured database and explicit opt-in")
def test_real_cgc_commit_gap_survives_process_exit_and_rebuild(tmp_path, monkeypatch):
    cases = []
    for phase in ("before_revision", "before_active"):
        for has_previous in (False, True):
            cases.append(exercise_commit_gap(tmp_path / f"{phase}_{has_previous}", monkeypatch,
                                            real_database=True, phase=phase, has_previous=has_previous))
    backend = CGCNeo4jBackend()
    try:
        health = backend.health()
    finally:
        backend.close()
    atomic_json(ROOT.parents[1] / "artifacts/mcp_p0/cgc_commit_gap.json", {
        "status": "passed", "timestamp": datetime.now(timezone.utc).isoformat(),
        "layer": "real_cgc_neo4j_and_isolated_worker_process_exit", "backend": health,
        "cases": cases, "native_host_used": False,
        "scope": "Synthetic graph fixtures; child exits after real DB commit and before a local atomic publish. No IDE or controller restart.",
        "limitations": ["Controlled process exit at publication boundaries; not a filesystem power-loss durability test."]
    })
