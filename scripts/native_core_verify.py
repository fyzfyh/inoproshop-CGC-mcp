"""Real MCP HTTP -> Core -> named pipe -> native IDE acceptance.

Diagnostic matrix injection uses the private test adapter; compilation and
diagnostic reads use public MCP tools. Product edits/create/recovery use MCP.
"""
from __future__ import annotations
import argparse
import ast
import asyncio
import hashlib
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/inoproshop-mcp/src'))
from inoproshop_mcp.common import atomic_json, node_hash, DomainError
from inoproshop_mcp.transport import discover_hosts, PipeClient
from native_dialogs import DialogWatcher
from native_monitor_stage import run_monitor_stage
from native_am402_stage import run_am402_stage
from native_read_stage import run_read_stage
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def diagnostic_cases(declaration, implementation):
    tree = ast.parse((ROOT / 'demo/v2/verify_diagnostics.py').read_text(encoding='utf-8-sig'))
    assignment = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'cases' for t in n.targets))
    environment = {'declaration': declaration, 'implementation': implementation,
                   'extra_decl': lambda value: declaration.replace('END_VAR', value + '\nEND_VAR', 1)}
    return eval(compile(ast.Expression(assignment.value), '<trusted diagnostic case definitions>', 'eval'), {'__builtins__': {}}, environment)


def error_causes(error):
    if hasattr(error, 'exceptions'):
        return [cause for child in error.exceptions for cause in error_causes(child)]
    return [{'type': type(error).__name__, 'message': str(error), 'code': getattr(error, 'code', None)}]


async def run(args):
    run_id = time.strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8]
    run_root = ROOT / 'work/mcp_p0/native_core' / run_id; run_root.mkdir(parents=True)
    report_path = ROOT / 'artifacts/mcp_p0/native_core' / run_id / 'report.json'
    state = args.state.resolve(); token = (state / 'token').read_text(encoding='ascii').strip()
    project_root = ROOT / 'work/mcp_p0/projects'
    config_path = run_root / 'config.json'
    atomic_json(config_path, {'state_dir': str(state), 'project_root': str(project_root), 'test_owned': True,
                             'native_evidence': str(state / 'native_evidence.json'), 'queue_timeout': 180})
    entry = ROOT / 'adapters/inoproshop/bridge/entry.py'
    wrapper = run_root / 'run_host.py'
    open_source = args.source
    seed_report = run_root / 'fixture_seed.json'
    preflight = ''
    if not args.skip_model:
        seed_source = project_root / ('SyntheticSeed_' + run_id + '.project')
        shutil.copy2(args.source, seed_source)
        open_source = str(seed_source)
        preflight = ('INOPROSHOP_FIXTURE_SOURCE = ' + repr(str(seed_source)) + '\nINOPROSHOP_FIXTURE_REPORT = ' + repr(str(seed_report)) +
            '\n__file__ = ' + repr(str(ROOT / 'scripts/native_fixture_seed.py')) +
            '\ntry:\n execfile(__file__, globals())\nexcept Exception:\n system.exit()\n raise\n')
    if args.modal_check:
        preflight += ('INOPROSHOP_MODAL_PROBE_ROOT = ' + repr(str(run_root)) + '\n__file__ = ' +
                      repr(str(ROOT / 'scripts/native_modal_fixture.py')) + '\nexecfile(__file__, globals())\n')
    wrapper.write_text("# encoding: utf-8\nsystem.delay(1500)\ntry:\n system.prompt_handling = getattr(PromptHandling, 'None')\nexcept Exception: pass\n" + preflight + "INOPROSHOP_MCP_CONFIG = " + repr(str(config_path)) + "\n__file__ = " + repr(str(entry)) + "\nexecfile(__file__, globals())\n", encoding='utf-8')
    report = {'status': 'running', 'run_id': run_id, 'started_at': time.time(), 'checks': {}, 'diagnostics': [],
              'layer': 'official_MCP_HTTP_to_Core_to_named_pipe_to_native_IDE', 'model_invoked': False, 'extended_only': args.extended_only}
    proc = watcher = pipe = None; binding = None
    original_hash = hashlib.sha256(Path(args.source).read_bytes()).hexdigest()
    def check(name, condition, detail=None):
        report['checks'][name] = {'status': 'passed' if condition else 'failed', 'detail': detail}
        atomic_json(report_path, report)
        if not condition: raise AssertionError(name)
    def native(command, **params):
        nonlocal binding
        result = pipe.call(command, dict(params, expected_binding_id=binding))
        binding = result.get('binding_id', binding)
        return result
    def native_replace(node, declaration, implementation):
        current = native('snapshot')
        return native('replace_text', object_id=node['id'], expected_node_hash=node_hash(node),
                      expected_snapshot_hash=current['content_hash'], declaration=declaration, implementation=implementation)
    try:
        startup = subprocess.STARTUPINFO(); startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW; startup.wShowWindow = 0
        env = os.environ.copy(); env['__COMPAT_LAYER'] = 'RunAsInvoker'
        exe = r'D:\Inoproshop\CODESYS\Common\InoProShop.exe'
        proc = subprocess.Popen([exe, '--profile=InoProShop(V1.10.0.3)', '--runscript=' + str(wrapper)],
                                cwd=str(Path(exe).parent), startupinfo=startup, env=env)
        watcher = DialogWatcher(proc.pid, run_root / 'startup_dialogs.json')
        report['owned_pid'] = proc.pid; atomic_json(report_path, report)
        deadline = time.monotonic() + 180
        host = None
        while time.monotonic() < deadline:
            host = next((h for h in discover_hosts(state) if h['pid'] == proc.pid and h['available']), None)
            if host: break
            if proc.poll() is not None: raise RuntimeError('Owned host exited during startup.')
            await asyncio.sleep(0.5)
        if host is None: raise TimeoutError('Owned bridge did not register.')
        if not args.skip_model:
            seed = json.loads(seed_report.read_text(encoding='utf-8'))
            check('synthetic_fixture_native_preflight', seed['status'] == 'passed', seed)
        binding = host['binding_id']; pipe = PipeClient(host, token, timeout=180)
        if args.fixture_only:
            report.update(status='passed', layer='owned_native_fixture_preflight_only', finished_at=time.time())
            atomic_json(report_path, report)
            print(json.dumps({'status': 'passed', 'fixture_report': str(seed_report), 'report': str(report_path)}), flush=True)
            return report
        async with httpx.AsyncClient(headers={'Authorization': 'Bearer ' + token}, timeout=240, trust_env=False) as http:
            async with streamable_http_client(args.url, http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    async def tool(name, **params):
                        result = await session.call_tool(name, params)
                        data = result.structuredContent
                        if result.isError:
                            error = (data or {}).get('error', {})
                            raise DomainError(error.get('code', 'MCP_ERROR'), error.get('message', name), error.get('details'))
                        if not isinstance(data, dict): raise AssertionError('Missing structured tool result: ' + name)
                        return data
                    async def wait(operation):
                        deadline = time.monotonic() + 240
                        while time.monotonic() < deadline:
                            current = await tool('ino_get_operation', project_id=operation['project_id'], operation_id=operation['operation_id'])
                            if current['state'] in ('COMPLETED', 'FAILED', 'REJECTED', 'CANCELLED', 'RESTORED', 'OUTCOME_UNKNOWN', 'NEEDS_RECOVERY'):
                                return current
                            await asyncio.sleep(0.25)
                        raise TimeoutError('MCP operation did not finish.')
                    if args.am402_only:
                        project_id = await run_am402_stage(tool=tool, wait=wait, check=check, host=host,
                                                           project_root=project_root, run_id=run_id)
                        report.update(status='passed', project_id=project_id, final_project_id=project_id, finished_at=time.time())
                        atomic_json(report_path, report)
                        print(json.dumps({'status': 'passed', 'project_id': project_id, 'report': str(report_path), 'holding_seconds': args.hold_seconds}), flush=True)
                        if args.hold_seconds: await asyncio.sleep(args.hold_seconds)
                        return report
                    opened = await wait(await tool('ino_open_project', host_id=host['host_id'], host_instance_id=host['host_instance_id'],
                                        source=open_source, destination=str(project_root / ('MCP_' + run_id + '.project')), idempotency_key='open-' + run_id))
                    check('mcp_open_project', opened['state'] == 'COMPLETED', opened)
                    project_id = opened['project_id']; report['project_id'] = project_id
                    host = next(h for h in discover_hosts(state) if h['pid'] == proc.pid)
                    binding = host['binding_id']
                    async def capture(): return await tool('ino_capture_snapshot', project_id=project_id)
                    async def compile_mcp():
                        captured = await capture()
                        return await wait(await tool('ino_compile', project_id=project_id, expected_snapshot_id=captured['snapshot_id'], idempotency_key=uuid.uuid4().hex))
                    async def apply(change, mode='strict'):
                        captured = await capture()
                        indexed = await wait(await tool('context_index_snapshot', project_id=project_id, snapshot_id=captured['snapshot_id'], idempotency_key=uuid.uuid4().hex))
                        if indexed['state'] != 'COMPLETED':
                            raise DomainError('INDEX_FAILED', 'Native snapshot publication did not complete.', indexed)
                        preview = await tool('ino_preview_change', project_id=project_id, snapshot_id=captured['snapshot_id'], change=change, mode=mode)
                        auth = dict({key: preview[key] for key in ('project_id', 'patch_id', 'patch_hash')}, source='user_task')
                        operation = await tool('ino_apply_change', project_id=project_id, patch_id=preview['patch_id'], idempotency_key=uuid.uuid4().hex, authorization=auth)
                        return await wait(operation)
                    capabilities = await tool('ino_get_capabilities', project_id=project_id)
                    check('mcp_native_capability_gate', all(capabilities['host']['capabilities'][k]['status'] == 'supported' for k in ('create_st_pou', 'delete_object', 'two_object_recovery')), capabilities)
                    snapshot = await capture()
                    tree = await tool('ino_read_object', project_id=project_id, snapshot_id=snapshot['snapshot_id'], limit=500)
                    caller_id = next(n['id'] for n in tree['items'] if n['name'] == 'POU1')
                    readback = await tool('ino_read_object', project_id=project_id, snapshot_id=snapshot['snapshot_id'], object_id=caller_id)
                    check('mcp_snapshot_source_read', bool(readback['sources']['implementation']['items']), readback)
                    raw = native('snapshot'); base = next(n for n in raw['nodes'] if n['id'] == caller_id)
                    if args.modal_check:
                        from native_modal_stage import run_modal_stage
                        await run_modal_stage(native, check, state, proc.pid, run_root)
                    await run_read_stage(tool, wait, capture, check, project_id, caller_id)
                    if not args.extended_only:
                        edited = await apply({'type': 'replace_st_implementation', 'object_id': caller_id, 'implementation': base['implementation'] + '\n// MCP native edit'})
                        check('mcp_edit_compile', edited['state'] == 'COMPLETED' and edited['result']['compile_status'] == 'passed', edited)
                        after = await capture()
                        restore = await wait(await tool('ino_restore_checkpoint', project_id=project_id, checkpoint_id=edited['result']['checkpoint_id'], expected_snapshot_id=after['snapshot_id'], idempotency_key=uuid.uuid4().hex))
                        check('mcp_restore_checkpoint', restore['state'] == 'RESTORED', restore)
                        negative = await apply({'type': 'replace_st_implementation', 'object_id': caller_id, 'implementation': base['implementation'] + '\nMCP_UNKNOWN := 1;'})
                        check('mcp_failure_auto_restore', negative['state'] == 'RESTORED' and negative['result']['compile_status'] == 'failed', negative)
                        # Product creation uses a scalar INT interface explicitly bound to the sample's INT variable A.
                        name = 'FB_MCP_' + uuid.uuid4().hex[:8]
                        create_change = {'type': 'create_st_pou', 'name': name,
                            'declaration': 'FUNCTION_BLOCK ' + name + '\nVAR_INPUT\n x : INT;\nEND_VAR\nVAR_OUTPUT\n y : INT;\nEND_VAR',
                            'implementation': 'y := x;', 'caller': {'object_id': caller_id, 'instance_name': 'mcpCore_' + uuid.uuid4().hex[:6],
                                'inputs': {'x': 'A'}, 'outputs': {'y': 'A'}, 'condition': None, 'insert_after_line': len(base['implementation'].splitlines())}}
                        created = await apply(create_change)
                        check('mcp_create_and_attach', created['state'] == 'COMPLETED' and bool(created['result']['created_object_id']), created)
                        captured = await capture()
                        recovered = await wait(await tool('ino_restore_checkpoint', project_id=project_id, checkpoint_id=created['result']['checkpoint_id'], expected_snapshot_id=captured['snapshot_id'], idempotency_key=uuid.uuid4().hex))
                        check('mcp_two_object_restore', recovered['state'] == 'RESTORED' and not any(n['name'] == name for n in native('snapshot')['nodes']), recovered)
                        # Existing-error repair fixture, with failure seeded privately and actual repair through MCP.
                        current = next(n for n in native('snapshot')['nodes'] if n['id'] == caller_id)
                        native_replace(current, current['declaration'], base['implementation'] + '\nMCP_REPAIR_ERROR := 1;')
                        repair = await apply({'type': 'replace_st_implementation', 'object_id': caller_id, 'implementation': base['implementation']}, mode='repair')
                        check('mcp_bad_baseline_repair', repair['state'] == 'COMPLETED' and repair['result']['compile_status'] == 'passed', repair)
                        if not args.skip_diagnostics:
                            for identifier, title, declaration, implementation, expected in diagnostic_cases(base['declaration'], base['implementation']):
                                current = next(n for n in native('snapshot')['nodes'] if n['id'] == caller_id)
                                native_replace(current, declaration, implementation)
                                operation = await compile_mcp()
                                built = operation.get('result', {}).get('compile', {})
                                diagnoses = await tool('ino_get_diagnostics', project_id=project_id, operation_id=operation['operation_id'])
                                messages = [m for m in built.get('messages', []) if m['severity'].lower() in ('error', 'fatalerror', 'warning')]
                                current = next(n for n in native('snapshot')['nodes'] if n['id'] == caller_id)
                                native_replace(current, base['declaration'], base['implementation'])
                                recovery = await compile_mcp()
                                good = built.get('status') in ('passed', 'failed') and (expected == 'observe' or built['status'] == expected)
                                good = good and bool(diagnoses['items']) and all(m.get('location', {}).get('status') == 'located' for m in messages) and recovery.get('result', {}).get('compile_status') == 'passed'
                                case = {'id': identifier, 'title': title, 'expected': expected, 'status': 'passed' if good else 'failed',
                                        'compile': built, 'diagnostics': diagnoses, 'recovery_operation': recovery['operation_id']}
                                report['diagnostics'].append(case); atomic_json(report_path, report)
                                print(json.dumps({'case': identifier, 'status': case['status'], 'compile': built.get('status')}, ensure_ascii=False), flush=True)
                                if not good: raise AssertionError('Diagnostic matrix case ' + identifier)
                            check('mcp_17_diagnostics', len(report['diagnostics']) == 17)
                    if not args.skip_monitor:
                        latencies = []
                        for cycle in range(args.monitor_cycles):
                            latency = await run_monitor_stage(tool, wait, capture, native, native_replace, check, project_id, caller_id,
                                                              prefix=('cycle_' + str(cycle + 1) + '_') if args.monitor_cycles > 1 else '')
                            latencies.append(latency)
                        report['monitor_latency_seconds'] = latencies
                    if not args.skip_model:
                        from native_model_stage import run_model_stage
                        model_stage = await run_model_stage(tool, wait, apply, capture, native, native_replace, check,
                                                            project_id, run_id, report, report_path)
                        report['model_invoked'] = bool(model_stage.get('model_invoked_by_stage') or model_stage.get('live_omp', {}).get('model_invoked'))
                    if args.coverage_check and args.skip_model:
                        from native_coverage_stage import run_coverage_stage
                        report['native_coverage'] = await run_coverage_stage(tool, wait, apply, capture, native, native_replace, check, project_id, caller_id)
                    captured = await capture()
                    saved = await wait(await tool('ino_save_copy', project_id=project_id, expected_snapshot_id=captured['snapshot_id'], destination=str(project_root / ('MCP_Saved_' + run_id + '.project')), idempotency_key=uuid.uuid4().hex))
                    check('mcp_save_copy', saved['state'] == 'COMPLETED', saved)
                    binding = saved['result']['save']['binding_id']
                    check('source_unchanged', original_hash == hashlib.sha256(Path(args.source).read_bytes()).hexdigest(), original_hash)
                    if not args.skip_am402:
                        project_id = await run_am402_stage(tool=tool, wait=wait, check=check, host=host,
                                                           project_root=project_root, run_id=run_id)
                        report['final_project_id'] = project_id
                    report.update(status='passed', finished_at=time.time()); atomic_json(report_path, report)
                    print(json.dumps({'status': 'passed', 'project_id': project_id, 'report': str(report_path), 'holding_seconds': args.hold_seconds}), flush=True)
                    if args.hold_seconds: await asyncio.sleep(args.hold_seconds)
    except Exception as exc:
        report.update(status='failed', error=str(exc), error_code=getattr(exc, 'code', None),
                      error_causes=error_causes(exc), traceback=traceback.format_exc(), finished_at=time.time())
        if seed_report.exists():
            report['fixture_preflight'] = json.loads(seed_report.read_text(encoding='utf-8'))
        atomic_json(report_path, report)
        print(json.dumps({'status': 'failed', 'error': str(exc), 'report': str(report_path)}), flush=True)
    finally:
        if pipe and proc.poll() is None:
            pipe.timeout = 10
            try:
                latest = next(h for h in discover_hosts(state) if h['pid'] == proc.pid)
                binding = latest['binding_id']; native('shutdown')
            except Exception as exc: report['shutdown_error'] = str(exc)
        if proc:
            try: proc.wait(timeout=20)
            except subprocess.TimeoutExpired: proc.terminate(); proc.wait(timeout=15); report['owned_host_terminated_after_timeout'] = True
            report['host_exit_code'] = proc.returncode
            if report.get('status') == 'passed' and proc.returncode != 0:
                report.update(status='failed', error='Owned native host did not exit cleanly.')
        (run_root / 'stop').touch()
        if watcher: watcher.close()
        atomic_json(report_path, report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', type=Path, default=ROOT / 'work/mcp_p0/state')
    parser.add_argument('--url', default='http://127.0.0.1:8765/mcp')
    parser.add_argument('--source', default=r'D:\Inoproshop\CODESYS\Projects\Demo for AM600\002_Marquee(ST).project')
    parser.add_argument('--skip-diagnostics', action='store_true')
    parser.add_argument('--extended-only', action='store_true', help='Run export/monitor/models/AM402 after prior core and diagnostics evidence.')
    parser.add_argument('--skip-monitor', action='store_true')
    parser.add_argument('--monitor-cycles', type=int, choices=range(1, 6), default=1)
    parser.add_argument('--skip-model', action='store_true')
    parser.add_argument('--skip-am402', action='store_true')
    parser.add_argument('--am402-only', action='store_true', help='Start with an empty host and run the real AM402 repair stage directly.')
    parser.add_argument('--fixture-only', action='store_true', help='Validate private synthetic GVL/POU seeding and language probes without MCP mutation or model calls.')
    parser.add_argument('--modal-check', action='store_true')
    parser.add_argument('--coverage-check', action='store_true')
    parser.add_argument('--hold-seconds', type=int, default=0)
    arguments = parser.parse_args()
    if arguments.am402_only: arguments.skip_model = True
    if arguments.fixture_only: arguments.skip_model = False
    completed = asyncio.run(run(arguments))
    raise SystemExit(0 if completed['status'] == 'passed' else 1)
