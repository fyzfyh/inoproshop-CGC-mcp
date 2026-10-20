# encoding: utf-8
"""Compiler positions mapped to the source captured for that build (Py2/Py3)."""
from __future__ import unicode_literals
import re
from core import node_hash


def locate(message, snapshot, context_lines=2):
    result = {'status': 'unavailable', 'reason': 'Compiler did not provide a supported textual position.'}
    position = message.get('position_text') or ''
    # position/position_offset are OEM internal values, NOT source line numbers.
    chinese = re.search(r'(\d+)\s*行\s*[,，]\s*(\d+)\s*列', position)
    english = re.search(r'line\s*(\d+)\s*[,;]\s*(?:col(?:umn)?)\s*(\d+)', position, re.I)
    match = chinese or english
    line_only = re.search(r'(\d+)\s*行', position) or re.search(r'line\s*(\d+)', position, re.I)
    section_match = re.search(r'\((Impl|Decl)\)\s*$', position, re.I)
    if not (match or line_only) or not section_match:
        return result
    section = 'implementation' if section_match.group(1).lower() == 'impl' else 'declaration'
    line = int((match or line_only).group(1))
    column = int(match.group(2)) if match else None
    identifier = message.get('object_id')
    if not identifier:
        guid = re.search(r'guid=([0-9a-f-]{36})', message.get('object') or '', re.I)
        identifier = guid.group(1) if guid else None
    nodes = [n for n in snapshot.get('nodes', []) if n['id'].lower() == (identifier or '').lower()]
    result.update(section=section, line=line, column=column, node_id=identifier)
    if len(nodes) != 1:
        result['reason'] = 'Compiler object GUID is absent from the build snapshot.'
        return result
    node = nodes[0]
    result.update(path=node['path'], name=node['name'], source_hash=node_hash(node))
    source = node.get(section)
    lines = source.split('\n') if source is not None else []
    lines = [value.rstrip('\r') for value in lines]
    if not 1 <= line <= len(lines) or (column is not None and not 1 <= column <= len(lines[line - 1]) + 1):
        result['reason'] = 'Compiler coordinates are outside the captured source.'
        return result
    fragment = re.search(r'@\s*(.*?)\s*\((?:Impl|Decl)\)\s*$', position, re.I)
    if fragment and fragment.group(1).strip() not in lines[line - 1].strip():
        result['reason'] = 'Compiler excerpt does not match the captured source line.'
        return result
    start, end = max(1, line - context_lines), min(len(lines), line + context_lines)
    result.update(status='located', reason=None, source_line=lines[line - 1],
                  start_line=start, end_line=end,
                  snippet='\n'.join(lines[start - 1:end]),
                  lines=[{'line': number, 'text': lines[number - 1], 'error_line': number == line}
                         for number in range(start, end + 1)],
                  source_version='captured_before_build',
                  precision='line_and_column' if column is not None else 'line_only',
                  location_basis='compiler_position_text_and_object_guid')
    return result


def diagnostic_messages(report):
    return [m for m in report.get('messages', [])
            if m.get('severity', '').lower() in ('error', 'fatalerror', 'warning')]


def format_location(message):
    location = message.get('location') or {}
    if location.get('status') != 'located':
        return '暂不能映射到源码片段：' + location.get('reason', '编译器未提供位置。') + '\n' + (message.get('position_text') or '')
    section = '实现区 ST' if location['section'] == 'implementation' else '声明区'
    coordinate = '第 {0} 行'.format(location['line'])
    coordinate += '，第 {0} 列（编译器报告）'.format(location['column']) if location['column'] is not None else '（编译器未提供列号）'
    header = '{0}\n{1} · {2}\n以下为本轮编译时的源码；回滚或继续编辑后仍保留此版本。'.format(
        location['path'], section, coordinate)
    rows = ['{0} {1:>4} | {2}'.format('>' if row['error_line'] else ' ', row['line'], row['text'])
            for row in location['lines']]
    return header + '\n\n' + '\n'.join(rows)


def format_compile(report):
    labels = {'passed': '编译通过', 'failed': '编译失败', 'unknown': '结果未知'}
    rows = ['{0} · 错误 {1} · 警告 {2} · 应用 {3}'.format(
        labels.get(report.get('status'), report.get('status')), report.get('errors'),
        report.get('warnings'), report.get('application', ''))]
    if report.get('error'): rows.append(report['error'])
    if report.get('reason'): rows.append(report['reason'])
    for message in diagnostic_messages(report):
        rows.append('[{0} #{1}] {2}\n{3}'.format(message['severity'], message.get('number') or '?',
            message['text'], message.get('position_text') or '编译器未提供文本位置'))
    if not diagnostic_messages(report):
        rows.extend(m['text'] for m in report.get('messages', []))
    return '\n\n'.join(rows)
