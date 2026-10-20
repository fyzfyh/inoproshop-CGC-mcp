# encoding: utf-8
"""Small IronPython-compatible adapter. All methods execute on the IDE UI thread."""
from __future__ import unicode_literals
import hashlib
import io
import json
import os
import re
import shutil
import time
import uuid
from xml.sax.saxutils import escape, quoteattr

BUILD_CATEGORY = '97f48d64-a2a3-4856-b640-75c046e37ea9'


def text(value):
    try: return unicode(value)
    except NameError: return str(value)


def hash_value(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('ascii')).hexdigest()


def node_hash(node):
    return hash_value([node.get('declaration'), node.get('implementation')])


class Rejected(Exception):
    def __init__(self, code, message, details=None):
        Exception.__init__(self, message)
        self.code, self.message, self.details = code, message, details or {}


def inside(path, root):
    actual = os.path.normcase(os.path.realpath(os.path.abspath(path)))
    allowed = os.path.normcase(os.path.realpath(os.path.abspath(root)))
    if not actual.startswith(allowed + os.sep) or not actual.lower().endswith('.project'):
        raise Rejected('PATH_NOT_ALLOWED', 'Only .project copies beneath the configured project root may be changed.')
    return actual


class NativeAdapter(object):
    def __init__(self, projects, system, config, capture, locate):
        self.projects, self.system, self.config = projects, system, config
        self.capture, self.locate = capture, locate
        self.project = None
        self.project_path = None
        self.binding_id = uuid.uuid4().hex
        self.objects = {}
        source_fingerprints = {}
        for filename in ('adapter.py', 'entry.py'):
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
            if os.path.isfile(path):
                with open(path, 'rb') as handle: source_fingerprints[filename] = hashlib.sha256(handle.read()).hexdigest()
        self.version = {'bridge': '1.0', 'protocol': 1, 'sources': source_fingerprints}
        self.refresh_binding()

    def refresh_binding(self):
        current = self.projects.primary
        path = text(current.path) if current is not None else None
        same = current is self.project
        if not same and current is not None and self.project is not None:
            try: same = bool(current == self.project)
            except Exception: pass
        if not same or path != self.project_path:
            self.project, self.project_path = current, path
            self.binding_id = uuid.uuid4().hex
            self.objects = {}
        return self.binding_id

    def check(self, params, write=False, allow_empty=False):
        self.refresh_binding()
        if params.get('expected_binding_id') != self.binding_id:
            raise Rejected('HOST_BINDING_CHANGED', 'The IDE project binding changed; capture and bind again.', {'binding_id': self.binding_id})
        if self.project is None:
            if allow_empty: return
            raise Rejected('PROJECT_NOT_OPEN', 'No primary project is open.')
        if write:
            inside(self.project.path, self.config['project_root'])
            if not self.project.primary: raise Rejected('HOST_BINDING_CHANGED', 'Project is no longer primary.')

    def snapshot(self, params=None):
        if params is not None: self.check(params)
        self.refresh_binding()
        if self.project is None: raise Rejected('PROJECT_NOT_OPEN', 'No primary project is open.')
        result = self.capture(self.project)
        self.objects = {}
        def visit(parent):
            for obj in parent.get_children():
                self.objects[text(obj.guid)] = obj
                visit(obj)
        visit(self.project)
        for node in result['nodes']:
            if node.get('kind') != 'device': continue
            try:
                identity = self.objects[node['id']].get_device_identification()
                node['device_identity'] = {'status': 'read', 'type': int(identity.type),
                    'id': text(identity.id), 'version': text(identity.version),
                    'evidence': 'device_api.get_device_identification'}
            except Exception:
                node['device_identity'] = {'status': 'unavailable', 'reason': 'Native device identification is not exposed.'}
        result['content_hash'] = hash_value([result['content_hash'], result['nodes']])
        app = self.project.active_application
        result.update(binding_id=self.binding_id, dirty=bool(self.project.dirty), version=self.version,
                      active_application_id=text(app.guid) if app is not None else None,
                      tasks=[n for n in result['nodes'] if n.get('kind') == 'task'], editor_sync='unknown')
        result['bridge_snapshot_hash'] = result['content_hash']
        return result

    def expected_snapshot(self, params):
        current = self.snapshot()
        expected = params.get('expected_snapshot_hash')
        if not expected or expected != current['content_hash']:
            raise Rejected('SNAPSHOT_CONFLICT', 'Project changed since the checkpoint.', {'actual_snapshot_hash': current['content_hash']})
        return current

    def expected_node(self, params):
        current = self.snapshot()
        nodes = [n for n in current['nodes'] if n['id'] == params.get('object_id')]
        if len(nodes) != 1: raise Rejected('OBJECT_NOT_FOUND', 'The original object GUID is not present.')
        node = nodes[0]
        if not params.get('expected_node_hash') or node_hash(node) != params['expected_node_hash']:
            raise Rejected('SOURCE_CONFLICT', 'Object changed since the checkpoint.', {'actual_node_hash': node_hash(node)})
        return node

    def capabilities(self, params):
        self.check(params, allow_empty=True)
        evidence_path = self.config.get('native_evidence')
        evidence = {}
        if evidence_path and os.path.isfile(evidence_path):
            try:
                with io.open(evidence_path, encoding='utf-8-sig') as handle: evidence = json.load(handle)
            except Exception: pass
        if not isinstance(evidence, dict): evidence = {}
        checks = evidence.get('checks', {})
        if not isinstance(checks, dict) or any(not isinstance(value, dict) for value in checks.values()):
            evidence = {}
        software = self.capture(self.project).get('software') if self.project is not None else None
        verified = evidence.get('status') == 'passed' and evidence.get('bridge_version') == self.version and evidence.get('software') == software
        capabilities = {}
        for name in ('snapshot', 'compile', 'replace_text', 'import_st', 'create_st_pou', 'delete_object', 'save_copy', 'open_project', 'two_object_recovery'):
            capabilities[name] = {'status': 'supported' if verified and evidence.get('checks', {}).get(name, {}).get('status') == 'passed' else 'unknown',
                                  'reason': 'Native verification evidence' if verified else 'Requires verification on this OEM and bridge version.'}
        capabilities['graphical_readback'] = {'status': 'blocked', 'reason': 'Historical OEM XML export constructor failure; not revalidated.'}
        return {'binding_id': self.binding_id, 'version': self.version, 'capabilities': capabilities,
                'write_project_root': self.config['project_root'], 'test_owned': bool(self.config.get('test_owned'))}

    def compile(self, params):
        self.check(params)
        snapshot = self.expected_snapshot(params) if params.get('expected_snapshot_hash') else self.snapshot()
        app = self.project.active_application
        if app is None: raise Rejected('APPLICATION_REQUIRED', 'Select an active Application.')
        if params.get('application_id') and text(app.guid) != params['application_id']:
            raise Rejected('APPLICATION_MISMATCH', 'Requested application is not the active Application.')
        report = {'status': 'unknown', 'application_id': text(app.guid), 'scope': 'active_application_build',
                  'started_at': time.time(), 'messages': [], 'errors': None, 'warnings': None,
                  'source_snapshot': snapshot, 'snapshot_hash': snapshot['content_hash'], 'binding_id': self.binding_id,
                  'target_coverage': 'unknown'}
        try:
            report['native_stage'] = 'select_message_category'
            category = next(c for c in self.system.get_message_categories(False) if text(c).lower() == BUILD_CATEGORY)
            report['native_stage'] = 'clear_messages'
            self.system.clear_messages(category)
            report['native_stage'] = 'build'
            app.build()
            report['native_stage'] = 'read_messages'
            for message in self.system.get_message_objects(category):
                row = {'severity': text(message.severity), 'text': text(message.text)}
                for key in ('number', 'position', 'position_offset', 'position_text', 'length', 'object'):
                    try: value = getattr(message, key); row[key] = None if value is None else text(value)
                    except Exception: row[key] = None
                try: row['object_id'] = text(message.object.guid)
                except Exception: row['object_id'] = None
                if row['severity'].lower() in ('error', 'fatalerror', 'warning'):
                    row['location'] = self.locate(row, snapshot)
                report['messages'].append(row)
            errors = sum(m['severity'].lower() in ('error', 'fatalerror') for m in report['messages'])
            warnings = sum(m['severity'].lower() == 'warning' for m in report['messages'])
            summary = '\n'.join(m['text'] for m in report['messages'])
            complete = re.search(r'(?:编译完成|编译结束).*?(\d+)\s*错误.*?(\d+)\s*警告', summary)
            if complete is None: complete = re.search(r'(\d+)\s+errors?\s*[,;].*?(\d+)\s+warnings?', summary, re.I)
            if complete: errors, warnings = max(errors, int(complete.group(1))), max(warnings, int(complete.group(2)))
            report.update(errors=errors, warnings=warnings, completion_evidence=complete.group(0) if complete else None)
            if errors: report['status'] = 'failed'
            elif complete: report['status'] = 'passed'
            else: report['reason'] = 'No explicit compilation completion summary.'
        except Exception as exc:
            report.update(status='unknown', reason=text(exc))
            import traceback
            report['exception_trace'] = traceback.format_exc()
        report['finished_at'] = time.time()
        return report

    def replace_text(self, params):
        self.check(params, write=True)
        if params.get('expected_snapshot_hash'): self.expected_snapshot(params)
        node = self.expected_node(params)
        if node.get('implementation') is None or node.get('declaration') is None:
            raise Rejected('UNSUPPORTED_CAPABILITY', 'Only textual POU declaration and implementation can be replaced.')
        for field in ('declaration', 'implementation'):
            if not isinstance(params.get(field), type('')) or len(params[field]) > 200000:
                raise Rejected('INVALID_ARGUMENT', 'Text source is missing or exceeds 200000 characters.')
        obj = self.objects[node['id']]
        try:
            obj.textual_declaration.replace(new_text=params['declaration'])
            obj.textual_implementation.replace(new_text=params['implementation'])
            after = {'declaration': text(obj.textual_declaration.text), 'implementation': text(obj.textual_implementation.text)}
            if any(after[key] != params[key] for key in after):
                raise ValueError('Written source did not round-trip exactly.')
        except Exception as exc:
            raise Rejected('OPERATION_OUTCOME_UNKNOWN', text(exc), {'object_id': node['id'], 'requires_reconciliation': True})
        return {'object_id': node['id'], 'node_hash': node_hash(after), 'binding_id': self.binding_id, 'readback': after}

    def import_st(self, params):
        self.check(params, write=True)
        snapshot = self.expected_snapshot(params)
        name = params.get('name', '')
        if not re.match(r'^[^\W\d]\w{0,79}$', name, re.UNICODE):
            raise Rejected('INVALID_ARGUMENT', 'New FB names must be IEC identifiers of at most 80 characters.')
        if not re.match(r'^\s*FUNCTION_BLOCK\s+' + re.escape(name) + r'\s*(?:\n|$)', params.get('declaration', ''), re.I):
            raise Rejected('UNSUPPORTED_CAPABILITY', 'This creation route supports named ST FUNCTION_BLOCK only.')
        if any(n['name'].lower() == name.lower() for n in snapshot['nodes']):
            raise Rejected('NAME_CONFLICT', 'An object with this name already exists.')
        app_id = params.get('application_id')
        app = self.objects.get(app_id)
        if app is None or not any(n['id'] == app_id and n.get('kind') == 'application' for n in snapshot['nodes']):
            raise Rejected('APPLICATION_REQUIRED', 'Select a captured application GUID.')
        xml = ('<?xml version="1.0" encoding="utf-8"?>'
               '<project xmlns="http://www.plcopen.org/xml/tc6_0200">'
               '<fileHeader companyName="InoAI" productName="InoProShop MCP" productVersion="1" creationDateTime="2026-01-01T00:00:00Z"/>'
               '<contentHeader name="MCP"><coordinateInfo><fbd><scaling x="1" y="1"/></fbd><ld><scaling x="1" y="1"/></ld><sfc><scaling x="1" y="1"/></sfc></coordinateInfo></contentHeader>'
               '<types><dataTypes/><pous><pou name=' + quoteattr(name) + ' pouType="functionBlock"><interface/>'
               '<body><ST><xhtml xmlns="http://www.w3.org/1999/xhtml"> </xhtml></ST></body>'
               '</pou></pous></types><instances><configurations/></instances></project>')
        before = set(n['id'] for n in snapshot['nodes'])
        try:
            app.import_xml(dataOrPath=xml)
            fresh = self.snapshot()
            added = [n for n in fresh['nodes'] if n['id'] not in before and n['name'] == name and n.get('application_id') == app_id]
            if len(added) != 1: raise ValueError('Import did not produce exactly one expected FB.')
            result = self.replace_text(dict(params, object_id=added[0]['id'], expected_node_hash=node_hash(added[0]), expected_snapshot_hash=fresh['content_hash']))
            result.update(created=True, name=name, application_id=app_id, target_coverage='not_covered')
            return result
        except Exception as exc:
            try: ids = [n['id'] for n in self.snapshot()['nodes'] if n['id'] not in before]
            except Exception: ids = []
            raise Rejected('OPERATION_OUTCOME_UNKNOWN', text(exc), {'new_object_ids': ids, 'requires_reconciliation': True})

    def delete_object(self, params):
        self.check(params, write=True)
        if params.get('expected_snapshot_hash'): self.expected_snapshot(params)
        node = self.expected_node(params)
        if node.get('pou_type') != 'FUNCTION_BLOCK':
            raise Rejected('UNSUPPORTED_CAPABILITY', 'Compensation deletion is restricted to ST FUNCTION_BLOCK objects.')
        try:
            self.objects[node['id']].remove()
            if any(n['id'] == node['id'] for n in self.snapshot()['nodes']):
                raise ValueError('Deleted object is still present.')
        except Exception as exc:
            raise Rejected('OPERATION_OUTCOME_UNKNOWN', text(exc), {'object_id': node['id'], 'requires_reconciliation': True})
        return {'object_id': node['id'], 'deleted': True, 'binding_id': self.binding_id}

    def save_copy(self, params):
        self.check(params, write=True)
        self.expected_snapshot(params)
        destination = inside(params['destination'], self.config['project_root'])
        if os.path.exists(destination) and os.path.normcase(destination) != os.path.normcase(self.project_path):
            raise Rejected('PATH_EXISTS', 'Destination already exists; overwriting another project is not supported.')
        old_binding = self.binding_id
        try:
            if os.path.normcase(destination) == os.path.normcase(self.project_path): self.project.save()
            else: self.project.save_as(destination)
            self.refresh_binding()
            if os.path.normcase(self.project_path) != os.path.normcase(destination) or self.project.dirty:
                raise ValueError('Saved path or dirty state did not match the requested result.')
        except Exception as exc:
            raise Rejected('OPERATION_OUTCOME_UNKNOWN', text(exc), {'destination': destination, 'requires_reconciliation': True})
        return {'path': self.project_path, 'project_path': self.project_path, 'binding_changed': self.binding_id != old_binding, 'binding_id': self.binding_id, 'dirty': False}

    def open_project(self, params):
        self.check(params, allow_empty=True)
        if self.project is not None and self.project.dirty:
            raise Rejected('UNSAVED_CHANGES', 'Current project has unsaved changes and will not be closed.')
        source = os.path.realpath(os.path.abspath(params['source']))
        destination = inside(params['destination'], self.config['project_root'])
        if not source.lower().endswith('.project') or not os.path.isfile(source):
            raise Rejected('INVALID_ARGUMENT', 'Source must be an existing .project file.')
        if os.path.exists(destination): raise Rejected('PATH_EXISTS', 'Destination must be a new project copy.')
        shutil.copy2(source, destination)
        expected_sha = params.get('expected_source_sha256')
        if expected_sha:
            with open(destination, 'rb') as handle: actual_sha = hashlib.sha256(handle.read()).hexdigest()
            if actual_sha != expected_sha:
                raise Rejected('TEMPLATE_HASH_MISMATCH', 'Copied template bytes do not match the selected checksum; current IDE project was preserved.')
        previous = self.project_path
        try:
            if self.project is not None: self.project.close()
            self.projects.open(destination)
            self.refresh_binding()
            if self.project is None or os.path.normcase(self.project_path) != os.path.normcase(destination):
                raise ValueError('Requested project did not become the primary project.')
        except Exception as exc:
            recovery = {'previous_path': previous, 'status': 'not_required'}
            if previous:
                try:
                    self.projects.open(previous); self.refresh_binding()
                    recovery['status'] = 'restored' if self.project_path == previous else 'needs_recovery'
                except Exception as restore_error:
                    recovery.update(status='needs_recovery', error=text(restore_error))
            raise Rejected('OPEN_FAILED', text(exc), {'recovery': recovery, 'binding_id': self.binding_id})
        return {'path': self.project_path, 'project_path': self.project_path, 'binding_id': self.binding_id, 'binding_changed': True}

    def dispatch(self, command, params):
        if command not in ('capabilities', 'snapshot', 'compile', 'replace_text', 'import_st', 'delete_object', 'save_copy', 'open_project'):
            raise Rejected('UNSUPPORTED_CAPABILITY', 'Unknown bridge command: ' + text(command))
        return getattr(self, command)(params)
