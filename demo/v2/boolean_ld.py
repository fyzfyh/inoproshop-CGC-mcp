# encoding: utf-8
"""Strict, stateless Boolean ST <-> PLCopen LD. No eval; no AI conversion.

Supported: BOOL VAR_INPUT/VAR_OUTPUT, AND/OR/NOT, parentheses, one assignment
per output. Reject timers, state, aliases, initializers, calls, and output reads.
"""
from __future__ import unicode_literals
import itertools
import re
import xml.etree.ElementTree as ET
from core import mask_comments_strings, utc

NS = 'http://www.plcopen.org/xml/tc6_0200'
ET.register_namespace('', NS)
XHTML = 'http://www.w3.org/1999/xhtml'


def q(tag): return '{' + NS + '}' + tag
def local(tag): return tag.split('}')[-1]


def element(parent, tag, attrs=None, text=None):
    node = ET.SubElement(parent, q(tag), attrs or {})
    if text is not None: node.text = text
    return node


class Unsupported(ValueError): pass


class Parser(object):
    def __init__(self, source, inputs):
        self.parts = re.findall(r'[A-Za-z_][A-Za-z_0-9]*|\S', source)
        self.pos = 0
        self.inputs = dict((n.lower(), n) for n in inputs)

    def take(self, token):
        if self.pos < len(self.parts) and self.parts[self.pos].upper() == token:
            self.pos += 1
            return True
        return False

    def atom(self):
        if self.take('NOT'): return ('not', self.atom())
        if self.take('('):
            node = self.or_expr()
            if not self.take(')'): raise Unsupported('Missing closing parenthesis.')
            return node
        if self.pos >= len(self.parts): raise Unsupported('Expression is incomplete.')
        word = self.parts[self.pos]; self.pos += 1
        if word.lower() not in self.inputs:
            raise Unsupported('Only declared BOOL inputs may be read: ' + word)
        return ('var', self.inputs[word.lower()])

    def and_expr(self):
        node = self.atom()
        while self.take('AND'): node = ('and', node, self.atom())
        return node

    def or_expr(self):
        node = self.and_expr()
        while self.take('OR'): node = ('or', node, self.and_expr())
        return node

    def parse(self):
        node = self.or_expr()
        if self.pos != len(self.parts): raise Unsupported('Unsupported expression tokens: ' + ' '.join(self.parts[self.pos:]))
        return node


def parse_st(declaration, implementation):
    # Reject quoted values outright. Masking alone would turn expressions into empty tokens.
    if "'" in declaration or '"' in declaration: raise Unsupported('Strings are outside the Boolean subset.')
    clean = mask_comments_strings(declaration).strip()
    header = re.match(r'^(FUNCTION_BLOCK|PROGRAM)\s+([A-Za-z_]\w*)\s*', clean, re.I)
    if not header: raise Unsupported('Expected FUNCTION_BLOCK or PROGRAM declaration.')
    tail = clean[header.end():]
    blocks = list(re.finditer(r'\b(VAR_INPUT|VAR_OUTPUT)\b(.*?)\bEND_VAR\b', tail, re.I | re.S))
    remainder = re.sub(r'\b(VAR_INPUT|VAR_OUTPUT)\b.*?\bEND_VAR\b', '', tail, flags=re.I | re.S)
    if remainder.strip(): raise Unsupported('Only VAR_INPUT/VAR_OUTPUT BOOL declarations are supported.')
    inputs, outputs, seen = [], [], set()
    for block in blocks:
        destination = inputs if block.group(1).upper() == 'VAR_INPUT' else outputs
        for part in block.group(2).split(';'):
            if not part.strip(): continue
            match = re.match(r'^\s*([A-Za-z_]\w*)\s*:\s*BOOL\s*$', part, re.I)
            if not match: raise Unsupported('Each variable must be a plain BOOL without initializers or addresses.')
            name = match.group(1)
            if name.lower() in seen: raise Unsupported('Duplicate variable: ' + name)
            seen.add(name.lower()); destination.append(name)
    if not inputs or not outputs: raise Unsupported('At least one input and output are required.')
    if len(inputs) > 10: raise Unsupported('Truth-table verification is limited to 10 Boolean inputs.')
    body = mask_comments_strings(implementation)
    statements = list(re.finditer(r'([A-Za-z_]\w*)\s*:=\s*([^;]+);', body))
    if re.sub(r'([A-Za-z_]\w*)\s*:=\s*([^;]+);', '', body).strip():
        raise Unsupported('Only semicolon-terminated Boolean assignments are supported.')
    output_lookup = dict((v.lower(), v) for v in outputs)
    assigns = []
    assigned = set()
    for match in statements:
        key = match.group(1).lower()
        if key not in output_lookup or key in assigned: raise Unsupported('Each declared output must be assigned exactly once.')
        assigned.add(key)
        assigns.append((output_lookup[key], Parser(match.group(2), inputs).parse()))
    if assigned != set(output_lookup): raise Unsupported('All declared outputs must be assigned.')
    return {'name': header.group(2), 'pou_type': 'functionBlock' if header.group(1).upper() == 'FUNCTION_BLOCK' else 'program',
            'inputs': inputs, 'outputs': outputs, 'assignments': assigns}


def evaluate(expr, values):
    if expr[0] == 'var': return values[expr[1]]
    if expr[0] == 'not': return not evaluate(expr[1], values)
    if expr[0] == 'and': return evaluate(expr[1], values) and evaluate(expr[2], values)
    if expr[0] == 'or': return evaluate(expr[1], values) or evaluate(expr[2], values)
    raise Unsupported('Unknown expression node.')


def nnf(expr, negate=False):
    if expr[0] == 'var': return ('contact', expr[1], negate)
    if expr[0] == 'not': return nnf(expr[1], not negate)
    op = expr[0]
    if negate: op = 'or' if op == 'and' else 'and'
    return (op, nnf(expr[1], negate), nnf(expr[2], negate))


def to_xml(model, name=None):
    project = ET.Element(q('project'))
    element(project, 'fileHeader', {'companyName': 'InoAI Demo', 'productName': 'InoAI V2',
            'productVersion': '2.0', 'creationDateTime': utc()})
    header = element(project, 'contentHeader', {'name': name or model['name']})
    info = element(header, 'coordinateInfo')
    for language in ('fbd', 'ld', 'sfc'):
        element(element(info, language), 'scaling', {'x': '1', 'y': '1'})
    types = element(project, 'types'); element(types, 'dataTypes')
    pou = element(element(types, 'pous'), 'pou', {'name': name or model['name'], 'pouType': model['pou_type']})
    interface = element(pou, 'interface')
    for group, variables in [('inputVars', model['inputs']), ('outputVars', model['outputs'])]:
        dest = element(interface, group)
        for variable in variables:
            element(element(element(dest, 'variable', {'name': variable}), 'type'), 'BOOL')
    ld = element(element(pou, 'body'), 'LD')
    count = [0]
    def create(tag, x, y, attrs=None):
        count[0] += 1
        values = {'localId': str(count[0]), 'width': '60', 'height': '30'}
        values.update(attrs or {})
        obj = element(ld, tag, values)
        element(obj, 'position', {'x': str(x), 'y': str(y)})
        return obj, str(count[0])
    def incoming(obj, ids):
        point = element(obj, 'connectionPointIn')
        element(point, 'relPosition', {'x': '0', 'y': '15'})
        for identifier in ids: element(point, 'connection', {'refLocalId': identifier})
    def outgoing(obj):
        attrs = {'formalParameter': 'none'} if local(obj.tag) == 'leftPowerRail' else {}
        element(element(obj, 'connectionPointOut', attrs), 'relPosition', {'x': '60', 'y': '15'})
    def draw(expr, incoming_ids, x, y):
        if expr[0] == 'contact':
            obj, identifier = create('contact', x, y, {'negated': 'true' if expr[2] else 'false'})
            incoming(obj, incoming_ids); outgoing(obj); element(obj, 'variable', text=expr[1])
            return [identifier], x + 120, y + 70
        if expr[0] == 'and':
            left, x2, bottom = draw(expr[1], incoming_ids, x, y)
            right, x3, bottom2 = draw(expr[2], left, x2, y)
            return right, x3, max(bottom, bottom2)
        left, x2, bottom = draw(expr[1], incoming_ids, x, y)
        right, x3, bottom2 = draw(expr[2], incoming_ids, x, bottom)
        return left + right, max(x2, x3), bottom2
    y = 40
    for output, expression in model['assignments']:
        rail, rail_id = create('leftPowerRail', 0, y)
        outgoing(rail)
        ends, x, bottom = draw(nnf(expression), [rail_id], 100, y)
        coil, coil_id = create('coil', x, y, {'negated': 'false'})
        incoming(coil, ends); outgoing(coil); element(coil, 'variable', text=output)
        right, unused = create('rightPowerRail', x + 130, y)
        incoming(right, [coil_id]); y = bottom + 80
    element(element(project, 'instances'), 'configurations')
    return ET.tostring(project, encoding='utf-8').decode('utf-8')


def from_xml(xml):
    if '<!DOCTYPE' in xml.upper() or '<!ENTITY' in xml.upper(): raise Unsupported('XML entities are not accepted.')
    root = ET.fromstring(xml.encode('utf-8'))
    pous = root.findall('.//' + q('pou'))
    if len(pous) != 1: raise Unsupported('Select a PLCopen XML document containing exactly one POU.')
    pou = pous[0]
    if pou.get('pouType') not in ('functionBlock', 'program'): raise Unsupported('Unsupported POU type.')
    interface = pou.find(q('interface'))
    if interface is None: raise Unsupported('Missing variable interface.')
    inputs, outputs = [], []
    for group in interface:
        if local(group.tag) not in ('inputVars', 'outputVars'): raise Unsupported('Only Boolean input/output variables supported.')
        dest = inputs if local(group.tag) == 'inputVars' else outputs
        for variable in group:
            if variable.find(q('type') + '/' + q('BOOL')) is None or variable.find(q('initialValue')) is not None:
                raise Unsupported('Only plain BOOL variables supported.')
            dest.append(variable.get('name'))
    ld = pou.find(q('body') + '/' + q('LD'))
    if ld is None: raise Unsupported('The selected POU does not contain LD.')
    objects = {}
    for obj in ld:
        if local(obj.tag) not in ('leftPowerRail', 'rightPowerRail', 'contact', 'coil', 'comment'):
            raise Unsupported('Unsupported ladder element: ' + local(obj.tag))
        if local(obj.tag) == 'comment': continue
        identifier = obj.get('localId')
        if not identifier or identifier in objects: raise Unsupported('Invalid/duplicate LD localId.')
        objects[identifier] = obj
        for attr in ('edge', 'storage'):
            if obj.get(attr, 'none') != 'none': raise Unsupported('Stateful coils/contacts are outside the subset.')
    visiting = set()
    def combine(ids):
        if not ids: raise Unsupported('Disconnected ladder element.')
        terms = [upstream(identifier) for identifier in ids]
        if any(v is None for v in terms): return None  # energized left rail OR anything
        expr = terms[0]
        for value in terms[1:]: expr = ('or', expr, value)
        return expr
    def upstream(identifier):
        if identifier in visiting or identifier not in objects: raise Unsupported('Cyclic or missing LD connection.')
        visiting.add(identifier)
        obj = objects[identifier]; tag = local(obj.tag)
        if tag == 'leftPowerRail': value = None
        elif tag == 'contact':
            symbol = obj.findtext(q('variable'))
            if symbol not in inputs: raise Unsupported('Contact must refer to a declared input.')
            leaf = ('var', symbol)
            if obj.get('negated', 'false') == 'true': leaf = ('not', leaf)
            pred = combine([c.get('refLocalId') for c in obj.findall(q('connectionPointIn') + '/' + q('connection'))])
            value = leaf if pred is None else ('and', pred, leaf)
        else: raise Unsupported('Coils may not feed logic in this stateless subset.')
        visiting.remove(identifier)
        return value
    assignments = []
    for obj in ld:
        if local(obj.tag) == 'coil':
            symbol = obj.findtext(q('variable'))
            value = combine([c.get('refLocalId') for c in obj.findall(q('connectionPointIn') + '/' + q('connection'))])
            if value is None: raise Unsupported('Constant coils are outside the current subset.')
            if obj.get('negated', 'false') == 'true': value = ('not', value)
            assignments.append((symbol, value))
    model = {'name': pou.get('name'), 'pou_type': pou.get('pouType'), 'inputs': inputs,
             'outputs': outputs, 'assignments': assignments}
    declaration, implementation = to_st(model)
    return parse_st(declaration, implementation)  # Apply the same strict input/output checks.


def to_st_xml(model, name=None):
    """Create a standard textual POU via the proven XML importer, not optional create_* APIs."""
    root = ET.fromstring(to_xml(model, name).encode('utf-8'))
    pou = root.find('.//' + q('pou'))
    body = pou.find(q('body'))
    body.clear()
    st = element(body, 'ST')
    xhtml = ET.SubElement(st, '{' + XHTML + '}xhtml')
    xhtml.text = to_st(model, name)[1]
    return ET.tostring(root, encoding='utf-8').decode('utf-8')


def expression_text(expr):
    if expr[0] == 'var': return expr[1]
    if expr[0] == 'not': return 'NOT (' + expression_text(expr[1]) + ')'
    return '(' + expression_text(expr[1]) + ' ' + expr[0].upper() + ' ' + expression_text(expr[2]) + ')'


def to_st(model, name=None):
    declaration = ('FUNCTION_BLOCK ' if model['pou_type'] == 'functionBlock' else 'PROGRAM ') + (name or model['name'])
    for keyword, variables in [('VAR_INPUT', model['inputs']), ('VAR_OUTPUT', model['outputs'])]:
        declaration += '\n' + keyword + '\n' + '\n'.join('    ' + v + ' : BOOL;' for v in variables) + '\nEND_VAR'
    return declaration, '\n'.join(output + ' := ' + expression_text(expr) + ';' for output, expr in model['assignments'])


def equivalent(left, right):
    if left['inputs'] != right['inputs'] or set(left['outputs']) != set(right['outputs']):
        raise Unsupported('Input/output interfaces differ.')
    if len(left['inputs']) > 10: raise Unsupported('Too many Boolean inputs for exhaustive verification.')
    count = 0
    for combination in itertools.product((False, True), repeat=len(left['inputs'])):
        values = dict(zip(left['inputs'], combination))
        a = dict((output, evaluate(expr, values)) for output, expr in left['assignments'])
        b = dict((output, evaluate(expr, values)) for output, expr in right['assignments'])
        count += 1
        if a != b: return {'status': 'failed', 'inputs': values, 'left': a, 'right': b, 'cases': count}
    return {'status': 'passed', 'cases': count, 'scope': 'Exhaustive stateless Boolean truth table; no timing/state proof.'}
