"""Public MCP read, pagination and real graph provenance evidence."""
import uuid


async def run_read_stage(tool, wait, capture, check, project_id, caller_id):
    captured = await capture()
    base = dict(project_id=project_id, snapshot_id=captured['snapshot_id'])
    all_rows = await tool('ino_read_object', **base, limit=500)
    rows, offset, pages = [], 0, 0
    while True:
        page = await tool('ino_read_object', **base, offset=offset, limit=17)
        rows.extend(page['items']); pages += 1
        if page['next_offset'] is None: break
        offset = page['next_offset']
    check('mcp_native_tree_pagination', rows == all_rows['items'] and len(rows) == all_rows['total'],
          {'pages': pages, 'page_size': 17, 'total': len(rows), 'duplicate_ids': len(rows) - len({n['id'] for n in rows})})
    configurations = {}
    for category in ('task', 'device', 'library', 'io'):
        configurations[category] = await tool('ino_read_configuration', **base, category=category)
    check('mcp_native_configuration_metadata', all(v['coverage'] == 'available_metadata_only' for v in configurations.values()), configurations)
    indexed = await wait(await tool('context_index_snapshot', **base, idempotency_key=uuid.uuid4().hex))
    check('mcp_native_read_graph_index', indexed['state'] == 'COMPLETED', indexed)
    revision = indexed['result']['index_revision']
    caller = next(row for row in rows if row['id'] == caller_id)
    searched = await tool('context_search', **base, query=caller['name'], limit=100, index_revision=revision)
    symbol = next((row for row in searched['items'] if row.get('match_type') == 'symbol' and row.get('object_id') == caller_id and row.get('kind') == 'POU'), None)
    check('mcp_native_graph_search_provenance', symbol is not None and all(
        row.get('source', {}).get('snapshot_id') == captured['snapshot_id'] and row.get('source', {}).get('object_id')
        for row in searched['items']), searched)
    references = await tool('context_find_references', **base, symbol_id=symbol['id'], index_revision=revision, limit=100)
    impact = await tool('context_get_impact', **base, object_id=caller_id, index_revision=revision, limit=100)
    check('mcp_native_graph_reference_impact_provenance', bool(references['items']) and all(
        edge.get('evidence', {}).get('snapshot_id') == captured['snapshot_id'] for edge in references['items'] + impact['evidence']),
        {'references': references, 'impact': impact})
