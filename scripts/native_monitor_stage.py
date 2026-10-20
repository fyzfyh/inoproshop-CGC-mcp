"""Native fixture evidence for immutable export and monitor debounce."""
from __future__ import annotations
import asyncio
from datetime import datetime
import hashlib
import json
from pathlib import Path
import time
import uuid
import zipfile


async def run_monitor_stage(tool, wait, capture, native, native_replace, check, project_id, caller_id, prefix=''):
    original_check = check
    check = lambda name, condition, detail=None: original_check(prefix + name, condition, detail)
    before = native('snapshot')
    captured = await capture()
    exported = await tool('ino_export_context', project_id=project_id, snapshot_id=captured['snapshot_id'])
    root = Path(exported['directory'])
    sources = json.loads((root / 'source_map.json').read_text(encoding='utf-8'))
    objects = {n['id']: n for n in before['nodes']}
    with zipfile.ZipFile(exported['zip']) as archive:
        hashes = all(hashlib.sha256((root / name).read_bytes()).hexdigest() == value and
                     hashlib.sha256(archive.read(name)).hexdigest() == value
                     for name, value in exported['manifest'].items())
        exact_sources = all((root / item['file']).read_bytes().decode('utf-8') ==
                            objects[item['object_id']][item['section']] for item in sources)
    after = native('snapshot')
    check('mcp_native_export', hashes and exact_sources and before['content_hash'] == after['content_hash'] and
          before['dirty'] == after['dirty'], {'directory': str(root), 'zip': exported['zip'],
          'manifest_files': len(exported['manifest']), 'exact_source_sections': len(sources), 'dirty_unchanged': True})
    indexed = await wait(await tool('context_index_snapshot', project_id=project_id,
                                  snapshot_id=captured['snapshot_id'], idempotency_key=uuid.uuid4().hex))
    check('monitor_baseline_index', indexed['state'] == 'COMPLETED', indexed)
    base = objects[caller_id]
    monitor_enabled = False
    try:
        await tool('ino_set_monitor', project_id=project_id, enabled=True,
                   poll_seconds=0.5, debounce_seconds=10.0, auto_compile=False)
        monitor_enabled = True
        await asyncio.sleep(1.5)
        initial = await tool('ino_get_changes', project_id=project_id)
        cursor = initial['cursor']
        writes = []
        for number in range(3):
            current = next(n for n in native('snapshot')['nodes'] if n['id'] == caller_id)
            native_replace(current, current['declaration'], base['implementation'] + '\n// monitor burst ' + str(number))
            writes.append(time.time())
            await asyncio.sleep(0.15)
        events = []
        deadline = time.monotonic() + 90
        observed_at = None
        while time.monotonic() < deadline:
            page = await tool('ino_get_changes', project_id=project_id, after_cursor=cursor)
            events.extend(page['events']); cursor = page['cursor']
            if any(e.get('kind') == 'project_changed' for e in events):
                observed_at = time.time()
                break
            await asyncio.sleep(0.25)
        await asyncio.sleep(11)
        page = await tool('ino_get_changes', project_id=project_id, after_cursor=cursor)
        events.extend(page['events'])
        changed = [e for e in events if e.get('kind') == 'project_changed']
        exact_final_source = False
        latency = None
        if len(changed) == 1:
            observed = await tool('ino_read_object', project_id=project_id, snapshot_id=changed[0]['snapshot_id'],
                                  object_id=caller_id, section='implementation', limit=500)
            exact_final_source = observed['sources']['implementation']['items'] == (base['implementation'] + '\n// monitor burst 2').splitlines()
            latency = datetime.fromisoformat(changed[0]['at']).timestamp() - writes[-1]
        valid = len(changed) == 1 and changed[0]['index_status'] == 'ready' and exact_final_source and latency >= 9.0 and not any(e.get('kind') == 'monitor_error' for e in events)
        check('mcp_native_monitor_debounce', valid, {'burst_writes': len(writes), 'write_completed_at': writes,
              'configured_debounce_seconds': 10.0, 'event_matches_final_source': exact_final_source,
              'event_latency_after_last_write_seconds': latency,
              'observed_latency_after_last_write_seconds': observed_at - writes[-1] if observed_at else None,
              'events': events, 'unchanged_observation_seconds': 11, 'auto_compile': False})
        return latency
    finally:
        if monitor_enabled:
            await tool('ino_set_monitor', project_id=project_id, enabled=False,
                       poll_seconds=0.5, debounce_seconds=10.0, auto_compile=False)
        current = next(n for n in native('snapshot')['nodes'] if n['id'] == caller_id)
        if current['implementation'] != base['implementation']:
            native_replace(current, base['declaration'], base['implementation'])
