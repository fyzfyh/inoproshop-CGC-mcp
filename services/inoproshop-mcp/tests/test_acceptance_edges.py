"""A09/A20 deterministic acceptance edges, without native-compiler claims.

All input is synthetic. Index tests explicitly use the local IR backend;
real CGC transactions remain covered by test_index_cgc.py. Rule goldens
describe lexical advice, including matches inside comments and strings.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from inoproshop_mcp import index as index_module
from inoproshop_mcp.common import DomainError, digest
from inoproshop_mcp.core import Core
from inoproshop_mcp.index import PLCIndex, reachable_objects


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def indexed_golden(tmp_path):
    snapshot = json.loads((ROOT / "fixtures/index/golden.json").read_text(encoding="utf-8"))
    index = PLCIndex(tmp_path / "state", backend=False)
    return index, snapshot, index.build(snapshot)


def changed_snapshot(original, snapshot_id):
    changed = copy.deepcopy(original)
    changed["snapshot_id"] = snapshot_id
    changed["hash"] = digest([snapshot_id, changed["nodes"]])
    return changed


def test_a09_deleted_object_removes_its_edges_but_old_snapshot_remains_queryable(indexed_golden):
    index, original, before = indexed_golden
    old_owned_ids = {node["id"] for node in before["graph"]["nodes"] if node.get("object_id") == "motor"}
    assert old_owned_ids
    old_impact = index.impact(original, "motor", index_revision=before["index_revision"])
    assert {item["object_id"] for item in old_impact["items"]} == {"motor", "main", "task"}

    deleted = changed_snapshot(original, "after-fb-delete")
    deleted["nodes"] = [node for node in deleted["nodes"] if node["id"] != "motor"]
    deleted["hash"] = digest(deleted["nodes"])
    after = index.build(deleted)

    assert after["index_revision"] != before["index_revision"]
    assert not any(node.get("object_id") == "motor" for node in after["graph"]["nodes"])
    assert not any(edge["source"] in old_owned_ids or edge["target"] in old_owned_ids
                   for edge in after["graph"]["edges"])
    assert reachable_objects(deleted) == {"main"}
    # The caller still names the deleted FB. Preserve that source as an unknown
    # type; deleting a definition must not fabricate an empty/unreferenced graph.
    assert any(issue["reason"] == "type_definition_unavailable" for issue in after["graph"]["issues"])
    with pytest.raises(DomainError) as missing:
        index.impact(deleted, "motor")
    assert missing.value.code == "OBJECT_NOT_FOUND"
    with pytest.raises(DomainError) as stale:
        index.search(deleted, "Motor", index_revision=before["index_revision"])
    assert stale.value.code == "INDEX_NOT_FOUND"

    reopened = PLCIndex(index.root.parent, backend=False)
    assert reopened.impact(original, "motor", index_revision=before["index_revision"])["items"] == old_impact["items"]
    assert reopened.search(original, "Motor")["index_revision"] == before["index_revision"]


def test_a09_renamed_object_updates_names_without_rewriting_old_snapshot(indexed_golden):
    index, original, before = indexed_golden
    renamed = changed_snapshot(original, "after-fb-rename")
    for node in renamed["nodes"]:
        if node["id"] == "motor":
            node["name"] = "Pump"
        if isinstance(node.get("declaration"), str):
            node["declaration"] = node["declaration"].replace("Motor", "Pump")
    renamed["hash"] = digest(renamed["nodes"])
    after = index.build(renamed)

    assert after["index_revision"] != before["index_revision"]
    assert after["graph"]["issues"] == []
    assert index.search(renamed, "Motor")["total"] == 0
    new_symbols = [item for item in index.search(renamed, "Pump")["items"] if item["match_type"] == "symbol"]
    assert any(item["object_id"] == "motor" and item["name"] == "Pump" for item in new_symbols)
    assert {item["object_id"] for item in index.impact(renamed, "motor")["items"]} == {"motor", "main", "task"}
    assert reachable_objects(renamed) == {"main", "motor"}

    reopened = PLCIndex(index.root.parent, backend=False)
    old_symbols = [item for item in reopened.search(original, "Motor", index_revision=before["index_revision"])["items"]
                   if item["match_type"] == "symbol"]
    assert any(item["object_id"] == "motor" and item["name"] == "Motor" for item in old_symbols)
    assert reopened.search(original, "Pump", index_revision=before["index_revision"])["total"] == 0
    assert all(edge["evidence"]["snapshot_id"] == renamed["snapshot_id"] for edge in after["graph"]["edges"])


def test_a09_parser_upgrade_publishes_new_revision_and_keeps_explicit_old_revision(indexed_golden, monkeypatch):
    index, snapshot, before = indexed_golden
    old_parser = index_module.PARSER_VERSION
    old_symbols = {node["id"] for node in before["graph"]["nodes"]}
    old_variable = next(node for node in before["graph"]["nodes"]
                        if node.get("object_id") == "main" and node["name"] == "value")
    old_reads = index.references(snapshot, old_variable["id"], "READS", index_revision=before["index_revision"])

    upgraded_parser = old_parser + "+acceptance-upgrade"
    monkeypatch.setattr(index_module, "PARSER_VERSION", upgraded_parser)
    after = index.build(snapshot)

    assert before["index_revision"] != after["index_revision"]
    assert after["graph"]["parser_version"] == upgraded_parser
    assert {node["id"] for node in after["graph"]["nodes"]} == old_symbols
    assert index.search(snapshot, "value")["index_revision"] == after["index_revision"]
    assert index.build(snapshot)["index_revision"] == after["index_revision"]

    reopened = PLCIndex(index.root.parent, backend=False)
    historical = reopened.references(snapshot, old_variable["id"], "READS", index_revision=before["index_revision"])
    current = reopened.references(snapshot, old_variable["id"], "READS", index_revision=after["index_revision"])
    assert historical["items"] == old_reads["items"]
    assert {edge["evidence"]["parser_version"] for edge in historical["items"]} == {old_parser}
    assert {edge["evidence"]["parser_version"] for edge in current["items"]} == {upgraded_parser}
    assert {edge["evidence"]["line"] for edge in historical["items"]} == {3, 4}
    assert {edge["evidence"]["line"] for edge in current["items"]} == {3, 4}


@pytest.fixture
def rules_core(tmp_path):
    def forbidden_host(*_):
        raise AssertionError("Rule checks must use a stored snapshot without host calls")

    core = Core(tmp_path / "state", tmp_path / "projects", forbidden_host, index_backend=False)
    core.store.put("project", "rules-project", {"project_id": "rules-project"})
    yield core
    core.close()


def run_rules(core, implementation, *, declaration="PROGRAM Synthetic\n", version="1"):
    snapshot = {"project_id": "rules-project", "snapshot_id": "rules-snapshot", "nodes": [
        {"id": "synthetic-guid", "declaration": declaration, "implementation": implementation},
        {"id": "declaration-only", "declaration": "// TODO WHILE / 0", "implementation": None},
    ]}
    core.store.artifact("snapshot", snapshot["project_id"], snapshot, snapshot["snapshot_id"])
    result = core.call("ino_check_rules", {"project_id": snapshot["project_id"], "snapshot_id": snapshot["snapshot_id"],
                                           "rule_set_version": version})
    assert core.store.read_artifact(snapshot["snapshot_id"], snapshot["project_id"], "snapshot") == snapshot
    return result


def test_a20_rule_goldens_preserve_line_numbers_and_label_comments_strings_as_lexical_advice(rules_core):
    source = "\r\n".join([
        "// TODO：确认时序",
        "\toutput := numerator / 0;",
        "WHILE enabled DO",
        " output := output + 1;",
        "END_WHILE;",
        "REPEAT",
        " output := output - 1;",
        "UNTIL done END_REPEAT;",
        "(* todo: WHILE / 0 是说明文字 *)",
        "message := 'TODO REPEAT / 0';",
        "(* ordinary comment *)",
        "todo_flag := 1; meanwhile := 2;",
        "output := numerator / 10;",
    ])
    result = run_rules(rules_core, source)
    assert result["rule_set_version"] == "1"
    assert result["snapshot_id"] == "rules-snapshot"
    assert result["not_a_compiler"] is True
    assert [(item["rule_id"], item["line"]) for item in result["findings"]] == [
        ("PLC001", 1), ("PLC002", 2), ("PLC003", 3), ("PLC003", 6),
        ("PLC001", 9), ("PLC002", 9), ("PLC003", 9),
        ("PLC001", 10), ("PLC002", 10), ("PLC003", 10),
    ]
    assert {item["kind"] for item in result["findings"]} == {"lexical_advice"}
    assert {item["object_id"] for item in result["findings"]} == {"synthetic-guid"}
    assert {item["section"] for item in result["findings"]} == {"implementation"}
    assert "compile_status" not in result and "model_opinion" not in result


def test_a20_nonzero_numeric_literals_do_not_become_literal_zero_advice(rules_core):
    result = run_rules(rules_core, "x := y / 0.5;\nx := y / 0_1;\nx := y / 10;\nx := y / 0;")
    assert [(item["rule_id"], item["line"]) for item in result["findings"]] == [("PLC002", 4)]


def test_a20_unknown_rule_version_is_explicitly_rejected(rules_core):
    with pytest.raises(DomainError) as caught:
        run_rules(rules_core, "// TODO", version="uninstalled-rules")
    assert caught.value.code == "UNSUPPORTED_RULE_SET"


def test_a20_rules_cannot_read_another_projects_snapshot(rules_core):
    run_rules(rules_core, "// TODO")
    rules_core.store.put("project", "other-project", {"project_id": "other-project"})
    with pytest.raises(DomainError) as caught:
        rules_core.call("ino_check_rules", {"project_id": "other-project", "snapshot_id": "rules-snapshot"})
    assert caught.value.code == "ARTIFACT_NOT_FOUND"
