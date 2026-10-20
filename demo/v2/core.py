# encoding: utf-8
"""Host-independent project context, retrieval, lint and change contracts.

Python 2.7/IronPython and Python 3 compatible. No model, PLC or IDE dependency.
"""
from __future__ import unicode_literals
import collections
import datetime
import difflib
import hashlib
import io
import json
import math
import os
import re


def utc():
    if hasattr(datetime, 'timezone'):
        return datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00', 'Z')
    return datetime.datetime.utcnow().isoformat() + 'Z'


def write_json(path, value):
    folder = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(folder):
        os.makedirs(folder)
    with io.open(path, 'w', encoding='utf-8') as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def read_json(path):
    with io.open(path, 'r', encoding='utf-8-sig') as handle:
        return json.load(handle)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True,
                                    separators=(',', ':')).encode('ascii')).hexdigest()


def node_hash(node):
    graphical = node.get('graphical_xml')
    if graphical:
        graphical = re.sub(r'(creationDateTime|modificationDateTime)="[^"]*"', r'\1="normalized"', graphical)
    return digest([node.get('declaration'), node.get('implementation'), graphical])


def snapshot_hash(nodes):
    return digest(sorted([(n['id'], n['path'], n.get('language'), node_hash(n)) for n in nodes]))


def require_inside(path, root):
    actual = os.path.normcase(os.path.realpath(os.path.abspath(path)))
    allowed = os.path.normcase(os.path.realpath(os.path.abspath(root)))
    if not actual.startswith(allowed + os.sep):
        raise ValueError('Target must be a file below the disposable work/v2 directory.')
    return actual


def tokens(text):
    # Identifier matches and Chinese bigrams, no downloadable tokenizer.
    values = []
    for word in re.findall(r'[A-Za-z_][A-Za-z_0-9]*|[\u4e00-\u9fff]+|\d+', text):
        values.append(word.lower())
        if re.match(r'[\u4e00-\u9fff]', word):
            values.extend(word[i:i + 2] for i in range(len(word) - 1))
        else:
            values.extend(v.lower() for v in re.findall(r'[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|\d+', word))
    return values


def make_index(snapshot, previous=None):
    """Replace full persisted index; changed/deleted chunks cannot survive refresh."""
    chunks = []
    for node in snapshot['nodes']:
        for section in ('declaration', 'implementation', 'graphical_xml'):
            content = node.get(section)
            if not content:
                continue
            lines = content.splitlines()
            for offset in range(0, len(lines), 45):
                end = min(offset + 50, len(lines))
                part = '\n'.join(lines[offset:end])
                chunks.append({
                    'id': node['id'] + ':' + section + ':' + str(offset + 1),
                    'node_id': node['id'], 'name': node['name'], 'path': node['path'],
                    'section': section, 'start_line': offset + 1, 'end_line': end,
                    'source_hash': node_hash(node), 'text': part,
                    'terms': tokens(node['path'] + '\n' + part),
                })
    old = dict((c['id'], digest(c['text'])) for c in (previous or {}).get('chunks', []))
    new = dict((c['id'], digest(c['text'])) for c in chunks)
    return {'schema': 2, 'retrieval': 'local_bm25_identifiers_no_embeddings',
            'project_path': snapshot['project_path'], 'snapshot_hash': snapshot['hash'],
            'built_at': utc(), 'chunks': chunks,
            'delta': {'added': len(set(new) - set(old)), 'deleted': len(set(old) - set(new)),
                      'changed': sum(new[k] != old[k] for k in set(new) & set(old))}}


def retrieve(index, query, limit=6):
    docs = index['chunks']
    wanted = set(tokens(query))
    if not wanted or not docs:
        return []
    avg = float(sum(len(c['terms']) for c in docs)) / len(docs)
    frequencies = collections.Counter()
    for chunk in docs:
        frequencies.update(set(chunk['terms']) & wanted)
    hits = []
    for chunk in docs:
        counts = collections.Counter(chunk['terms'])
        score = 0.0
        for term in wanted:
            count = counts.get(term, 0)
            if not count:
                continue
            idf = math.log(1 + (len(docs) - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
            score += idf * count * 2.2 / (count + 1.2 * (0.25 + 0.75 * len(chunk['terms']) / max(avg, 1)))
        if score > 0:
            row = dict((k, v) for k, v in chunk.items() if k != 'terms')
            row['score'] = round(score, 5)
            row['citation'] = '[{0} | {1}:{2}-{3}]'.format(chunk['path'], chunk['section'], chunk['start_line'], chunk['end_line'])
            hits.append(row)
    return sorted(hits, key=lambda h: (-h['score'], h['id']))[:limit]


def build_context(snapshot, index, query, target_id=None):
    if index['snapshot_hash'] != snapshot['hash'] or index['project_path'] != snapshot['project_path']:
        raise ValueError('Index is stale or belongs to another project; refresh it before using AI.')
    hits = retrieve(index, query)
    selected = next((n for n in snapshot['nodes'] if n['id'] == target_id), None)
    # Include explicit symbol references as additional evidence, never claim compiler-resolved edges.
    related = []
    if selected:
        referenced = set(tokens((selected.get('declaration') or '') + '\n' + (selected.get('implementation') or '')))
        for node in snapshot['nodes']:
            if node['id'] != selected['id'] and node['name'].lower() in referenced:
                related.append(node)
    return {'project_path': snapshot['project_path'], 'snapshot_hash': snapshot['hash'],
            'query': query, 'target': selected, 'hits': hits,
            'identifier_related_objects': related[:8],
            'relation_note': 'Lexical identifier matches, not a compiler-resolved call graph.',
            'coverage': snapshot.get('coverage', {})}


def proposal(snapshot, node_id, declaration, implementation, origin='manual'):
    current = next(n for n in snapshot['nodes'] if n['id'] == node_id)
    if current.get('implementation') is None:
        raise ValueError('Selected object has no textual implementation.')
    return {'schema': 1, 'project_path': snapshot['project_path'], 'node_id': node_id,
            'snapshot_hash': snapshot['hash'], 'base_hash': node_hash(current),
            'declaration': declaration, 'implementation': implementation,
            'origin': origin, 'created_at': utc()}


def validate_proposal(change, current, project_path):
    if change['project_path'] != project_path or change['node_id'] != current['id']:
        raise ValueError('Patch target does not match the current project/object.')
    if change['base_hash'] != node_hash(current):
        raise ValueError('Code changed after the proposal was created; refresh and regenerate the diff.')
    for key in ('declaration', 'implementation'):
        if not isinstance(change.get(key), type('')):
            # JSON in IronPython returns unicode; permit native str as well.
            if not isinstance(change.get(key), (str,)):
                raise ValueError('Patch requires text declaration and implementation.')
        if len(change[key]) > 200000:
            raise ValueError('Patch is larger than the Demo limit.')
    old_header = re.search(r'\b(PROGRAM|FUNCTION_BLOCK|FUNCTION)\s+(\w+)', current['declaration'], re.I)
    new_header = re.search(r'\b(PROGRAM|FUNCTION_BLOCK|FUNCTION)\s+(\w+)', change['declaration'], re.I)
    if not old_header or not new_header or old_header.group(0).lower() != new_header.group(0).lower():
        raise ValueError('V2 edits must retain the POU type and name.')


def change_diff(node, change):
    lines = []
    for section in ('declaration', 'implementation'):
        lines.extend(difflib.unified_diff((node.get(section) or '').splitlines(), change[section].splitlines(),
                     fromfile=node['path'] + '/' + section + ' (current)',
                     tofile=node['path'] + '/' + section + ' (proposed)', lineterm=''))
    return '\n'.join(lines) or '(No changes)'


def mask_comments_strings(text):
    # Preserve line/column offsets, including nested IEC block comments.
    out, i, depth = [], 0, 0
    while i < len(text):
        if text[i:i + 2] == '(*':
            depth += 1; out.extend('  '); i += 2
        elif depth and text[i:i + 2] == '*)':
            depth -= 1; out.extend('  '); i += 2
        elif depth:
            out.append('\n' if text[i] == '\n' else ' '); i += 1
        elif text[i:i + 2] == '//':
            end = text.find('\n', i)
            end = len(text) if end < 0 else end
            out.extend(' ' * (end - i)); i = end
        elif text[i] in ("'", '"'):
            quote = text[i]; out.append(' '); i += 1
            while i < len(text):
                ch = text[i]; out.append('\n' if ch == '\n' else ' '); i += 1
                if ch == '$' and i < len(text): out.append(' '); i += 1
                elif ch == quote: break
        else:
            out.append(text[i]); i += 1
    return ''.join(out)


DEFAULT_RULES = {'max_line_length': 120, 'forbidden_tokens': ['GOTO'], 'flag_todo': True}


def lint(snapshot, rules=None):
    rules = rules or DEFAULT_RULES
    findings = []
    for node in snapshot['nodes']:
        for section in ('declaration', 'implementation'):
            original = node.get(section) or ''
            masked = mask_comments_strings(original).splitlines()
            for number, line in enumerate(original.splitlines(), 1):
                codes = []
                if '\t' in line: codes.append(('STYLE_TABS', 'Use spaces instead of tabs.'))
                if len(line) > rules['max_line_length']: codes.append(('STYLE_LENGTH', 'Line exceeds configured length.'))
                if rules.get('flag_todo') and re.search(r'\bTODO\b', line, re.I): codes.append(('REVIEW_TODO', 'Unresolved TODO.'))
                visible = masked[number - 1] if number <= len(masked) else ''
                for word in rules.get('forbidden_tokens', []):
                    if re.search(r'\b' + re.escape(word) + r'\b', visible, re.I):
                        codes.append(('RULE_FORBIDDEN_TOKEN', 'Forbidden token: ' + word))
                for rule, message in codes:
                    findings.append({'node_id': node['id'], 'path': node['path'], 'section': section,
                                     'line': number, 'rule': rule, 'severity': 'warning', 'message': message})
    return {'scope': 'Configured lexical coding rules only; not syntax or functional correctness.',
            'snapshot_hash': snapshot['hash'], 'rules': rules, 'findings': findings}


class ChangeMonitor(object):
    """Stable-snapshot debounce. The host drives polls on its own UI thread."""
    def __init__(self, initial_hash, delay=2.0):
        self.last = initial_hash
        self.pending = None
        self.since = 0
        self.delay = delay

    def observe(self, fingerprint, now):
        if fingerprint == self.last:
            self.pending = None
            return False
        if fingerprint != self.pending:
            self.pending, self.since = fingerprint, now
            return False
        if now - self.since >= self.delay:
            self.last, self.pending = fingerprint, None
            return True
        return False
