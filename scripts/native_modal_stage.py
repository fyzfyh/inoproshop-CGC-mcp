"""Read-only command remains queued during an owned, automatically closed modal."""
import asyncio
import json
import time

from inoproshop_mcp.transport import discover_hosts


async def run_modal_stage(native, check, state, owned_pid, run_root):
    before = native('snapshot')
    (run_root / 'modal_trigger').touch()
    deadline = time.monotonic() + 15
    while not (run_root / 'modal_opened.json').exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    opened = json.loads((run_root / 'modal_opened.json').read_text(encoding='utf-8'))
    check('native_controlled_modal_opened', opened['modal'], opened)
    command = asyncio.create_task(asyncio.to_thread(native, 'snapshot'))
    waiting = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        host = next(h for h in discover_hosts(state) if h['pid'] == owned_pid)
        if host.get('waiting_reason') == 'IDE_MODAL_OR_DISABLED' and not command.done():
            waiting = {'waiting_reason': host['waiting_reason'], 'busy': host.get('busy'),
                       'request_still_pending': True, 'at': time.time()}
            break
        await asyncio.sleep(0.1)
    # Always wait for the same one request; never enqueue a replacement.
    result = await asyncio.wait_for(command, timeout=20)
    closed = json.loads((run_root / 'modal_closed.json').read_text(encoding='utf-8'))
    check('native_modal_queues_read_without_executing', waiting is not None and not waiting['busy'], waiting)
    check('native_modal_same_request_succeeds_after_close', result['content_hash'] == before['content_hash']
          and closed['at'] >= opened['at'] + 7, {'closed': closed, 'same_request': True, 'source_unchanged': True})
