from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .changes import lookup, make_patch
from .common import DomainError, atomic_json, digest, inside, node_hash, page, uid, utc
from .store import Store, TERMINAL

TOOLS = (
    "ino_get_capabilities", "ino_list_hosts", "ino_list_projects", "ino_open_project",
    "ino_capture_snapshot", "ino_read_object", "ino_read_configuration", "ino_export_context",
    "context_index_snapshot", "context_search", "context_find_references", "context_get_impact",
    "ino_preview_change", "ino_apply_change", "ino_compile", "ino_get_diagnostics",
    "ino_restore_checkpoint", "ino_save_copy", "ino_get_operation", "ino_cancel_operation",
    "ino_set_monitor", "ino_get_changes", "ino_check_rules",
    "ino_list_project_templates", "ino_create_project", "context_search_text",
)


class Core:
    def __init__(self, state_dir=None, project_root=None, transport_factory=None, index_backend=None, template_root=None):
        workspace = Path(__file__).resolve().parents[4]
        self.state_dir = Path(state_dir or os.environ.get("INO_MCP_STATE", workspace / "work/mcp_p0/state")).resolve()
        self.project_root = Path(project_root or os.environ.get("INO_MCP_PROJECTS", workspace / "work/mcp_p0/projects")).resolve()
        self.project_root.mkdir(parents=True, exist_ok=True)
        from .templates import TemplateCatalog
        self.templates = TemplateCatalog(template_root or os.environ.get("INO_MCP_TEMPLATES", workspace / "templates"))
        self.store = Store(self.state_dir)
        if transport_factory is None:
            from .transport import PipeClient
            transport_factory = PipeClient
        self.transport_factory = transport_factory
        self.index_backend = index_backend
        self._index_instance = None
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ino-job")
        self._locks = {}
        self._locks_guard = threading.Lock()
        self._registry_lock = threading.RLock()
        self._stop = threading.Event()
        self._observations = {}
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True, name="ino-monitor")
        self._monitor_thread.start()

    def close(self):
        self._stop.set()
        self._monitor_thread.join()
        self.executor.shutdown(wait=True)
        if self._index_instance is not None:
            backend = self._index_instance.backend
            if backend is not None and backend is not False and hasattr(backend, "close"):
                backend.close()
        self.store.close()

    def _lock(self, project_id):
        with self._locks_guard:
            return self._locks.setdefault(project_id, threading.RLock())

    @property
    def index(self):
        if self._index_instance is None:
            from .index import PLCIndex
            self._index_instance = PLCIndex(self.state_dir, backend=self.index_backend)
        return self._index_instance

    def call(self, tool_name, arguments=None):
        if tool_name not in TOOLS:
            raise DomainError("UNKNOWN_TOOL", "Unknown InoProShop tool.")
        args = dict(arguments or {})
        if args.get("project_id") and tool_name != "ino_open_project":
            project = self._project(args["project_id"])
            for field in ("host_id", "host_instance_id"):
                supplied = args.pop(field, None)
                if supplied is not None and supplied != project.get(field):
                    raise DomainError("HOST_RESTARTED", "Explicit host binding does not match the project.")
        try:
            return getattr(self, tool_name)(**args)
        except TypeError as exc:
            raise DomainError("INVALID_ARGUMENT", "Arguments do not match this tool contract.") from exc

    def _hosts(self):
        from .transport import discover_hosts
        return discover_hosts(self.state_dir)

    def _host(self, host_id, instance_id=None):
        matches = [h for h in self._hosts() if h["host_id"] == host_id and
                   h.get("available", True) and (instance_id is None or h["host_instance_id"] == instance_id)]
        if len(matches) != 1:
            raise DomainError("HOST_UNAVAILABLE", "Choose one registered, live host instance.")
        return matches[0]

    def _project(self, project_id):
        value = self.store.get("project", project_id)
        if not value:
            raise DomainError("PROJECT_NOT_FOUND", "Project is not registered.")
        return value

    def _client(self, project_id):
        project = self._project(project_id)
        host = self._host(project["host_id"], project["host_instance_id"])
        if host.get("binding_id") != project.get("binding_id"):
            raise DomainError("HOST_RESTARTED", "Project was closed, switched or reopened; select the new binding.")
        return self.transport_factory(host, self.store.token), project

    def _command(self, project_id, command, params=None):
        client, project = self._client(project_id)
        return client.call(command, dict(params or {}, expected_binding_id=project["binding_id"]))

    def _writable(self, project_id, recovery=False):
        project = self._project(project_id)
        inside(project["project_path"], self.project_root)
        if project.get("template"):
            from .templates import verify_controller
            origin = project.get("template_origin_project_id")
            if origin and self.store.unresolved(origin):
                raise DomainError("NEEDS_RECOVERY", "The originating template operation still has an unknown outcome.")
            if project.get("template_verification", {}).get("status") != "passed" and not project.get("controller_recheck_required"):
                raise DomainError("CONTROLLER_NOT_VERIFIED", "Template controller verification did not pass; inspection remains available.")
            verification = verify_controller(self._capture(project_id), project["template"]["controller"])
            project["template_verification"] = verification
            project.pop("controller_recheck_required", None)
            self.store.put("project", project_id, project)
            if verification["status"] != "passed":
                raise DomainError("CONTROLLER_MISMATCH", "Root controller changed or cannot be verified; no source write is allowed.", verification)
        siblings = [p for p in self.store.all("project") if p["project_id"] != project_id and
                    os.path.normcase(p["project_path"]) == os.path.normcase(project["project_path"]) and
                    any(h.get("available", True) and h.get("binding_id") == p.get("binding_id") and h["host_instance_id"] == p["host_instance_id"] for h in self._hosts())]
        if siblings:
            raise DomainError("PROJECT_BUSY", "The same path is open in another registered host; use an independent copy.")
        if not recovery and self.store.unresolved(project_id):
            raise DomainError("NEEDS_RECOVERY", "An earlier write has an unresolved outcome. Inspect or restore its checkpoint first.")

    def _snapshot_data(self, project_id, raw):
        data = copy.deepcopy(raw)
        data.update(project_id=project_id, snapshot_id=uid("snapshot"), editor_sync=raw.get("editor_sync", "unknown"))
        dirty = raw.get("project_dirty", raw.get("dirty"))
        data["project_dirty"] = dirty if isinstance(dirty, bool) else None
        data["bridge_snapshot_hash"] = raw.get("bridge_snapshot_hash", raw.get("content_hash", raw.get("hash")))
        data["tasks"] = raw.get("tasks", [n for n in raw["nodes"] if n.get("kind") == "task"])
        for node in data["nodes"]:
            node["content_hash"] = node_hash(node)
        data["hash"] = self._fingerprint(raw)
        return data

    @staticmethod
    def _fingerprint(raw):
        return digest({"binding": raw.get("binding_id"), "path": raw.get("project_path"),
                       "nodes": raw.get("nodes"), "relations": raw.get("relations"),
                       "tasks": raw.get("tasks"), "application": raw.get("active_application_id")})

    def _capture(self, project_id):
        previous = self._command(project_id, "snapshot")
        for attempt in range(3):
            current = self._command(project_id, "snapshot")
            if self._fingerprint(previous) == self._fingerprint(current):
                snapshot = self._snapshot_data(project_id, current)
                self.store.artifact("snapshot", project_id, snapshot, snapshot["snapshot_id"])
                prior = self.store.get("latest_snapshot", project_id)
                if prior and prior["hash"] != snapshot["hash"]:
                    self.store.put("current_diagnostic", project_id, None)
                self.store.put("latest_snapshot", project_id, {"snapshot_id": snapshot["snapshot_id"], "hash": snapshot["hash"]})
                return snapshot
            previous = current
        raise DomainError("INCONSISTENT_SNAPSHOT", "Project changed while being captured; retry after editing settles.")

    def _snapshot(self, project_id, snapshot_id):
        return self.store.read_artifact(snapshot_id, project_id, "snapshot")

    def _check_version(self, project_id, snapshot):
        current = self._capture(project_id)
        if current["hash"] != snapshot["hash"]:
            raise DomainError("STALE_SNAPSHOT", "Project facts changed since the preview/capture.")
        return current

    def _stage(self, operation_id, state, **fields):
        op = self.store.operation(operation_id)
        if op["cancel_requested"] and op["state"] in ("QUEUED", "CHECKING", "CHECKPOINTED"):
            raise DomainError("CANCELLED", "Cancelled before native mutation began.")
        return self.store.update_operation(operation_id, state=state, **fields)

    def _submit(self, project_id, tool, idem, args, work):
        op, created = self.store.create_operation(project_id, tool, idem, args)
        if created:
            def run():
                with self._lock(project_id):
                    try:
                        self._stage(op["operation_id"], "CHECKING")
                        result = work(op["operation_id"])
                        final_state = result.pop("_operation_state", "COMPLETED")
                        self.store.update_operation(op["operation_id"], state=final_state, result=result)
                    except DomainError as exc:
                        if exc.code in ("OPERATION_OUTCOME_UNKNOWN", "HOST_RESTARTED", "HOST_UNAVAILABLE"):
                            record = self.store.operation(op["operation_id"])
                            previous = record["state"]
                            before_write = previous in ("CHECKING", "QUEUED", "CHECKPOINTED") or (
                                tool == "ino_apply_change" and previous == "COMPILING" and not record.get("checkpoint_id"))
                            state = "REJECTED" if before_write else "FAILED" if tool in ("ino_compile", "context_index_snapshot") else "OUTCOME_UNKNOWN"
                        elif exc.code == "NEEDS_RECOVERY":
                            previous = self.store.operation(op["operation_id"])["state"]
                            state = "REJECTED" if previous in ("CHECKING", "QUEUED") else "NEEDS_RECOVERY"
                        elif exc.code == "CANCELLED": state = "CANCELLED"
                        else:
                            previous = self.store.operation(op["operation_id"])["state"]
                            state = "NEEDS_RECOVERY" if previous == "RESTORING" else "FAILED"
                        self.store.update_operation(op["operation_id"], state=state, error=exc.as_dict())
                    except Exception as exc:
                        previous = self.store.operation(op["operation_id"])["state"]
                        state = "NEEDS_RECOVERY" if previous in ("APPLYING", "READING_BACK", "COMPILING", "SAVING", "RESTORING") else "FAILED"
                        self.store.update_operation(op["operation_id"], state=state,
                                                    error={"code": "INTERNAL_ERROR", "message": type(exc).__name__})
                    finally:
                        final = self.store.operation(op["operation_id"])
                        self.store.event(project_id, {"kind": "operation", "operation_id": op["operation_id"], "state": final["state"]})
            self.executor.submit(run)
        return {"operation_id": op["operation_id"], "project_id": project_id, "state": op["state"], "deduplicated": not created}

    def ino_list_hosts(self):
        return {"hosts": self._hosts()}

    def ino_list_projects(self, host_id=None, host_instance_id=None):
        with self._registry_lock:
            return self._list_projects(host_id, host_instance_id)

    def _list_projects(self, host_id=None, host_instance_id=None):
        hosts = self._hosts() if host_id is None else [self._host(host_id, host_instance_id)]
        result = []
        for host in hosts:
            if not host.get("available", True) or not host.get("project_path") or not host.get("binding_id"): continue
            existing = [p for p in self.store.all("project") if p["host_instance_id"] == host["host_instance_id"] and p["binding_id"] == host["binding_id"]]
            if not existing:
                # An open may finish before its reply is received. Template
                # copies must not acquire a new unrestricted identity then.
                for registered in self.store.all("project"):
                    if (registered.get("template") and registered["host_instance_id"] == host["host_instance_id"] and
                            os.path.normcase(registered["project_path"]) == os.path.normcase(host["project_path"])):
                        if registered.get("binding_id") != host["binding_id"]:
                            recheck = registered.get("template_verification", {}).get("status") == "passed" and not self.store.unresolved(registered["project_id"])
                            registered.update(binding_id=host["binding_id"],
                                template_verification={"status": "pending", "reason": "Template copy was rediscovered under a new binding."},
                                controller_recheck_required=recheck)
                            self.store.put("project", registered["project_id"], registered)
                        existing = [registered]
                        break
            if not existing:
                # A save can change the binding before its reply reaches this controller.
                # Do not give an unresolved write a fresh, unrestricted project identity.
                for registered in self.store.all("project"):
                    if registered["host_instance_id"] != host["host_instance_id"]:
                        continue
                    intent_pointer = self.store.get("pending_save", registered["project_id"])
                    if intent_pointer:
                        operation = self.store.operation(intent_pointer["operation_id"])
                        if operation["state"] in ("SAVING", "OUTCOME_UNKNOWN", "NEEDS_RECOVERY"):
                            intent = self.store.read_artifact(intent_pointer["intent_id"], registered["project_id"], "save_intent")
                            if os.path.normcase(intent["destination"]) == os.path.normcase(host["project_path"]):
                                existing = [registered]
                    if existing:
                        break
            if existing:
                project = existing[0]
            else:
                project = {"project_id": uid("project"), "host_id": host["host_id"], "host_instance_id": host["host_instance_id"],
                           "binding_id": host["binding_id"], "project_path": host["project_path"], "registered_at": utc()}
                origin_id = self.store.get("template_path", digest(os.path.normcase(str(Path(host["project_path"]).resolve()))))
                if origin_id:
                    origin = self._project(origin_id)
                    project.update(template=origin["template"], template_origin_project_id=origin_id,
                        template_verification={"status": "pending", "reason": "Controller must be rechecked under the selected new IDE instance."},
                        controller_recheck_required=origin.get("template_verification", {}).get("status") == "passed" and not self.store.unresolved(origin_id))
                self.store.put("project", project["project_id"], project)
            result.append(project)
        return {"projects": result}

    def ino_get_capabilities(self, project_id=None, host_id=None, host_instance_id=None):
        if project_id:
            capabilities = self._command(project_id, "capabilities")
        elif host_id:
            host = self._host(host_id, host_instance_id)
            capabilities = self.transport_factory(host, self.store.token).call("capabilities", {"expected_binding_id": host.get("binding_id")})
        else:
            capabilities = {"host_status": "not_selected"}
        return {"schema_version": "1.0", "tools": list(TOOLS), "host": capabilities,
                "scope": "offline_project_copies", "model": "managed_by_omp", "graphical_editing": "not_in_p0"}

    def ino_open_project(self, source, destination, host_id, host_instance_id, idempotency_key):
        with self._registry_lock:
            return self._open_project(source, destination, host_id, host_instance_id, idempotency_key)

    def ino_list_project_templates(self):
        return self.templates.list()

    def ino_create_project(self, template_id, template_hash, destination, host_id, host_instance_id, idempotency_key):
        with self._registry_lock:
            template = self.templates.select(template_id, template_hash)
            return self._open_project(template["source"], destination, host_id, host_instance_id, idempotency_key, template)

    def _open_project(self, source, destination, host_id, host_instance_id, idempotency_key, template=None):
        # Windows resolves an existing file to its stored casing; the native
        # bridge creates lower-cased paths. Hash the same identity before and
        # after creation so a safe retry does not become a different intent.
        destination = os.path.normcase(str(inside(destination, self.project_root)))
        if template and Path(destination).suffix.lower() != ".project":
            raise DomainError("INVALID_ARGUMENT", "Template destination must be a .project copy.")
        host = self._host(host_id, host_instance_id)
        identity = digest([host_instance_id, idempotency_key] + (["ino_create_project"] if template else []))
        pending = self.store.get("open_identity", identity)
        args = {"source": source, "destination": destination, "host_id": host_id, "host_instance_id": host_instance_id}
        if template:
            args.update(template_id=template["template_id"], template_hash=template["template_hash"],
                        expected_source_sha256=template["sha256"], expected_controller=template["controller"])
        if pending and pending["args_hash"] != digest(args):
            raise DomainError("IDEMPOTENCY_CONFLICT", "Open request parameters changed.")
        project_id = pending["project_id"] if pending else uid("project")
        if not pending:
            self.store.put("open_identity", identity, {"project_id": project_id, "args_hash": digest(args)})
            self.store.put("project", project_id, {"project_id": project_id, "host_id": host_id,
                "host_instance_id": host_instance_id, "binding_id": None, "project_path": destination, "state": "opening",
                **({"template": template, "template_verification": {"status": "pending"}} if template else {})})
            if template:
                self.store.put("template_path", digest(destination), project_id)
        def work(op_id):
            if template:
                self.templates.select(template["template_id"], template["template_hash"])
            self._stage(op_id, "OPENING")
            with self._registry_lock:
                result = self.transport_factory(host, self.store.token).call("open_project", dict(args, expected_binding_id=host.get("binding_id")))
                binding = result.get("binding_id")
                if not binding:
                    raise DomainError("OPERATION_OUTCOME_UNKNOWN", "Opened project binding was not returned.")
                project = self._project(project_id)
                project.update(binding_id=binding, state="open", project_path=result.get("project_path", destination))
                self.store.put("project", project_id, project)
                if template:
                    from .templates import verify_controller
                    self._stage(op_id, "VERIFYING_CONTROLLER")
                    verification = verify_controller(self._capture(project_id), template["controller"])
                    project.update(template_verification=verification,
                                   state="open" if verification["status"] == "passed" else "template_verification_failed")
                    self.store.put("project", project_id, project)
                    result["controller_verification"] = verification
                    if verification["status"] != "passed":
                        raise DomainError("CONTROLLER_MISMATCH" if verification["status"] == "failed" else "CONTROLLER_NOT_VERIFIED",
                            "Template copy is open for inspection, but controller verification did not pass. No source was edited.",
                            {"project_id": project_id, "destination": destination, "verification": verification})
            return dict(result, project_id=project_id)
        return self._submit(project_id, "ino_create_project" if template else "ino_open_project", idempotency_key, args, work)

    def ino_capture_snapshot(self, project_id):
        with self._lock(project_id):
            snapshot = self._capture(project_id)
        return {k: snapshot.get(k) for k in ("project_id", "snapshot_id", "hash", "coverage", "issues", "editor_sync", "project_dirty", "active_application_id")}

    def ino_read_object(self, project_id, snapshot_id, object_id=None, section="both", offset=0, limit=100):
        snapshot = self._snapshot(project_id, snapshot_id)
        if object_id is None:
            rows = [{k: n.get(k) for k in ("id", "parent_id", "name", "path", "kind", "application_id", "content_hash")} for n in snapshot["nodes"]]
            return dict(page(rows, offset, limit), snapshot_id=snapshot_id)
        node = lookup(snapshot, object_id)
        if section not in ("both", "declaration", "implementation"):
            raise DomainError("INVALID_ARGUMENT", "Unknown source section.")
        sections = ("declaration", "implementation") if section == "both" else (section,)
        sources = {s: dict(page((node.get(s) or "").splitlines(), offset, limit), first_line=offset + 1,
                           status=(node.get("source_status") or {}).get(s, "read" if node.get(s) is not None else "unavailable")) for s in sections}
        return {"project_id": project_id, "snapshot_id": snapshot_id, "object_id": object_id,
                "path": node["path"], "content_hash": node_hash(node), "sources": sources}

    def ino_read_configuration(self, project_id, snapshot_id, category):
        snapshot = self._snapshot(project_id, snapshot_id)
        if category not in ("task", "device", "library", "io"):
            raise DomainError("INVALID_ARGUMENT", "Unknown configuration category.")
        kinds = {"task": {"task", "task_configuration"}, "device": {"device"}, "library": {"library"}, "io": {"io"}}
        rows = [n for n in snapshot["nodes"] if n.get("kind") in kinds[category]]
        return {"snapshot_id": snapshot_id, "category": category, "items": rows,
                "coverage": "available_metadata_only", "issues": snapshot.get("issues", []),
                "missing": [] if rows else ["Requested configuration is not exposed by the current host capture."]}

    def _compile(self, project_id, op_id, snapshot=None, application_id=None):
        snapshot = snapshot or self._capture(project_id)
        actual = snapshot.get("active_application_id")
        if application_id is not None and application_id != actual:
            raise DomainError("UNSUPPORTED_CAPABILITY", "Select the requested Application in the IDE before compiling.")
        self._stage(op_id, "COMPILING")
        result = self._command(project_id, "compile", {"application_id": actual, "expected_snapshot_hash": snapshot["bridge_snapshot_hash"]})
        status = result.get("status", "unknown")
        if result.get("error") or status not in ("passed", "failed", "unknown"):
            status = "unknown"
        if status == "passed" and (result.get("errors") != 0 or not result.get("messages")):
            status = "unknown"
        after_build = self._capture(project_id)
        result["source_current"] = after_build["hash"] == snapshot["hash"]
        if not result["source_current"]:
            result.update(native_status=status, reason="Project source changed during compilation.")
            status = "unknown"
        result.update(status=status, snapshot_id=snapshot["snapshot_id"], application_id=actual)
        diagnostic_id = self.store.artifact("diagnostic", project_id, result)
        self.store.put("diagnostic_by_operation", op_id + ":" + diagnostic_id, {"project_id": project_id, "operation_id": op_id, "diagnostic_id": diagnostic_id})
        if result["source_current"]:
            self.store.put("current_diagnostic", project_id, {"diagnostic_id": diagnostic_id, "operation_id": op_id})
        return result

    def ino_compile(self, project_id, idempotency_key, expected_snapshot_id=None, application_id=None):
        def work(op_id):
            snapshot = self._snapshot(project_id, expected_snapshot_id) if expected_snapshot_id else self._capture(project_id)
            snapshot = self._check_version(project_id, snapshot)
            report = self._compile(project_id, op_id, snapshot, application_id)
            return {"compile_status": report["status"], "compile": report,
                    "_operation_state": "COMPLETED" if report["status"] == "passed" else "FAILED"}
        return self._submit(project_id, "ino_compile", idempotency_key,
                            {"expected_snapshot_id": expected_snapshot_id, "application_id": application_id}, work)

    def ino_get_diagnostics(self, project_id, operation_id=None, current=False, offset=0, limit=100):
        if current or operation_id is None:
            pointer = self.store.get("current_diagnostic", project_id)
            records = [pointer] if pointer else []
        else:
            self.store.operation(operation_id, project_id)
            records = [v for v in self.store.all("diagnostic_by_operation") if v["operation_id"] == operation_id and v["project_id"] == project_id]
        reports = [dict(self.store.read_artifact(v["diagnostic_id"], project_id, "diagnostic"), diagnostic_id=v["diagnostic_id"]) for v in records]
        return page(reports, offset, limit)

    def context_index_snapshot(self, project_id, snapshot_id, idempotency_key=None):
        snapshot = self._snapshot(project_id, snapshot_id)
        return self._submit(project_id, "context_index_snapshot", idempotency_key or snapshot_id, {"snapshot_id": snapshot_id},
                            lambda op_id: self._build_index(project_id, snapshot, op_id))

    def _build_index(self, project_id, snapshot, op_id=None):
        if op_id: self._stage(op_id, "INDEXING")
        result = self.index.build(snapshot)
        self.store.put("index_state", project_id, {"snapshot_id": snapshot["snapshot_id"], "hash": snapshot["hash"], "index_revision": result["index_revision"], "status": "ready"})
        graph = result.pop("graph", {})
        result["counts"] = {"nodes": len(graph.get("nodes", [])), "edges": len(graph.get("edges", []))}
        result["details"] = "Use paginated context tools with this snapshot_id and index_revision."
        return result

    def context_search(self, project_id, snapshot_id, query, limit=50, offset=0, index_revision=None, **kwargs):
        snapshot = self._snapshot(project_id, snapshot_id)
        return self._context_freshness(project_id, snapshot, self.index.search(snapshot, query, limit=limit, offset=offset, index_revision=index_revision))

    def context_search_text(self, project_id, snapshot_id, query, section="both", object_id=None,
                            case_sensitive=False, context_lines=0, limit=50, offset=0):
        from .text_search import search_snapshot
        snapshot = self._snapshot(project_id, snapshot_id)
        result = search_snapshot(snapshot, query, section, object_id, case_sensitive, context_lines, offset, limit)
        return self._context_freshness(project_id, snapshot, result)

    def context_find_references(self, project_id, snapshot_id, symbol_id, relation=None, index_revision=None, limit=100, offset=0):
        snapshot = self._snapshot(project_id, snapshot_id)
        return self._context_freshness(project_id, snapshot, self.index.references(snapshot, symbol_id, relation=relation, index_revision=index_revision, limit=limit, offset=offset))

    def context_get_impact(self, project_id, snapshot_id, object_id, max_depth=3, index_revision=None, limit=100, offset=0):
        snapshot = self._snapshot(project_id, snapshot_id)
        return self._context_freshness(project_id, snapshot, self.index.impact(snapshot, object_id, max_depth=max_depth, index_revision=index_revision, limit=limit, offset=offset))

    def _context_freshness(self, project_id, snapshot, result):
        latest = self.store.get("latest_snapshot", project_id)
        result.update(current_project_stale=(latest["hash"] != snapshot["hash"]) if latest else None,
                      freshness_basis="last_captured_snapshot; live_host_not_checked",
                      latest_captured_snapshot_id=latest["snapshot_id"] if latest else None)
        return result

    @staticmethod
    def _error_rows(report):
        return [m for m in (report or {}).get("messages", []) if m.get("severity", "").lower() in ("error", "fatalerror")]

    @staticmethod
    def _error_key(message):
        # Exclude volatile timings, snapshot IDs and source hashes. Keep source position and text.
        location = message.get("location") or {}
        return digest([message.get("number"), message.get("text"), message.get("object_id"),
                       location.get("section"), location.get("line"), location.get("column")])

    def _repair_delta(self, baseline, candidate, patch):
        original, remaining = self._error_rows(baseline), self._error_rows(candidate)
        baseline_counts = Counter(self._error_key(m) for m in original)
        new_errors = []
        for message in remaining:
            key = self._error_key(message)
            if baseline_counts[key]: baseline_counts[key] -= 1
            else: new_errors.append(message)
        target_ids = {c["before"]["id"] for c in patch["changes"]}
        targets = [m for m in original if (m.get("object_id") or (m.get("location") or {}).get("node_id")) in target_ids]
        return {"original": original, "target": targets, "target_basis": "compiler_object_guid_matches_changed_object",
                "unlocated_original": [m for m in original if not (m.get("object_id") or (m.get("location") or {}).get("node_id"))],
                "new": new_errors, "remaining": remaining, "overall_passed": bool(candidate and candidate.get("status") == "passed")}

    def ino_preview_change(self, project_id, snapshot_id, change, mode="strict", allow_partial_repair=False):
        snapshot = self._snapshot(project_id, snapshot_id)
        patch = make_patch(snapshot, change, mode, allow_partial_repair)
        from .index import reachable_objects
        reachable = reachable_objects(snapshot)
        if patch["target_object_id"] not in reachable:
            raise DomainError("TARGET_NOT_COVERED", "The target/caller is not proven reachable from an actual task.")
        patch["impact"] = self.index.impact(snapshot, patch["target_object_id"], max_depth=3)
        if change.get("type") == "replace_st_sections" and "declaration" in change:
            from .index import extract_graph
            candidate = copy.deepcopy(snapshot)
            for replacement in patch["changes"]:
                after = dict(replacement["after"], content_hash=node_hash(replacement["after"]))
                candidate["nodes"] = [after if n["id"] == replacement["before"]["id"] else n for n in candidate["nodes"]]
            candidate["snapshot_id"] = "candidate_" + patch["patch_id"]
            candidate["hash"] = self._fingerprint(candidate)
            graph = extract_graph(candidate)
            patch["declaration_analysis"] = {"basis": "candidate_static_analysis_not_compiler",
                "candidate_id": candidate["snapshot_id"], "base_snapshot_id": snapshot_id,
                "unresolved_count": graph["unresolved_count"], "issues": graph["issues"],
                "coverage": graph["coverage"], "limitations": graph["limitations"]}
        patch["patch_hash"] = digest({k: v for k, v in patch.items() if k != "patch_hash"})
        self.store.artifact("patch", project_id, patch, patch["patch_id"])
        return patch

    @staticmethod
    def _capability(capabilities, key):
        value = capabilities.get(key, capabilities.get("capabilities", {}).get(key))
        return value is True or value == "supported" or (isinstance(value, dict) and value.get("status") == "supported")

    def ino_apply_change(self, project_id, patch_id, idempotency_key, authorization, save_destination=None):
        patch = self.store.read_artifact(patch_id, project_id, "patch")
        if not isinstance(authorization, dict) or any(authorization.get(k) != patch[k] for k in ("project_id", "patch_id", "patch_hash")):
            raise DomainError("AUTHORIZATION_MISMATCH", "Authorization must bind this exact project and reviewed patch hash.")
        if save_destination: save_destination = str(inside(save_destination, self.project_root))
        return self._submit(project_id, "ino_apply_change", idempotency_key,
                            {"patch_id": patch_id, "authorization": authorization, "save_destination": save_destination},
                            lambda op_id: self._apply(project_id, op_id, patch, save_destination))

    def _apply(self, project_id, op_id, patch, save_destination):
        self._writable(project_id)
        current = self._check_version(project_id, self._snapshot(project_id, patch["snapshot_id"]))
        capabilities = self._command(project_id, "capabilities")
        if patch["new_object"] and not all(self._capability(capabilities, k) for k in ("delete_object", "create_st_pou", "two_object_recovery")):
            raise DomainError("UNSUPPORTED_CAPABILITY", "Native deletion and two-object recovery must be verified before create-and-attach is enabled.")
        baseline = self._compile(project_id, op_id, current, patch["application_id"])
        if baseline["status"] == "unknown" or (patch["mode"] == "strict" and baseline["status"] != "passed"):
            raise DomainError("BASELINE_COMPILE_FAILED", "Strict mode requires a passing baseline; unknown compilation cannot authorize writes.", {"baseline": baseline})
        current = self._check_version(project_id, current)
        checkpoint_id = uid("checkpoint")
        checkpoint = {"checkpoint_id": checkpoint_id, "project_id": project_id, "operation_id": op_id,
                      "snapshot_id": current["snapshot_id"], "patch": patch, "baseline": baseline, "created_object_id": None,
                      "created_object_hash": None, "phase": "prepared", "created_at": utc()}
        self.store.artifact("checkpoint", project_id, checkpoint, checkpoint_id)
        self.store.put("checkpoint_state", checkpoint_id, checkpoint)
        self._stage(op_id, "CHECKPOINTED", checkpoint_id=checkpoint_id)
        self._stage(op_id, "APPLYING")
        candidate = None
        try:
            if patch["new_object"]:
                new = self._command(project_id, "import_st", dict(patch["new_object"], expected_snapshot_hash=current["bridge_snapshot_hash"]))
                checkpoint["created_object_id"] = new.get("id", new.get("object_id"))
                if not checkpoint["created_object_id"]:
                    raise DomainError("OPERATION_OUTCOME_UNKNOWN", "Import did not return a native object GUID.")
                checkpoint["created_object_hash"] = node_hash(patch["new_object"])
                checkpoint["phase"] = "created_unverified"
                self.store.put("checkpoint_state", checkpoint_id, checkpoint)
                current = self._capture(project_id)
                actual = lookup(current, checkpoint["created_object_id"])
                if node_hash(actual) != node_hash(patch["new_object"]):
                    raise DomainError("READBACK_MISMATCH", "New FB source differs from candidate.")
                checkpoint["created_object_hash"] = node_hash(actual)
                checkpoint["phase"] = "created"
                self.store.put("checkpoint_state", checkpoint_id, checkpoint)
            for change in patch["changes"]:
                self._command(project_id, "replace_text", {"object_id": change["before"]["id"],
                    "declaration": change["after"]["declaration"], "implementation": change["after"]["implementation"],
                    "expected_node_hash": change["before_hash"], "expected_snapshot_hash": current["bridge_snapshot_hash"]})
                checkpoint["phase"] = "written"
                self.store.put("checkpoint_state", checkpoint_id, checkpoint)
                current = self._capture(project_id)
                if node_hash(lookup(current, change["before"]["id"])) != change["after_hash"]:
                    raise DomainError("READBACK_MISMATCH", "Caller/source readback differs from the preview.")
            self._stage(op_id, "READING_BACK")
            from .index import reachable_objects
            reachable = reachable_objects(current)
            required = {patch["target_object_id"]}
            if checkpoint["created_object_id"]: required.add(checkpoint["created_object_id"])
            if not required.issubset(reachable):
                raise DomainError("TARGET_NOT_COVERED", "Modified code is no longer task reachable.")
            candidate = self._compile(project_id, op_id, current, patch["application_id"])
            checkpoint["candidate_compile"] = candidate
            self.store.put("checkpoint_state", checkpoint_id, checkpoint)
            if candidate["status"] != "passed":
                raise DomainError("COMPILE_FAILED" if candidate["status"] == "failed" else "COMPILE_UNKNOWN", "Candidate did not pass native compilation.")
        except DomainError as exc:
            if exc.code in ("OPERATION_OUTCOME_UNKNOWN", "HOST_UNAVAILABLE", "HOST_RESTARTED"):
                raise
            restored = self._restore(project_id, op_id, checkpoint)
            return {"_operation_state": "RESTORED", "edit_status": "restored", "compile_status": candidate["status"] if candidate else "not_completed",
                    "failure": exc.as_dict(), "candidate_compile": candidate, "baseline_compile": baseline,
                    "checkpoint_id": checkpoint_id, "recovery": restored, "persistence_status": "memory_only",
                    "repair": self._repair_delta(baseline, candidate, patch)}
        result = {"edit_status": "applied", "readback_status": "matched", "compile_status": "passed",
                  "compile": candidate, "baseline_compile": baseline, "checkpoint_id": checkpoint_id,
                  "created_object_id": checkpoint["created_object_id"], "persistence_status": "memory_only"}
        result["repair"] = self._repair_delta(baseline, candidate, patch)
        if save_destination:
            self.store.update_operation(op_id, result=result)
            self._stage(op_id, "SAVING")
            try:
                result["save"] = self._save(project_id, current, save_destination, op_id)
                result["persistence_status"] = "saved_copy"
            except DomainError as exc:
                if exc.code == "OPERATION_OUTCOME_UNKNOWN": raise
                result.update(persistence_status="save_failed", save_error=exc.as_dict(), _operation_state="FAILED")
        current = self._capture(project_id)
        result["snapshot_id"] = current["snapshot_id"]
        try:
            self._build_index(project_id, current, op_id)
            result["index_status"] = "ready"
        except Exception as exc:
            self.store.put("index_state", project_id, {"status": "stale", "snapshot_id": current["snapshot_id"]})
            result.update(index_status="failed", index_error=getattr(exc, "code", type(exc).__name__))
        self.store.event(project_id, {"kind": "project_changed", "snapshot_id": current["snapshot_id"], "operation_id": op_id})
        return result

    def _restore(self, project_id, op_id, checkpoint):
        self._stage(op_id, "RESTORING")
        patch = checkpoint["patch"]
        current = self._capture(project_id)
        created_id = checkpoint.get("created_object_id")
        if patch["new_object"] and created_id is None:
            matches = [n for n in current["nodes"] if n["name"].casefold() == patch["new_object"]["name"].casefold()]
            if matches:
                raise DomainError("NEEDS_RECOVERY", "Import ownership is unknown; never remove by name or repeat the import.")
        for change in patch["changes"]:
            observed = node_hash(lookup(current, change["before"]["id"]))
            if observed not in (change["before_hash"], change["after_hash"]):
                raise DomainError("NEEDS_RECOVERY", "Caller was edited after this operation; automatic restoration stopped.")
        created_present = any(n["id"] == created_id for n in current["nodes"]) if created_id else False
        if created_id and not created_present and any(n["name"].casefold() == patch["new_object"]["name"].casefold() for n in current["nodes"]):
            raise DomainError("NEEDS_RECOVERY", "The recorded GUID is absent but another object now uses its name.")
        if created_present:
            actual = lookup(current, created_id)
            expected = checkpoint.get("created_object_hash") or node_hash(patch["new_object"])
            if node_hash(actual) != expected:
                raise DomainError("NEEDS_RECOVERY", "The new object has been edited; automatic deletion stopped.")
            excluded = {created_id} | {c["before"]["id"] for c in patch["changes"]}
            name = patch["new_object"]["name"]
            # Conservatively stop on any new lexical reference outside the controlled caller.
            for node in current["nodes"]:
                if node["id"] not in excluded and re.search(r"(?i)(?<!\w)" + re.escape(name) + r"(?!\w)", (node.get("declaration") or "") + "\n" + (node.get("implementation") or "")):
                    raise DomainError("NEEDS_RECOVERY", "Another object now references the created FB.")
        for change in reversed(patch["changes"]):
            observed = lookup(current, change["before"]["id"])
            if node_hash(observed) != change["before_hash"]:
                self._command(project_id, "replace_text", {"object_id": observed["id"],
                    "declaration": change["before"]["declaration"], "implementation": change["before"]["implementation"],
                    "expected_node_hash": node_hash(observed), "expected_snapshot_hash": current["bridge_snapshot_hash"]})
                current = self._capture(project_id)
                if node_hash(lookup(current, observed["id"])) != change["before_hash"]:
                    raise DomainError("NEEDS_RECOVERY", "Restored caller did not match its checkpoint.")
        if created_present:
            excluded = {created_id} | {c["before"]["id"] for c in patch["changes"]}
            name = patch["new_object"]["name"]
            for node in current["nodes"]:
                if node["id"] not in excluded and re.search(r"(?i)(?<!\w)" + re.escape(name) + r"(?!\w)", (node.get("declaration") or "") + "\n" + (node.get("implementation") or "")):
                    raise DomainError("NEEDS_RECOVERY", "A new reference appeared while restoring the caller.")
            self._command(project_id, "delete_object", {"object_id": created_id,
                "expected_node_hash": node_hash(lookup(current, created_id)), "expected_snapshot_hash": current["bridge_snapshot_hash"]})
            current = self._capture(project_id)
            if any(n["id"] == created_id for n in current["nodes"]):
                raise DomainError("NEEDS_RECOVERY", "Created object still exists after deletion.")
        restored_compile = self._compile(project_id, op_id, current, patch["application_id"])
        baseline = checkpoint["baseline"]
        baseline_errors = Counter(self._error_key(m) for m in self._error_rows(baseline))
        restored_errors = Counter(self._error_key(m) for m in self._error_rows(restored_compile))
        if (restored_compile["status"] == "unknown" or restored_compile["status"] != baseline["status"]
                or baseline_errors != restored_errors or baseline.get("errors") != restored_compile.get("errors")):
            raise DomainError("NEEDS_RECOVERY", "Sources restored but baseline compilation state could not be verified.")
        checkpoint.update(phase="restored", recovery_compile=restored_compile)
        self.store.put("checkpoint_state", checkpoint["checkpoint_id"], checkpoint)
        self.store.put("index_state", project_id, {"status": "stale", "snapshot_id": current["snapshot_id"]})
        return {"status": "restored", "compile": restored_compile, "snapshot_id": current["snapshot_id"]}

    def ino_restore_checkpoint(self, project_id, checkpoint_id, expected_snapshot_id, idempotency_key):
        original = self.store.read_artifact(checkpoint_id, project_id, "checkpoint")
        checkpoint = self.store.get("checkpoint_state", checkpoint_id, original)
        def work(op_id):
            self._writable(project_id, recovery=True)
            self._check_version(project_id, self._snapshot(project_id, expected_snapshot_id))
            result = self._restore(project_id, op_id, checkpoint)
            old = self.store.operation(checkpoint["operation_id"], project_id)
            if old["state"] in ("OUTCOME_UNKNOWN", "NEEDS_RECOVERY"):
                self.store.update_operation(old["operation_id"], state="RESTORED", recovery_operation_id=op_id)
            return dict(result, _operation_state="RESTORED")
        return self._submit(project_id, "ino_restore_checkpoint", idempotency_key,
                            {"checkpoint_id": checkpoint_id, "expected_snapshot_id": expected_snapshot_id}, work)

    def _save(self, project_id, snapshot, destination, op_id=None):
        with self._registry_lock:
            return self._save_bound(project_id, snapshot, destination, op_id)

    def _save_bound(self, project_id, snapshot, destination, op_id=None):
        if Path(destination).exists():
            raise DomainError("DESTINATION_EXISTS", "Choose a new copy path; existing files are never overwritten.")
        if op_id:
            intent = {"project_id": project_id, "operation_id": op_id, "snapshot_id": snapshot["snapshot_id"],
                      "destination": destination, "project": self._project(project_id), "at": utc()}
            intent_id = self.store.artifact("save_intent", project_id, intent)
            self.store.update_operation(op_id, save_intent_id=intent_id)
            self.store.put("pending_save", project_id, {"operation_id": op_id, "intent_id": intent_id})
        result = self._command(project_id, "save_copy", {"destination": destination, "expected_snapshot_hash": snapshot["bridge_snapshot_hash"]})
        if not Path(destination).is_file():
            raise DomainError("SAVE_FAILED", "The target copy is absent from disk.")
        if result.get("binding_id") and result["binding_id"] != self._project(project_id)["binding_id"]:
            project = self._project(project_id)
            project.update(binding_id=result["binding_id"], project_path=result.get("project_path", destination))
            self.store.put("project", project_id, project)
            if project.get("template"):
                path_key = digest(os.path.normcase(str(Path(project["project_path"]).resolve())))
                self.store.put("template_path", path_key, project_id)
        return result

    def ino_save_copy(self, project_id, expected_snapshot_id, destination, idempotency_key):
        destination = str(inside(destination, self.project_root))
        def work(op_id):
            self._writable(project_id)
            snapshot = self._check_version(project_id, self._snapshot(project_id, expected_snapshot_id))
            self._stage(op_id, "SAVING")
            return {"persistence_status": "saved_copy", "save": self._save(project_id, snapshot, destination, op_id)}
        return self._submit(project_id, "ino_save_copy", idempotency_key,
                            {"expected_snapshot_id": expected_snapshot_id, "destination": destination}, work)

    def ino_get_operation(self, project_id, operation_id, reconcile=False):
        if reconcile:
            with self._lock(project_id), self._registry_lock:
                return self._reconcile_save(project_id, operation_id)
        return self.store.operation(operation_id, project_id)

    def _reconcile_save(self, project_id, operation_id):
        """Read native facts only. Never replay a save or infer ownership from a filename."""
        op = self.store.operation(operation_id, project_id)
        if op["state"] not in ("OUTCOME_UNKNOWN", "NEEDS_RECOVERY"):
            return op
        if not op.get("save_intent_id"):
            raise DomainError("NEEDS_RECOVERY", "This operation has no durable save intent; inspect its checkpoint instead.")
        intent = self.store.read_artifact(op["save_intent_id"], project_id, "save_intent")
        expected = self._snapshot(project_id, intent["snapshot_id"])
        binding = intent["project"]
        host = self._host(binding["host_id"], binding["host_instance_id"])
        destination = inside(intent["destination"], self.project_root)
        if not destination.is_file() or os.path.normcase(str(destination)) != os.path.normcase(host.get("project_path", "")):
            raise DomainError("NEEDS_RECOVERY", "The original host is not bound to the saved destination; no save was repeated.")
        client = self.transport_factory(host, self.store.token)
        first = client.call("snapshot", {"expected_binding_id": host["binding_id"]})
        second = client.call("snapshot", {"expected_binding_id": host["binding_id"]})
        if self._fingerprint(first) != self._fingerprint(second):
            raise DomainError("INCONSISTENT_SNAPSHOT", "Wait for editing to settle before save reconciliation.")
        if second.get("dirty", second.get("project_dirty")) is not False:
            raise DomainError("NEEDS_RECOVERY", "The current IDE project is not explicitly clean; preservation takes priority.")
        if second.get("binding_id") != host["binding_id"] or os.path.normcase(second.get("project_path", "")) != os.path.normcase(str(destination)):
            raise DomainError("NEEDS_RECOVERY", "Native project identity does not match the recorded save destination.")
        observed = self._snapshot_data(project_id, second)
        fields = ("nodes", "relations", "tasks", "active_application_id")
        if digest({k: expected.get(k) for k in fields}) != digest({k: observed.get(k) for k in fields}):
            raise DomainError("NEEDS_RECOVERY", "Current project facts differ from the saved intent; later edits will not be overwritten.")
        import hashlib
        evidence = {"kind": "save_reconciliation", "operation_id": operation_id, "destination": str(destination),
                    "host_instance_id": host["host_instance_id"], "binding_id": host["binding_id"],
                    "file_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                    "basis": "same_host_instance; expected_source_facts; native_clean_destination; file_present",
                    "disk_reopen_verified": False, "native_writes_replayed": 0, "at": utc()}
        evidence_id = self.store.artifact("reconciliation", project_id, evidence)
        project = self._project(project_id)
        project.update(binding_id=host["binding_id"], project_path=str(destination))
        self.store.put("project", project_id, project)
        result = dict(op.get("result") or {})
        result.update(persistence_status="saved_copy_reconciled", save=evidence, reconciliation_id=evidence_id)
        if op["tool"] == "ino_apply_change":
            result["index_status"] = "rebuild_required"
        self.store.put("current_diagnostic", project_id, None)
        self.store.event(project_id, {"kind": "save_reconciled", "operation_id": operation_id, "evidence_id": evidence_id})
        return self.store.update_operation(operation_id, state="COMPLETED", result=result,
                                           previous_error=op.get("error"), error=None)

    def ino_cancel_operation(self, project_id, operation_id):
        op = self.store.operation(operation_id, project_id)
        cancellable = op["state"] in ("QUEUED", "CHECKING", "CHECKPOINTED")
        if op["state"] not in TERMINAL:
            self.store.update_operation(operation_id, cancel_requested=True)
        return {"accepted": cancellable, "state": op["state"], "operation_id": operation_id,
                "reason": "Cancellation occurs before writing; native calls are not interrupted."}

    def ino_export_context(self, project_id, snapshot_id, destination=None):
        snapshot = self._snapshot(project_id, snapshot_id)
        export_root = self.state_dir / "exports"
        target = inside(destination, export_root) if destination else export_root / uid("context")
        if target.exists(): raise DomainError("DESTINATION_EXISTS", "Context exports are immutable.")
        temporary = target.with_name(target.name + ".pending")
        temporary.mkdir(parents=True)
        atomic_json(temporary / "project.json", snapshot)
        (temporary / "coverage.md").write_text("# Coverage\n\n" + json.dumps(snapshot.get("coverage", {}), ensure_ascii=False, indent=2) + "\n\n" + "\n".join(snapshot.get("limitations", [])), encoding="utf-8")
        source_map = []
        for node in snapshot["nodes"]:
            for section in ("declaration", "implementation"):
                if node.get(section) is not None:
                    # Generated filename is content-addressed; raw native IDs never become paths.
                    filename = "sources/" + digest(node["id"])[:24] + "_" + section + ".st"
                    path = temporary / filename
                    path.parent.mkdir(exist_ok=True)
                    path.write_bytes(node[section].encode("utf-8"))
                    source_map.append({"file": filename, "object_id": node["id"], "section": section, "first_line": 1})
        atomic_json(temporary / "source_map.json", source_map)
        import hashlib
        manifest = {str(p.relative_to(temporary)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest() for p in temporary.rglob("*") if p.is_file()}
        atomic_json(temporary / "manifest.json", manifest)
        payload_files = [p for p in temporary.rglob("*") if p.is_file()]
        temp_archive = temporary / "context.zip.pending"
        with zipfile.ZipFile(temp_archive, "w", zipfile.ZIP_DEFLATED) as stream:
            for path in payload_files:
                stream.write(path, path.relative_to(temporary).as_posix())
        with zipfile.ZipFile(temp_archive) as stream:
            if stream.testzip() is not None: raise DomainError("EXPORT_FAILED", "Archive verification failed.")
            for filename, expected in manifest.items():
                if hashlib.sha256(stream.read(filename)).hexdigest() != expected:
                    raise DomainError("EXPORT_FAILED", "Archive manifest mismatch.")
        temp_archive.rename(temporary / "context.zip")
        # The directory rename publishes the verified ZIP and all source files together.
        temporary.rename(target)
        archive = target / "context.zip"
        result = {"project_id": project_id, "snapshot_id": snapshot_id, "directory": str(target), "zip": str(archive), "manifest": manifest}
        self.store.artifact("export", project_id, result)
        return result

    def ino_check_rules(self, project_id, snapshot_id, rule_set_version="1"):
        if rule_set_version != "1": raise DomainError("UNSUPPORTED_RULE_SET", "Only rule set 1 is installed.")
        snapshot = self._snapshot(project_id, snapshot_id)
        findings = []
        for node in snapshot["nodes"]:
            for line, source in enumerate((node.get("implementation") or "").splitlines(), 1):
                for rule, pattern, message in (("PLC001", r"\bTODO\b", "Unresolved TODO comment."),
                    ("PLC002", r"/\s*0(?![\w.])", "Literal integer zero divisor; inspect native diagnostics."),
                    ("PLC003", r"\bWHILE\b|\bREPEAT\b", "Loop execution bound requires review.")):
                    if re.search(pattern, source, re.I):
                        findings.append({"rule_id": rule, "object_id": node["id"], "section": "implementation", "line": line, "message": message, "kind": "lexical_advice"})
        return {"snapshot_id": snapshot_id, "rule_set_version": "1", "findings": findings, "not_a_compiler": True}

    def ino_set_monitor(self, project_id, enabled, poll_seconds=1.0, debounce_seconds=2.0, auto_compile=False):
        self._project(project_id)
        if not isinstance(enabled, bool) or not 0.5 <= poll_seconds <= 60 or not 0.5 <= debounce_seconds <= 60:
            raise DomainError("INVALID_ARGUMENT", "Monitor intervals must be between 0.5 and 60 seconds.")
        config = {"project_id": project_id, "enabled": enabled, "poll_seconds": poll_seconds,
                  "debounce_seconds": debounce_seconds, "auto_compile": auto_compile}
        self.store.put("monitor", project_id, config)
        self._observations.pop(project_id, None)
        return config

    def ino_get_changes(self, project_id, after_cursor=0, limit=100):
        page([], 0, limit)
        return self.store.events(project_id, after_cursor, limit)

    def _monitor_loop(self):
        while not self._stop.wait(0.25):
            for config in self.store.all("monitor"):
                if not config["enabled"]: continue
                project_id, now = config["project_id"], time.monotonic()
                previous_index = self.store.get("index_state", project_id) or {}
                state = self._observations.setdefault(project_id, {"last_poll": 0, "hash": None, "stable_since": now,
                    "published": previous_index.get("hash") if previous_index.get("status") == "ready" else None})
                if now - state["last_poll"] < config["poll_seconds"]: continue
                lock = self._lock(project_id)
                if not lock.acquire(blocking=False): continue
                try:
                    state["last_poll"] = now
                    raw = self._command(project_id, "snapshot")
                    observed_at = time.monotonic()
                    fingerprint = self._fingerprint(raw)
                    if fingerprint != state["hash"]:
                        state.update(hash=fingerprint, stable_since=observed_at)
                    elif fingerprint != state["published"] and observed_at - state["stable_since"] >= config["debounce_seconds"]:
                        snapshot = self._capture(project_id)
                        if snapshot["hash"] != fingerprint: continue
                        self.store.put("index_state", project_id, {"status": "stale", "snapshot_id": snapshot["snapshot_id"]})
                        try:
                            self._build_index(project_id, snapshot)
                            index_status = "ready"
                        except Exception:
                            index_status = "failed"
                        self.store.event(project_id, {"kind": "project_changed", "snapshot_id": snapshot["snapshot_id"], "index_status": index_status})
                        state["published"] = fingerprint
                        if config["auto_compile"]:
                            self.ino_compile(project_id, "monitor:" + fingerprint, snapshot["snapshot_id"])
                except DomainError as exc:
                    if state.get("error") != exc.code:
                        self.store.event(project_id, {"kind": "monitor_error", "error": exc.as_dict()})
                    state["error"] = exc.code
                except Exception as exc:
                    if state.get("error") != type(exc).__name__:
                        self.store.event(project_id, {"kind": "monitor_error", "error": {"code": type(exc).__name__}})
                    state["error"] = type(exc).__name__
                finally:
                    lock.release()
