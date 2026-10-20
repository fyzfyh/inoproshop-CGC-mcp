"""Launch an owned, hidden native host and verify only disposable offline copies.

Run with CPython on the engineering workstation. Never downloads or goes online.
The report is evidence for this exact bridge source and OEM software version.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/inoproshop-mcp/src"))
from inoproshop_mcp.common import DomainError, atomic_json, node_hash
from inoproshop_mcp.transport import PipeClient, discover_hosts
from native_dialogs import DialogWatcher


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=r"D:\Inoproshop\CODESYS\Projects\Demo for AM600\002_Marquee(ST).project")
    parser.add_argument("--exe", default=r"D:\Inoproshop\CODESYS\Common\InoProShop.exe")
    parser.add_argument("--profile", default="InoProShop(V1.10.0.3)")
    parser.add_argument("--state", default=str(ROOT / "work/mcp_p0/state"))
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    state = Path(args.state).resolve(); state.mkdir(parents=True, exist_ok=True)
    project_root = ROOT / "work/mcp_p0/projects"; project_root.mkdir(parents=True, exist_ok=True)
    run_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    run_root = ROOT / "work/mcp_p0/native" / run_id; run_root.mkdir(parents=True)
    report_path = ROOT / "artifacts/mcp_p0/native" / run_id / "report.json"
    evidence_path = state / "native_evidence.json"
    token_path = state / "token"
    if not token_path.exists(): token_path.write_text(secrets.token_hex(32), encoding="utf-8")
    token = token_path.read_text(encoding="utf-8").strip()
    config = {"state_dir": str(state), "project_root": str(project_root), "test_owned": True,
              "native_evidence": str(evidence_path), "queue_timeout": args.timeout}
    config_path = run_root / "config.json"; atomic_json(config_path, config)
    entry = ROOT / "adapters/inoproshop/bridge/entry.py"
    wrapper = run_root / "run_host.py"
    wrapper.write_text("# encoding: utf-8\nsystem.delay(1500)\ntry:\n system.prompt_handling = getattr(PromptHandling, 'None')\nexcept Exception: pass\nINOPROSHOP_MCP_CONFIG = " + repr(str(config_path)) + "\n__file__ = " + repr(str(entry)) + "\nexecfile(__file__, globals())\n", encoding="utf-8")
    report = {"status": "running", "run_id": run_id, "checks": {}, "source": str(Path(args.source).resolve()),
              "scope": "owned native host, disposable copies, no online/PLC operations", "started_at": time.time()}
    original_hash = hashlib.sha256(Path(args.source).read_bytes()).hexdigest()
    proc, client, binding, watcher = None, None, None, None
    second_proc = second_client = second_binding = second_watcher = None
    def check(name, condition, detail=None):
        report["checks"][name] = {"status": "passed" if condition else "failed", "detail": detail}
        atomic_json(report_path, report)
        if not condition: raise AssertionError(name)
    def call(command, **params):
        nonlocal binding
        result = client.call(command, dict(expected_binding_id=binding, **params))
        binding = result.get("binding_id", binding)
        return result
    def snap(): return call("snapshot")
    def replace(node, declaration, implementation):
        return call("replace_text", object_id=node["id"], expected_node_hash=node_hash(node), expected_snapshot_hash=snap()["content_hash"], declaration=declaration, implementation=implementation)
    try:
        startup = subprocess.STARTUPINFO(); startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW; startup.wShowWindow = 0
        env = os.environ.copy(); env["__COMPAT_LAYER"] = "RunAsInvoker"
        proc = subprocess.Popen([args.exe, "--profile=" + args.profile, '--runscript=' + str(wrapper)],
                                cwd=str(Path(args.exe).parent), startupinfo=startup, env=env)
        watcher = DialogWatcher(proc.pid, run_root / 'startup_dialogs.json')
        report["owned_pid"] = proc.pid; atomic_json(report_path, report)
        deadline = time.monotonic() + args.timeout
        host = None
        while time.monotonic() < deadline:
            host = next((h for h in discover_hosts(state) if h["pid"] == proc.pid and h["available"]), None)
            if host: break
            if proc.poll() is not None: raise RuntimeError("Native host exited before bridge registration: " + str(proc.returncode))
            time.sleep(0.5)
        if not host: raise TimeoutError("Native bridge did not register; inspect bridge_startup_error.json.")
        binding = host["binding_id"]; client = PipeClient(host, token, timeout=args.timeout)
        report["host"] = host
        try:
            PipeClient(host, "invalid-token", timeout=10).call("capabilities", {"expected_binding_id": binding})
            raise AssertionError("bad token was accepted")
        except DomainError as exc: check("authentication", exc.code == "UNAUTHORIZED", exc.code)
        destination = project_root / ("Native_" + run_id + ".project")
        opened = call("open_project", source=args.source, destination=str(destination))
        check("open_project", Path(opened["path"]).resolve() == destination.resolve())
        snapshot = snap(); report["software"] = snapshot["software"]; report["bridge_version"] = snapshot["version"]
        health = call('capabilities')['runtime_health']
        check('script_returned_ui_available', health['script_entry_has_returned'] and health['ide_enabled'] and not health['invoke_required'] and health['modal_windows'] == 0, health)
        check("snapshot", bool(snapshot["nodes"]) and bool(snapshot["tasks"]), snapshot["coverage"])
        compile_result = call("compile", application_id=snapshot["active_application_id"])
        check("compile", compile_result["status"] == "passed", compile_result)
        caller = next(n for n in snapshot["nodes"] if n["name"] == "POU1" and n.get("implementation") is not None)
        replaced = replace(caller, caller["declaration"], caller["implementation"] + "\n// Native MCP verification")
        check("replace_text", replaced["readback"]["implementation"].endswith("// Native MCP verification"))
        try:
            call("replace_text", object_id=caller["id"], expected_node_hash=node_hash(caller), declaration=caller["declaration"], implementation=caller["implementation"])
            raise AssertionError("stale source accepted")
        except DomainError as exc: check("source_conflict", exc.code == "SOURCE_CONFLICT", exc.code)
        current = next(n for n in snap()["nodes"] if n["id"] == caller["id"])
        replace(current, caller["declaration"], caller["implementation"])
        name = "FB_Native_" + uuid.uuid4().hex[:10]
        declaration = "FUNCTION_BLOCK " + name + "\nVAR_INPUT\n x : BOOL;\nEND_VAR\nVAR_OUTPUT\n y : BOOL;\nEND_VAR"
        request_id = uuid.uuid4().hex
        create_params = dict(name=name, declaration=declaration, implementation="y := x;", application_id=snapshot["active_application_id"],
                             expected_snapshot_hash=snap()["content_hash"], command_id=request_id)
        created = call("import_st", **create_params)
        repeated = call("import_st", **create_params)
        check("import_st", created["object_id"] == repeated["object_id"], created)
        current_snapshot = snap()
        added = next(n for n in current_snapshot["nodes"] if n["id"] == created["object_id"])
        current = next(n for n in current_snapshot["nodes"] if n["id"] == caller["id"])
        report["checkpoint"] = {"caller": caller, "created_object_id": added["id"], "created_source": added}
        atomic_json(report_path, report)
        linked_declaration = current["declaration"].replace("END_VAR", " mcpVerify : " + name + ";\nEND_VAR", 1)
        linked_implementation = current["implementation"] + "\nmcpVerify(x := TRUE);"
        replace(current, linked_declaration, linked_implementation)
        linked_build = call("compile")
        check("create_st_pou", linked_build["status"] == "passed", linked_build)
        fb = next(n for n in snap()["nodes"] if n["id"] == added["id"])
        replace(fb, fb["declaration"], "MCP_UNDECLARED := 1;")
        failed = call("compile")
        check("created_target_compile_coverage", failed["status"] == "failed" and failed["errors"] > 0, failed)
        current = next(n for n in snap()["nodes"] if n["id"] == caller["id"])
        replace(current, caller["declaration"], caller["implementation"])
        latest = snap(); fb = next(n for n in latest["nodes"] if n["id"] == added["id"])
        deleted = call("delete_object", object_id=fb["id"], expected_node_hash=node_hash(fb), expected_snapshot_hash=latest["content_hash"])
        check("delete_object", deleted["deleted"] and not any(n["id"] == fb["id"] for n in snap()["nodes"]))
        restored = snap(); restored_caller = next(n for n in restored["nodes"] if n["id"] == caller["id"])
        restored_build = call("compile")
        check("two_object_recovery", node_hash(restored_caller) == node_hash(caller) and restored_build["status"] == "passed", restored_build)
        try:
            call("open_project", source=args.source, destination=str(project_root / ("DirtyReject_" + run_id + ".project")))
            raise AssertionError("dirty project closed")
        except DomainError as exc: check("dirty_project_preserved", exc.code == "UNSAVED_CHANGES", exc.code)
        saved_path = project_root / ("Saved_" + run_id + ".project")
        old_binding = binding
        saved = call("save_copy", destination=str(saved_path), expected_snapshot_hash=snap()["content_hash"])
        check("save_copy", saved["binding_changed"] and saved_path.is_file() and not snap()["dirty"], saved)
        try:
            client.call("snapshot", {"expected_binding_id": old_binding})
            raise AssertionError("old binding accepted")
        except DomainError as exc: check("binding_invalidation", exc.code == "HOST_BINDING_CHANGED", exc.code)
        reopened = project_root / ("Reopen_" + run_id + ".project")
        call("open_project", source=str(saved_path), destination=str(reopened))
        final = snap(); final_caller = next(n for n in final["nodes"] if n["name"] == caller["name"] and n.get("implementation") is not None)
        check("save_reopen", node_hash(final_caller) == node_hash(caller) and call("compile")["status"] == "passed")
        invalid_source = project_root / ('Corrupt_' + run_id + '.project')
        invalid_source.write_bytes(b'This is an intentionally invalid project fixture.')
        previous_path = snap()['project_path']
        call('save_copy', destination=previous_path, expected_snapshot_hash=snap()['content_hash'])
        try:
            call('open_project', source=str(invalid_source), destination=str(project_root / ('FailedOpen_' + run_id + '.project')))
            raise AssertionError('Corrupt project was opened.')
        except DomainError as exc:
            report['failed_open_error'] = {'code': exc.code, 'details': exc.details}
            binding = next(h for h in discover_hosts(state) if h['pid'] == proc.pid)['binding_id']
            check('failed_open_preserves_previous', exc.code == 'OPEN_FAILED' and Path(snap()['project_path']).resolve() == Path(previous_path).resolve(), exc.details)
        # A second owned IDE proves identical protocol tokens do not mix project bindings.
        second_proc = subprocess.Popen([args.exe, '--profile=' + args.profile, '--runscript=' + str(wrapper)],
                                       cwd=str(Path(args.exe).parent), startupinfo=startup, env=env)
        second_watcher = DialogWatcher(second_proc.pid, run_root / 'second_startup_dialogs.json')
        deadline = time.monotonic() + args.timeout
        second_host = None
        while time.monotonic() < deadline:
            second_host = next((h for h in discover_hosts(state) if h['pid'] == second_proc.pid and h['available']), None)
            if second_host: break
            if second_proc.poll() is not None: raise RuntimeError('Second owned host exited during startup.')
            time.sleep(0.5)
        if second_host is None: raise TimeoutError('Second host did not register.')
        second_client = PipeClient(second_host, token, timeout=args.timeout)
        second_binding = second_host['binding_id']
        second_opened = second_client.call('open_project', {'expected_binding_id': second_binding, 'source': args.source,
                                                            'destination': str(project_root / ('Second_' + run_id + '.project'))})
        second_binding = second_opened['binding_id']
        second_snapshot = second_client.call('snapshot', {'expected_binding_id': second_binding})
        try:
            second_client.call('snapshot', {'expected_binding_id': binding})
            raise AssertionError('Cross-host binding accepted.')
        except DomainError as exc:
            check('two_hosts_isolated', exc.code == 'HOST_BINDING_CHANGED' and second_snapshot['project_path'] != snap()['project_path'] and second_host['host_instance_id'] != host['host_instance_id'],
                  {'first_pid': proc.pid, 'second_pid': second_proc.pid, 'cross_binding_error': exc.code})
        check("source_unchanged", original_hash == hashlib.sha256(Path(args.source).read_bytes()).hexdigest(), original_hash)
        report["status"] = "passed"
        report["finished_at"] = time.time()
        atomic_json(report_path, report)
    except Exception as exc:
        report.update(status="failed", error=str(exc), traceback=traceback.format_exc(), finished_at=time.time())
        atomic_json(report_path, report)
    finally:
        if second_client and second_proc.poll() is None:
            second_client.timeout = 10
            try: second_client.call('shutdown', {'expected_binding_id': second_binding})
            except Exception as exc: report['second_shutdown_error'] = str(exc)
        if second_proc:
            try: second_proc.wait(timeout=20)
            except subprocess.TimeoutExpired: second_proc.terminate(); second_proc.wait(timeout=15)
            report['second_host_exit_code'] = second_proc.returncode
        if client and proc.poll() is None:
            client.timeout = 10
            try: call("shutdown")
            except Exception as exc: report["shutdown_error"] = str(exc)
        if proc:
            try: proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                # This PID was created by this runner. Preserve evidence before terminating it.
                report["owned_host_terminated_after_timeout"] = True
                proc.terminate()
                proc.wait(timeout=15)
            report["host_exit_code"] = proc.returncode
        if watcher: watcher.close()
        if second_watcher: second_watcher.close()
        if report.get('status') == 'passed' and (report.get('host_exit_code') != 0 or report.get('second_host_exit_code') != 0):
            report.update(status='failed', error='The native host did not shut down cleanly.')
        atomic_json(report_path, report)
        if report.get('status') == 'passed': atomic_json(evidence_path, report)
    print(json.dumps({"status": report['status'], "error": report.get('error'), "report": str(report_path),
                      "evidence": str(evidence_path) if report['status'] == 'passed' else None}, ensure_ascii=False))
    return 0 if report['status'] == 'passed' else 1


if __name__ == "__main__": raise SystemExit(main())
