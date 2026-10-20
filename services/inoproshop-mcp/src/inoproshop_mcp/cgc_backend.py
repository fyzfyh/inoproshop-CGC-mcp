"""PLC schema extension over the real, pinned CGC Neo4j DatabaseManager.

No CGC generic Class/Function nodes are used to pretend ST support. All PLC
records are revision-scoped and written in one Neo4j transaction. CGC owns the
driver/session implementation; this extension supplies PLC schema and Cypher.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json

CGC_VERSION = "0.6.13"
NAMESPACE = "inoproshop_mcp_plc_ir_v1"


def _error(code, message, details=None):
    from .common import DomainError
    raise DomainError(code, message, details or {})


def _key(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


class CGCNeo4jBackend:
    """One explicit Neo4j backend; never auto-select a different CGC backend."""

    def __init__(self, manager=None):
        self.manager = manager
        self._driver = None

    def _connect(self):
        if self._driver is not None:
            return self._driver
        try:
            version = importlib.metadata.version("codegraphcontext")
            if version != CGC_VERSION:
                _error("INDEX_BACKEND_UNAVAILABLE", "CGC version differs from the verified adapter pin", {"required": CGC_VERSION, "installed": version})
            if self.manager is None:
                from codegraphcontext.core.database import DatabaseManager
                self.manager = DatabaseManager()
            if self.manager.get_backend_type() != "neo4j":
                _error("INDEX_BACKEND_UNAVAILABLE", "PLC adapter requires explicitly configured Neo4j")
            self._driver = self.manager.get_driver()
            with self._driver.session() as session:
                session.run("CREATE CONSTRAINT inoproshop_plc_revision_key IF NOT EXISTS FOR (r:PLCIndexRevision) REQUIRE r.key IS UNIQUE").consume()
                session.run("CREATE CONSTRAINT inoproshop_plc_node_key IF NOT EXISTS FOR (n:PLCIRNode) REQUIRE n.key IS UNIQUE").consume()
            return self._driver
        except Exception as exc:
            from .common import DomainError
            if isinstance(exc, DomainError):
                raise
            # Do not echo driver exception text: it can contain credentials/URIs.
            self._driver = None
            _error("INDEX_BACKEND_UNAVAILABLE", "CGC/Neo4j is not ready; configure NEO4J_URI, NEO4J_USERNAME and NEO4J_PASSWORD and verify the local database", {"exception_type": type(exc).__name__, "cgc_version": CGC_VERSION, "status": "blocked"})

    @staticmethod
    def _params(project_id, snapshot_id, revision):
        return {"namespace": NAMESPACE, "project_id": str(project_id), "snapshot_id": str(snapshot_id), "index_revision": str(revision), "revision_key": _key(NAMESPACE, str(project_id), str(snapshot_id), str(revision))}

    def publish(self, graph):
        params = self._params(graph["project_id"], graph["snapshot_id"], graph["index_revision"])
        nodes = [{"key": _key(params["revision_key"], node["id"]), "id": node["id"], "kind": node["kind"], "name": node.get("name", ""), "name_lower": node.get("name", "").casefold(), "qualified_lower": node.get("qualified_name", "").casefold(), "object_id": node.get("object_id"), "json": json.dumps(node, ensure_ascii=False)} for node in graph["nodes"]]
        edges = [{"id": edge["id"], "source_key": _key(params["revision_key"], edge["source"]), "target_key": _key(params["revision_key"], edge["target"]), "kind": edge["kind"], "state": edge["state"], "json": json.dumps(edge, ensure_ascii=False)} for edge in graph["edges"]]
        encoded = json.dumps(graph, ensure_ascii=False, sort_keys=True)

        def write(tx):
            tx.run("MERGE (r:PLCIndexRevision {key:$revision_key}) SET r.namespace=$namespace, r.project_id=$project_id, r.snapshot_id=$snapshot_id, r.index_revision=$index_revision, r.status='staging'", **params).consume()
            # Idempotent replay affects this revision only, inside this transaction.
            tx.run("MATCH (n:PLCIRNode {revision_key:$revision_key}) DETACH DELETE n", **params).consume()
            tx.run("UNWIND $nodes AS n CREATE (v:PLCIRNode) SET v = n, v.revision_key=$revision_key, v.namespace=$namespace, v.project_id=$project_id, v.snapshot_id=$snapshot_id, v.index_revision=$index_revision", nodes=nodes, **params).consume()
            tx.run("UNWIND $edges AS e MATCH (a:PLCIRNode {key:e.source_key, revision_key:$revision_key}), (b:PLCIRNode {key:e.target_key, revision_key:$revision_key}) CREATE (a)-[r:PLC_RELATION]->(b) SET r=e, r.revision_key=$revision_key", edges=edges, **params).consume()
            count = tx.run("MATCH (n:PLCIRNode {revision_key:$revision_key}) RETURN count(n) AS count", **params).single()["count"]
            edge_count = tx.run("MATCH (:PLCIRNode {revision_key:$revision_key})-[e:PLC_RELATION {revision_key:$revision_key}]->(:PLCIRNode {revision_key:$revision_key}) RETURN count(e) AS count", **params).single()["count"]
            if count != len(nodes) or edge_count != len(edges):
                raise ValueError("PLC graph referential integrity check failed")
            tx.run("MATCH (r:PLCIndexRevision {key:$revision_key}) SET r.status='published', r.graph_json=$graph_json, r.node_count=$node_count, r.edge_count=$edge_count, r.cgc_version=$cgc_version", graph_json=encoded, node_count=len(nodes), edge_count=len(edges), cgc_version=CGC_VERSION, **params).consume()

        try:
            with self._connect().session() as session:
                session.execute_write(write)
            return {"kind": "cgc_neo4j", "cgc_version": CGC_VERSION, "driver_version": importlib.metadata.version("neo4j"), "evidence": "database_transaction_verified", "nodes": len(nodes), "edges": len(edges)}
        except Exception as exc:
            from .common import DomainError
            if isinstance(exc, DomainError):
                raise
            _error("INDEX_FAILED", "CGC Neo4j index publication failed; previous index remains available", {"exception_type": type(exc).__name__})

    def load(self, project_id, snapshot_id, revision):
        params = self._params(project_id, snapshot_id, revision)
        records = self._query("MATCH (r:PLCIndexRevision {key:$revision_key, namespace:$namespace, project_id:$project_id, snapshot_id:$snapshot_id, index_revision:$index_revision, status:'published'}) RETURN r.graph_json AS graph_json", **params)
        if not records:
            _error("INDEX_NOT_FOUND", "CGC has no published revision for this project and snapshot")
        return json.loads(records[0]["graph_json"])

    def _query(self, query, **params):
        try:
            with self._connect().session() as session:
                return [dict(record) for record in session.run(query, **params)]
        except Exception as exc:
            from .common import DomainError
            if isinstance(exc, DomainError):
                raise
            _error("INDEX_BACKEND_UNAVAILABLE", "CGC Neo4j query failed; no local fallback was used", {"exception_type": type(exc).__name__, "status": "blocked"})

    def symbol_ids(self, project_id, snapshot_id, revision, query):
        params = self._params(project_id, snapshot_id, revision)
        return {row["id"] for row in self._query("MATCH (n:PLCIRNode {revision_key:$revision_key, project_id:$project_id, snapshot_id:$snapshot_id}) WHERE n.name_lower CONTAINS $query_text OR n.qualified_lower CONTAINS $query_text RETURN n.id AS id", query_text=query.casefold(), **params)}

    def reference_ids(self, project_id, snapshot_id, revision, symbol_ids, relation=None):
        params = self._params(project_id, snapshot_id, revision)
        return {row["id"] for row in self._query("MATCH (a:PLCIRNode {revision_key:$revision_key})-[r:PLC_RELATION {revision_key:$revision_key}]->(b:PLCIRNode {revision_key:$revision_key}) WHERE (a.id IN $symbol_ids OR b.id IN $symbol_ids) AND ($relation IS NULL OR r.kind=$relation) RETURN DISTINCT r.id AS id", symbol_ids=list(symbol_ids), relation=relation, **params)}

    def health(self):
        with self._connect().session() as session:
            record = session.run("CALL dbms.components() YIELD name, versions RETURN name, versions").single()
        runtime = self._query("CALL dbms.queryJmx('java.lang:type=Runtime') YIELD attributes RETURN attributes['SpecVersion'].value AS java_specification_version, attributes['VmVersion'].value AS java_vm_version")
        return {"kind": "cgc_neo4j", "cgc_version": CGC_VERSION, "database": dict(record) if record else None, "runtime": runtime[0] if runtime else None}

    def close(self):
        if self.manager is not None:
            self.manager.close_driver()
        self._driver = None
