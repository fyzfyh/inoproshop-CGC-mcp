"""Verify a launcher-selected desktop host using a disposable vendor sample copy.

All native operations use the public MCP tools.  This never compiles, modifies
source text, invokes a model, or closes an IDE/service.  The copied project is
left open for inspection.  Run only against an explicitly selected sample.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
from urllib.parse import urlsplit
import uuid

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


TERMINAL = {"COMPLETED", "FAILED", "REJECTED", "CANCELLED", "RESTORED", "OUTCOME_UNKNOWN", "NEEDS_RECOVERY"}


class AcceptanceFailure(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_json(path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise AcceptanceFailure("INVALID_JSON", "Expected a JSON object in the selected local file.")
    return data


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def same_path(left, right):
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(str(Path(right).resolve()))


def safe_code(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z_0-9]{0,80}", value) else "TOOL_FAILED"


def controlled_failure(exc):
    if isinstance(exc, AcceptanceFailure):
        return exc
    for child in getattr(exc, "exceptions", ()):
        found = controlled_failure(child)
        if found is not None:
            return found
    return None


def validate_inputs(args):
    context = read_json(args.context)
    for field in ("state_dir", "project_root", "host_id", "host_instance_id", "url"):
        if not isinstance(context.get(field), str) or not context[field]:
            raise AcceptanceFailure("INVALID_CONTEXT", "Launcher context is missing an expected identity or local path.")
    url = urlsplit(context["url"])
    if url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost", "::1") or url.username or url.password or url.path != "/mcp" or url.query or url.fragment:
        raise AcceptanceFailure("INVALID_CONTEXT_URL", "Acceptance only sends the local token to the launcher's loopback MCP endpoint.")
    state, project_root = Path(context["state_dir"]).resolve(), Path(context["project_root"]).resolve()
    if not state.is_dir() or not project_root.is_dir():
        raise AcceptanceFailure("CONTEXT_DIRECTORY_MISSING", "Launcher state and project-copy root must already exist.")
    source = args.source.resolve()
    if source.suffix.casefold() != ".project" or not source.is_file():
        raise AcceptanceFailure("SOURCE_REQUIRED", "The selected source must be an existing vendor sample .project file.")
    package = args.omp_package_root.resolve()
    if not (package / "src/mcp/client.ts").is_file() or not (package / "package.json").is_file():
        raise AcceptanceFailure("OMP_PACKAGE_MISSING", "The installed ohmypi MCP client package was not found.")
    bun = shutil.which(str(args.bun)) if args.bun else shutil.which("bun")
    if not bun:
        candidate = Path(args.bun).expanduser() if args.bun else Path.home() / ".bun/bin/bun.exe"
        bun = str(candidate.resolve()) if candidate.is_file() else None
    if not bun:
        raise AcceptanceFailure("BUN_MISSING", "Bun is required to exercise the installed ohmypi MCP client.")
    omp_script = Path(__file__).resolve().with_name("omp-smoke.ts")
    if not omp_script.is_file():
        raise AcceptanceFailure("OMP_SMOKE_MISSING", "The release is missing its existing omp-smoke.ts verification script.")
    return context, state, project_root, source, package, bun, omp_script


async def call_tool(session, name, arguments):
    result = await session.call_tool(name, arguments)
    data = result.structuredContent
    if not isinstance(data, dict):
        raise AcceptanceFailure("MCP_RESULT_MISSING", "The MCP tool did not return a structured result.")
    if result.isError:
        code = safe_code(data.get("error", {}).get("code"))
        raise AcceptanceFailure(code, "The public MCP tool rejected or could not complete this acceptance step.")
    return data


async def wait_operation(session, operation, report, label, args):
    identifiers = {key: operation[key] for key in ("project_id", "operation_id")}
    report[label] = dict(identifiers, state=operation.get("state"))
    atomic_json(args.output, report)
    deadline = time.monotonic() + args.timeout
    next_progress = time.monotonic() + 10
    while time.monotonic() < deadline:
        current = await call_tool(session, "ino_get_operation", identifiers)
        report[label]["state"] = current.get("state")
        if current.get("state") in TERMINAL:
            if current["state"] != "COMPLETED":
                report[label]["domain_error"] = safe_code(current.get("error", {}).get("code"))
                raise AcceptanceFailure("OPERATION_" + current["state"], "The public MCP operation did not complete successfully; it was not replayed.")
            return current
        if time.monotonic() >= next_progress:
            print(f"Waiting for {label}: {current.get('state', 'pending')}", flush=True)
            next_progress = time.monotonic() + 10
        await asyncio.sleep(0.5)
    raise AcceptanceFailure("OPERATION_TIMEOUT", "The public MCP operation timed out; its identity is retained and it was not replayed.")


def summarize_omp(data):
    result = {key: data[key] for key in (
        "status", "at", "layer", "omp_version", "model_invoked", "native_source_read",
        "native_snapshot_tree_read", "project_id", "snapshot_id", "error_type",
    ) if key in data}
    result["checks"] = [{"name": check.get("name"), "passed": check.get("passed") is True}
                        for check in data.get("checks", []) if isinstance(check, dict)]
    if data.get("domain_error"):
        result["domain_error"] = safe_code(data["domain_error"])
    result["source_payloads_omitted"] = True
    return result


async def verify_omp(args, context, project_id, package, bun, omp_script, report):
    output = args.output.with_name(args.output.stem + ".omp-client.json")
    command = [bun, str(omp_script), "--package-root", str(package), "--state-dir", context["state_dir"],
               "--url", context["url"], "--project-id", project_id, "--output", str(output)]
    process = await asyncio.create_subprocess_exec(*command, cwd=str(omp_script.parent.parent),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        await asyncio.wait_for(process.communicate(), timeout=args.timeout)
    except TimeoutError:
        # Only this helper's Bun client is owned here. IDE and services remain.
        process.kill()
        await process.communicate()
        raise AcceptanceFailure("OMP_CLIENT_TIMEOUT", "The installed ohmypi MCP client did not finish within the acceptance timeout.") from None
    finally:
        # The existing smoke worker briefly writes its vendor-sample readback.
        # Replace it with a whitelist before publishing acceptance artifacts.
        if output.is_file():
            try:
                summary = summarize_omp(read_json(output))
            except (OSError, ValueError, AcceptanceFailure):
                summary = {"status": "failed", "model_invoked": False, "source_payloads_omitted": True, "checks": []}
            atomic_json(output, summary)
            report["omp"] = summary
            report["omp_report"] = str(output)
    summary = report.get("omp", {})
    if process.returncode != 0 or summary.get("status") != "passed" or summary.get("model_invoked") is not False or summary.get("native_source_read") is not True or summary.get("native_snapshot_tree_read") is not True:
        raise AcceptanceFailure("OMP_CLIENT_FAILED", "The actual installed ohmypi MCP client did not prove a native POU read.")


async def run(args):
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    report = {"schema_version": 1, "run_id": run_id, "at": timestamp(), "status": "running",
              "model_invoked": False, "compile_invoked": False, "source_text_modified": False,
              "ide_or_services_closed": False, "checks": [], "stage": "preflight"}
    source = None

    def check(name, condition):
        report["checks"].append({"name": name, "passed": bool(condition)})
        atomic_json(args.output, report)
        if not condition:
            raise AcceptanceFailure("CHECK_FAILED", "Acceptance check failed: " + name)

    try:
        context, state, project_root, source, package, bun, omp_script = validate_inputs(args)
        report["source"] = {"path": str(source), "sha256_before": file_hash(source)}
        report["selected_host"] = {key: context[key] for key in ("host_id", "host_instance_id")}
        destination = project_root / ("Launcher_Smoke_" + run_id + ".project")
        report["copy_path"] = str(destination)
        token = (state / "token").read_text(encoding="ascii").strip()
        if len(token) < 32:
            raise AcceptanceFailure("TOKEN_INVALID", "The selected local controller token is missing or invalid.")
        print("Checking the launcher-selected production IDE through official MCP…", flush=True)
        report["stage"] = "host"
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}, timeout=args.timeout, trust_env=False) as client:
            async with streamable_http_client(context["url"], http_client=client) as (reader, writer, _):
                async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=args.timeout)) as session:
                    initialized = await session.initialize()
                    report["mcp_protocol_version"] = initialized.protocolVersion
                    hosts = (await call_tool(session, "ino_list_hosts", {})).get("hosts", [])
                    matches = [host for host in hosts if host.get("host_id") == context["host_id"] and host.get("host_instance_id") == context["host_instance_id"]]
                    check("exact_launcher_host_live", len(matches) == 1 and matches[0].get("available") is True)
                    host = matches[0]
                    check("production_host_not_test_owned", host.get("test_owned") is False)
                    if context.get("pid") is not None:
                        check("launcher_pid_matches", host.get("pid") == context["pid"])
                    capabilities = (await call_tool(session, "ino_get_capabilities", report["selected_host"])).get("host", {})
                    check("production_capabilities_and_root", capabilities.get("test_owned") is False and bool(capabilities.get("write_project_root")) and same_path(capabilities["write_project_root"], project_root))
                    health = capabilities.get("runtime_health", {})
                    check("desktop_ui_ready", health.get("script_entry_has_returned") is True and health.get("ide_enabled") is True and health.get("invoke_required") is False and health.get("modal_windows") == 0)
                    initial = await call_tool(session, "ino_list_projects", report["selected_host"])
                    report["initial_ide"] = {"project_path": host.get("project_path"), "project_count": len(initial.get("projects", [])), "runtime_health": health}
                    if args.require_empty:
                        check("initial_ide_empty", not host.get("project_path") and not initial.get("projects"))

                    print("Opening one new vendor-sample copy through the public MCP tool…", flush=True)
                    report["stage"] = "open_copy"
                    opening = await call_tool(session, "ino_open_project", dict(report["selected_host"], source=str(source), destination=str(destination), idempotency_key="launcher-smoke-open-" + run_id))
                    opened = await wait_operation(session, opening, report, "open_operation", args)
                    project_id = opened["project_id"]
                    report["project_id"] = project_id
                    projects = (await call_tool(session, "ino_list_projects", report["selected_host"])).get("projects", [])
                    chosen = next((item for item in projects if item.get("project_id") == project_id), {})
                    check("owned_copy_became_selected_host_project", bool(chosen.get("project_path")) and same_path(chosen["project_path"], destination) and chosen.get("host_instance_id") == context["host_instance_id"])
                    check("copy_file_matches_original", destination.is_file() and file_hash(destination) == report["source"]["sha256_before"])

                    print("Reading a concrete POU and building the graph index…", flush=True)
                    report["stage"] = "native_read"
                    capture = await call_tool(session, "ino_capture_snapshot", {"project_id": project_id})
                    snapshot_id = capture["snapshot_id"]
                    report["snapshot_id"] = snapshot_id
                    base = {"project_id": project_id, "snapshot_id": snapshot_id}
                    rows, offset = [], 0
                    while True:
                        page = await call_tool(session, "ino_read_object", dict(base, offset=offset, limit=500))
                        rows.extend(page["items"])
                        if page.get("next_offset") is None:
                            break
                        next_offset = page["next_offset"]
                        if not isinstance(next_offset, int) or next_offset <= offset:
                            raise AcceptanceFailure("INVALID_PAGINATION", "Native tree pagination did not advance.")
                        offset = next_offset
                    report["object_count"] = len(rows)
                    check("native_object_tree_read", bool(rows))
                    target = next((row for row in rows if row.get("kind") == "pou"), None)
                    check("concrete_pou_exists", target is not None)
                    readback = await call_tool(session, "ino_read_object", dict(base, object_id=target["id"], limit=500))
                    sections = readback.get("sources", {})
                    declaration_count = len(sections.get("declaration", {}).get("items", []))
                    implementation_count = len(sections.get("implementation", {}).get("items", []))
                    report["read_pou"] = {"object_id": target["id"], "name": target.get("name"), "declaration_lines_read": declaration_count, "implementation_lines_read": implementation_count}
                    check("native_pou_source_read", declaration_count > 0)

                    report["stage"] = "graph_index"
                    indexing = await call_tool(session, "context_index_snapshot", dict(base, idempotency_key="launcher-smoke-index-" + run_id))
                    indexed = await wait_operation(session, indexing, report, "index_operation", args)
                    result = indexed.get("result", {})
                    revision, backend = result.get("index_revision"), result.get("backend", {})
                    report["index"] = {"index_revision": revision, "counts": result.get("counts", {}), "backend": {key: backend[key] for key in ("kind", "evidence", "cgc_version", "driver_version", "nodes", "edges") if key in backend}}
                    check("real_neo4j_graph_published", bool(revision) and backend.get("kind") == "cgc_neo4j" and backend.get("evidence") == "database_transaction_verified")
                    searched = await call_tool(session, "context_search", dict(base, query=target["name"], index_revision=revision, limit=100))
                    hits = searched.get("items", [])
                    target_hit = any(item.get("match_type") == "symbol" and item.get("object_id") == target["id"] and item.get("kind") == "POU" for item in hits)
                    provenance = bool(hits) and all(item.get("source", {}).get("snapshot_id") == snapshot_id and item.get("source", {}).get("object_id") for item in hits)
                    report["search"] = {"query": target["name"], "result_count": len(hits), "index_revision": searched.get("index_revision"), "target_pou_found": target_hit, "snapshot_provenance_valid": bool(provenance)}
                    check("graph_search_matches_native_snapshot", target_hit and provenance and searched.get("index_revision") == revision)

        print("Verifying the installed ohmypi MCP client, without calling a model…", flush=True)
        report["stage"] = "omp_client"
        atomic_json(args.output, report)
        await verify_omp(args, context, project_id, package, bun, omp_script, report)
        check("actual_installed_omp_native_read", True)
        report["status"] = "passed"
        report["stage"] = "complete"
    except Exception as exc:
        failure = controlled_failure(exc)
        report.update(status="failed", error_type=type(exc).__name__, error_code=failure.code if failure else "ACCEPTANCE_FAILED",
                      error=failure.message if failure else "Acceptance did not complete; credentials and raw exception text are not recorded.")
    finally:
        if source is not None and "source" in report:
            try:
                after = file_hash(source)
                unchanged = after == report["source"]["sha256_before"]
                report["source"].update(sha256_after=after, unchanged=unchanged)
                report["checks"].append({"name": "original_sample_unchanged", "passed": unchanged})
                if not unchanged:
                    report.update(status="failed", error_code="SOURCE_CHANGED", error="The original sample hash changed during acceptance.")
            except OSError:
                report.update(status="failed", error_code="SOURCE_HASH_UNAVAILABLE", error="The original sample hash could not be verified after acceptance.")
        report["finished_at"] = timestamp()
        atomic_json(args.output, report)
    print(json.dumps({"status": report["status"], "checks": len(report["checks"]), "output": str(args.output), "model_invoked": False, "compile_invoked": False}, ensure_ascii=False), flush=True)
    return 0 if report["status"] == "passed" else 1


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--omp-package-root", type=Path, default=Path.home() / ".bun/install/global/node_modules/@oh-my-pi/pi-coding-agent")
    parser.add_argument("--bun")
    parser.add_argument("--require-empty", action="store_true")
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    args.context, args.source, args.output = (path.resolve() for path in (args.context, args.source, args.output))
    return args


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    raise SystemExit(asyncio.run(run(parse_args())))
