"""Public MCP acceptance of declarations, templates and text search on owned copies.

Uses the real CGC/Neo4j backend and native IDE. No model or online PLC calls.
First run native_verify.py with the same --state to establish bridge evidence.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/inoproshop-mcp/src'))
from inoproshop_mcp.__main__ import ensure_token, service_lock
from inoproshop_mcp.common import DomainError, atomic_json, node_hash
from inoproshop_mcp.core import Core
from inoproshop_mcp.index import reachable_objects
from inoproshop_mcp.launcher_database import ensure_database
from inoproshop_mcp.server import create_app
from inoproshop_mcp.transport import discover_hosts, PipeClient
from native_dialogs import DialogWatcher
import httpx
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def run(args):
    run_id = time.strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8]
    work = ROOT / 'work/mcp_p0/eng_extensions_20261020' / run_id
    work.mkdir(parents=True)
    report_path = ROOT / 'artifacts/declaration-template-search-20261020' / ('native-' + run_id + '.json')
    report = {'status': 'running', 'checks': {}, 'run_id': run_id,
        'layer': 'official_MCP_HTTP_to_Core_to_real_CGC_Neo4j_to_native_IDE',
        'model_invoked': False, 'online_PLC_operations': False, 'scope': 'owned IDE and disposable copies only'}
    report['source_sha256'] = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
        'services/inoproshop-mcp/src/inoproshop_mcp/core.py', 'services/inoproshop-mcp/src/inoproshop_mcp/changes.py',
        'services/inoproshop-mcp/src/inoproshop_mcp/templates.py', 'services/inoproshop-mcp/src/inoproshop_mcp/text_search.py',
        'services/inoproshop-mcp/src/inoproshop_mcp/server.py', 'services/inoproshop-mcp/src/inoproshop_mcp/store.py',
        'adapters/inoproshop/bridge/adapter.py', 'adapters/inoproshop/bridge/entry.py', 'scripts/native_engineering_extensions.py')}
    atomic_json(report_path, report)
    state, projects = args.state.resolve(), ROOT / 'work/mcp_p0/projects'
    source = Path(args.source).resolve()
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    core = server = thread = proc = watcher = None
    host = None
    preserve = False
    token = ensure_token(state)

    def check(name, passed, detail=None):
        report['checks'][name] = {'status': 'passed' if passed else 'failed', 'detail': detail}
        atomic_json(report_path, report)
        print(json.dumps({'check': name, 'status': report['checks'][name]['status']}, ensure_ascii=False), flush=True)
        if not passed:
            raise AssertionError(name)

    try:
        if not (state / 'native_evidence.json').is_file():
            raise RuntimeError('Run native_verify.py with this private state before native feature acceptance.')
        database = ensure_database(ROOT, ROOT / 'work/mcp_p0/neo4j', progress=lambda message: print(message, flush=True))
        if database['uri'] != 'bolt://127.0.0.1:17687':
            raise RuntimeError('Native acceptance requires the explicit local test database endpoint.')
        for key, field in [('NEO4J_URI', 'uri'), ('NEO4J_USERNAME', 'username'), ('NEO4J_PASSWORD', 'password')]:
            os.environ[key] = database[field]
        report['database'] = {'status': database['status'], 'reused': database['reused'], 'backend': 'real_CGC_Neo4j'}
        templates = work / 'templates'; templates.mkdir()
        core = Core(state, projects, template_root=templates)
        sock = socket.socket(); sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_app(core, token, port), log_level='error', access_log=False, proxy_headers=False))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True); thread.start()
        for _ in range(500):
            if server.started: break
            await asyncio.sleep(0.02)
        if not server.started: raise RuntimeError('Private acceptance HTTP server did not start.')
        config = work / 'bridge.json'
        atomic_json(config, {'state_dir': str(state), 'project_root': str(projects), 'test_owned': True,
                             'native_evidence': str(state / 'native_evidence.json'), 'queue_timeout': 180})
        entry = ROOT / 'adapters/inoproshop/bridge/entry.py'
        wrapper = work / 'run_host.py'
        wrapper.write_text("# encoding: utf-8\nsystem.delay(1500)\ntry:\n system.prompt_handling = getattr(PromptHandling, 'None')\nexcept Exception: pass\nINOPROSHOP_MCP_CONFIG = " + repr(str(config)) + "\n__file__ = " + repr(str(entry)) + "\nexecfile(__file__, globals())\n", encoding='utf-8')
        startup = subprocess.STARTUPINFO(); startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW; startup.wShowWindow = 0
        environment = dict(os.environ, __COMPAT_LAYER='RunAsInvoker')
        proc = subprocess.Popen([args.exe, '--profile=' + args.profile, '--runscript=' + str(wrapper)],
            cwd=str(Path(args.exe).parent), startupinfo=startup, env=environment)
        watcher = DialogWatcher(proc.pid, work / 'dialogs.json')
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            host = next((h for h in discover_hosts(state) if h['pid'] == proc.pid and h['available']), None)
            if host: break
            if proc.poll() is not None: raise RuntimeError('Owned IDE exited before registering: ' + str(proc.returncode))
            await asyncio.sleep(0.5)
        if host is None: raise TimeoutError('Owned native bridge did not register.')
        report['owned_pid'] = proc.pid
        async with httpx.AsyncClient(headers={'Authorization': 'Bearer ' + token}, trust_env=False, timeout=240) as http:
            async with streamable_http_client('http://127.0.0.1:' + str(port) + '/mcp', http_client=http) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    async def tool(name, **arguments):
                        result = await session.call_tool(name, arguments)
                        data = result.structuredContent or json.loads(result.content[0].text)
                        if result.isError:
                            error = data.get('error', {})
                            raise DomainError(error.get('code', 'MCP_ERROR'), error.get('message', 'Tool failed'), error.get('details'))
                        return data
                    async def wait(accepted):
                        deadline = time.monotonic() + 240
                        while time.monotonic() < deadline:
                            result = await tool('ino_get_operation', project_id=accepted['project_id'], operation_id=accepted['operation_id'])
                            if result['state'] in {'COMPLETED', 'FAILED', 'REJECTED', 'RESTORED', 'CANCELLED', 'OUTCOME_UNKNOWN', 'NEEDS_RECOVERY'}:
                                if result['state'] in {'OUTCOME_UNKNOWN', 'NEEDS_RECOVERY'}:
                                    raise DomainError('OPERATION_OUTCOME_UNKNOWN', 'Native operation needs inspection; fixture retained.', {'operation_id': result['operation_id']})
                                return result
                            await asyncio.sleep(0.2)
                        raise DomainError('OPERATION_OUTCOME_UNKNOWN', 'Native job did not finish; fixture retained.')
                    check('26_public_tools', len((await session.list_tools()).tools) == 26)
                    opened = await wait(await tool('ino_open_project', source=str(source), destination=str(projects / ('Eng_Inspect_' + run_id + '.project')),
                        host_id=host['host_id'], host_instance_id=host['host_instance_id'], idempotency_key='inspect-' + run_id))
                    check('reference_copy_opened', opened['state'] == 'COMPLETED')
                    inspection = opened['project_id']
                    captured = await tool('ino_capture_snapshot', project_id=inspection)
                    devices = await tool('ino_read_configuration', project_id=inspection, snapshot_id=captured['snapshot_id'], category='device')
                    raw = core._snapshot(inspection, captured['snapshot_id'])
                    root_ids = {n['id'] for n in raw['nodes'] if n.get('parent_id') is None}
                    roots = [n for n in devices['items'] if n['id'] in root_ids]
                    check('native_root_controller_read', len(roots) == 1 and roots[0].get('device_identity', {}).get('status') == 'read', roots)
                    expected = {key: roots[0]['device_identity'][key] for key in ('type', 'id', 'version')}
                    # Opening an older installed sample may make the disposable
                    # copy dirty. Normalize and save only that owned copy. A
                    # native build may add system objects; capture again rather
                    # than accepting a source-changing compile as a pass.
                    for attempt in range(2):
                        current = await tool('ino_capture_snapshot', project_id=inspection)
                        warmed = await wait(await tool('ino_compile', project_id=inspection,
                            expected_snapshot_id=current['snapshot_id'], idempotency_key='warm-' + str(attempt) + '-' + run_id))
                        if warmed['state'] == 'COMPLETED': break
                    check('reference_native_baseline_compile', warmed['state'] == 'COMPLETED', warmed)
                    current = await tool('ino_capture_snapshot', project_id=inspection)
                    normalized = await wait(await tool('ino_save_copy', project_id=inspection, expected_snapshot_id=current['snapshot_id'],
                        destination=str(projects / ('Eng_TemplateBaseline_' + run_id + '.project')), idempotency_key='normalize-' + run_id))
                    check('reference_normalized_copy_saved', normalized['state'] == 'COMPLETED')
                    reference = templates / 'reference.project'; shutil.copy2(normalized['result']['save']['path'], reference)
                    template_sha = hashlib.sha256(reference.read_bytes()).hexdigest()
                    correct = {'template_id': 'local-am600', 'name': 'Local installed AM600 sample', 'model': 'AM600 sample supplied with this IDE',
                        'project_file': reference.name, 'sha256': template_sha, 'controller': expected}
                    wrong = dict(correct, template_id='intentional-wrong-controller', controller=dict(expected, type=expected['type'] + 1))
                    atomic_json(templates / 'catalog.json', {'schema_version': '1.0', 'templates': [correct, wrong]})
                    catalog = await tool('ino_list_project_templates')
                    check('template_catalog_checksums', len(catalog['templates']) == 2 and all(t['status'] == 'ready' for t in catalog['templates']))
                    async def create(identifier, suffix):
                        selected = next(t for t in catalog['templates'] if t['template_id'] == identifier)
                        return await tool('ino_create_project', template_id=identifier, template_hash=selected['template_hash'],
                            destination=str(projects / (suffix + '_' + run_id + '.project')), host_id=host['host_id'],
                            host_instance_id=host['host_instance_id'], idempotency_key=suffix + '-' + run_id)
                    accepted = await create(correct['template_id'], 'Eng_Correct')
                    created = await wait(accepted)
                    project = created['project_id']
                    check('correct_controller_verified', created['state'] == 'COMPLETED' and created['result']['controller_verification']['status'] == 'passed', created['result'])
                    again = await create(correct['template_id'], 'Eng_Correct')
                    check('template_create_idempotent', again['deduplicated'] and again['operation_id'] == created['operation_id'])
                    async def capture(): return await tool('ino_capture_snapshot', project_id=project)
                    captured = await capture()
                    text = await tool('context_search_text', project_id=project, snapshot_id=captured['snapshot_id'], query='PROGRAM', section='declaration', limit=1)
                    check('native_text_search_without_graph', text['total'] > 0 and not text['graph_required'] and core._index_instance is None, text)
                    raw = core._snapshot(project, captured['snapshot_id'])
                    reachable = reachable_objects(raw)
                    target = next(n for n in raw['nodes'] if n['id'] in reachable and n.get('pou_type') == 'PROGRAM' and isinstance(n.get('implementation'), str))
                    baseline = {part: target[part] for part in ('declaration', 'implementation')}
                    declaration = baseline['declaration'].rstrip() + '\nVAR\n    inoDeclarationAcceptance : INT := 37;\nEND_VAR\n'
                    async def preview(decl):
                        current = await capture()
                        indexed = await wait(await tool('context_index_snapshot', project_id=project, snapshot_id=current['snapshot_id'], idempotency_key=uuid.uuid4().hex))
                        check('real_index_' + uuid.uuid4().hex[:6], indexed['state'] == 'COMPLETED', indexed.get('result', {}).get('backend'))
                        return await tool('ino_preview_change', project_id=project, snapshot_id=current['snapshot_id'],
                            change={'type': 'replace_st_sections', 'object_id': target['id'], 'declaration': decl})
                    async def apply(patch):
                        return await wait(await tool('ino_apply_change', project_id=project, patch_id=patch['patch_id'], idempotency_key=uuid.uuid4().hex,
                            authorization=dict({key: patch[key] for key in ('project_id', 'patch_id', 'patch_hash')}, source='user_task')))
                    def source_now():
                        record = next(h for h in discover_hosts(state) if h['pid'] == proc.pid)
                        result = PipeClient(record, token).call('snapshot', {'expected_binding_id': record['binding_id']})
                        return next(n for n in result['nodes'] if n['id'] == target['id'])
                    async def restore(operation):
                        current = await capture()
                        return await wait(await tool('ino_restore_checkpoint', project_id=project, checkpoint_id=operation['checkpoint_id'],
                            expected_snapshot_id=current['snapshot_id'], idempotency_key=uuid.uuid4().hex))
                    patch = await preview(declaration)
                    check('declaration_preview_preserves_body', bool(patch['changes'][0]['diff']['declaration']) and not patch['changes'][0]['diff']['implementation'])
                    applied = await apply(patch)
                    check('native_declaration_readback_compile', applied['state'] == 'COMPLETED' and source_now()['declaration'] == declaration and source_now()['implementation'] == baseline['implementation'], applied)
                    old = await tool('context_search_text', project_id=project, snapshot_id=captured['snapshot_id'], query='inoDeclarationAcceptance')
                    now = await capture()
                    new = await tool('context_search_text', project_id=project, snapshot_id=now['snapshot_id'], query='inoDeclarationAcceptance', object_id=target['id'])
                    check('text_search_snapshot_isolation', old['total'] == 0 and new['total'] == 1)
                    bad = await apply(await preview(declaration.replace('INT := 37;', 'INO_NONEXISTENT_TYPE;')))
                    diagnostics = await tool('ino_get_diagnostics', project_id=project, operation_id=bad['operation_id'])
                    check('native_bad_declaration_auto_restored', bad['state'] == 'RESTORED' and source_now()['declaration'] == declaration and
                        any(d['status'] == 'failed' for d in diagnostics['items']) and bad['result']['recovery']['compile']['status'] == 'passed', bad)
                    restored = await restore(applied)
                    check('native_checkpoint_restores_both_sections', restored['state'] == 'RESTORED' and node_hash(source_now()) == node_hash(baseline))
                    stale = await preview(declaration)
                    changed = await apply(await preview(declaration.replace('37', '38')))
                    rejected = await apply(stale)
                    check('native_stale_declaration_patch_rejected', changed['state'] == 'COMPLETED' and rejected['error']['code'] == 'STALE_SNAPSHOT')
                    check('native_stale_test_recovered', (await restore(changed))['state'] == 'RESTORED' and node_hash(source_now()) == node_hash(baseline))
                    current = await capture()
                    saved = await wait(await tool('ino_save_copy', project_id=project, expected_snapshot_id=current['snapshot_id'],
                        destination=str(projects / ('Eng_Saved_' + run_id + '.project')), idempotency_key='save-' + run_id))
                    check('native_saved_copy', saved['state'] == 'COMPLETED', saved)
                    reopened = await wait(await tool('ino_open_project', source=saved['result']['save']['path'],
                        destination=str(projects / ('Eng_Reopened_' + run_id + '.project')), host_id=host['host_id'],
                        host_instance_id=host['host_instance_id'], idempotency_key='reopen-' + run_id))
                    project = reopened['project_id']
                    check('native_save_reopen_same_source', reopened['state'] == 'COMPLETED' and node_hash(source_now()) == node_hash(baseline))
                    reopened_snapshot = await capture()
                    compiled = await wait(await tool('ino_compile', project_id=project,
                        expected_snapshot_id=reopened_snapshot['snapshot_id'], idempotency_key='compile-reopened-' + run_id))
                    check('native_reopened_compile', compiled['state'] == 'COMPLETED' and compiled['result']['compile_status'] == 'passed')
                    # Persist only this owned restored copy so changing projects
                    # cannot discard an unsaved compiler-generated object.
                    current = await capture()
                    before_negative = await wait(await tool('ino_save_copy', project_id=project, expected_snapshot_id=current['snapshot_id'],
                        destination=str(projects / ('Eng_BeforeNegative_' + run_id + '.project')), idempotency_key='before-negative-' + run_id))
                    check('owned_copy_saved_before_negative_template', before_negative['state'] == 'COMPLETED')
                    failed = await wait(await create(wrong['template_id'], 'Eng_Wrong'))
                    check('wrong_controller_rejected', failed['state'] == 'FAILED' and failed['error']['code'] == 'CONTROLLER_MISMATCH', failed)
                    wrong_snapshot = await tool('ino_capture_snapshot', project_id=failed['project_id'])
                    blocked = await wait(await tool('ino_save_copy', project_id=failed['project_id'], expected_snapshot_id=wrong_snapshot['snapshot_id'],
                        destination=str(projects / ('Eng_Blocked_' + run_id + '.project')), idempotency_key='blocked-' + run_id))
                    check('unverified_copy_is_not_writable', blocked['state'] != 'COMPLETED' and blocked['error']['code'] == 'CONTROLLER_NOT_VERIFIED')
                    check('vendor_reference_unchanged', hashlib.sha256(source.read_bytes()).hexdigest() == original_hash)
                    report.update(status='passed', reference_sha256=original_hash, template_sha256=template_sha, template_source=str(reference), controller=expected,
                        bridge_version=next(h for h in discover_hosts(state) if h['pid'] == proc.pid)['version'])
    except Exception as exc:
        def leaves(error):
            return [leaf for child in error.exceptions for leaf in leaves(child)] if hasattr(error, 'exceptions') else [error]
        errors = leaves(exc)
        preserve = any(getattr(error, 'code', None) in {'OPERATION_OUTCOME_UNKNOWN', 'NEEDS_RECOVERY'} for error in errors)
        report.update(status='failed', error=str(exc), error_code=getattr(exc, 'code', None),
            error_causes=[{'type': type(error).__name__, 'code': getattr(error, 'code', None), 'message': str(error)} for error in errors],
            traceback=traceback.format_exc(), fixture_retained=preserve)
    finally:
        if proc is not None and proc.poll() is None and not preserve:
            try:
                record = next(h for h in discover_hosts(state) if h['pid'] == proc.pid)
                PipeClient(record, token, timeout=15).call('shutdown', {'expected_binding_id': record['binding_id']})
            except Exception as exc: report['shutdown_error'] = type(exc).__name__
            try: proc.wait(timeout=25)
            except subprocess.TimeoutExpired:
                proc.terminate(); proc.wait(timeout=15); report['owned_host_terminated_after_timeout'] = True
        if proc is not None: report['host_exit_code'] = proc.poll()
        if watcher is not None: watcher.close()
        if server is not None: server.should_exit = True
        if thread is not None: thread.join(timeout=10)
        if core is not None: core.close()
        if report['status'] == 'passed' and report.get('host_exit_code') != 0:
            report.update(status='failed', error='Owned host did not exit cleanly.')
        report['finished_at'] = time.time()
        atomic_json(report_path, report)
    print(json.dumps({'status': report['status'], 'report': str(report_path), 'error': report.get('error')}, ensure_ascii=False), flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', type=Path, default=ROOT / 'work/mcp_p0/eng_extensions_20261020/state')
    parser.add_argument('--source', default=r'D:\Inoproshop\CODESYS\Projects\Demo for AM600\002_Marquee(ST).project')
    parser.add_argument('--exe', default=r'D:\Inoproshop\CODESYS\Common\InoProShop.exe')
    parser.add_argument('--profile', default='InoProShop(V1.10.0.3)')
    args = parser.parse_args()
    with service_lock(args.state):
        result = asyncio.run(run(args))
    raise SystemExit(0 if result['status'] == 'passed' else 1)
