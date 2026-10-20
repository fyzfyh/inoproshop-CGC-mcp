# encoding: utf-8
"""Real OEM ScriptEngine adapter. All calls run on the host's primary UI thread."""
from __future__ import unicode_literals
import os
import re
import uuid
import io
from core import (utc, digest, node_hash, snapshot_hash, write_json, require_inside,
                  validate_proposal)
from diagnostics import locate, diagnostic_messages

BUILD_CATEGORY = '97f48d64-a2a3-4856-b640-75c046e37ea9'

try:
    from scriptengine import ExportReporter
except ImportError:
    ExportReporter = object


class XmlReporter(ExportReporter):
    def __init__(self): self.errors = []
    def error(self, obj, message): self.errors.append(text(message))
    def warning(self, obj, message): pass
    def nonexportable(self, obj): self.errors.append('Nonexportable object: ' + object_name(obj))
    @property
    def aborting(self): return False


def native_xml(obj, destination):
    folder = os.path.dirname(destination)
    if not os.path.isdir(folder): os.makedirs(folder)
    reporter = XmlReporter()
    obj.export_xml(reporter, destination, recursive=False)
    if reporter.errors: raise ValueError('; '.join(reporter.errors))
    with io.open(destination, 'r', encoding='utf-8-sig') as handle: return handle.read()


def text(value):
    try: return unicode(value)
    except NameError: return str(value)


def get(obj, name, default=None):
    try: return getattr(obj, name)
    except Exception: return default


def object_name(obj):
    try: return text(obj.get_name())
    except Exception: return text(get(obj, 'name', '<unnamed>'))


class Host(object):
    def __init__(self, project, system, work_root, output):
        self.project, self.system = project, system
        self.work_root, self.output = work_root, output
        self.objects = {}
        self.last_backup = None
        self.last_compile = None
        self.last_transaction = None
        self.audit = []

    def record(self, action, status, detail=None):
        self.audit.append({'at': utc(), 'action': action, 'status': status, 'detail': detail})
        write_json(os.path.join(self.output, 'audit.json'), self.audit)

    def snapshot(self, persist=True):
        nodes, issues, self.objects = [], [], {}
        def walk(parent, path):
            for obj in parent.get_children():
                name = object_name(obj)
                full = path + '/' + name
                identifier = text(obj.guid)
                row = {'id': identifier, 'name': name, 'path': full,
                       'declaration': None, 'implementation': None, 'graphical_xml': None,
                       'language': 'non_text_object', 'is_application': bool(get(obj, 'is_application', False)),
                       'is_device': bool(get(obj, 'is_device', False))}
                self.objects[identifier] = obj
                for marker, section in [('has_textual_declaration', 'declaration'), ('has_textual_implementation', 'implementation')]:
                    if get(obj, marker, False):
                        try: row[section] = text(getattr(obj, 'textual_' + section).text)
                        except Exception as exc: issues.append({'id': identifier, 'section': section, 'error': text(exc)})
                if row['implementation'] is not None:
                    row['language'] = 'textual_implementation'
                elif row['declaration'] is not None:
                    row['language'] = 'declaration_only'
                    try:
                        xml = native_xml(obj, os.path.join(self.output, 'xml_cache', identifier + '.xml'))
                        if xml and re.search(r'<(?:\w+:)?(?:LD|FBD|SFC)\b', text(xml)):
                            row['graphical_xml'] = text(xml)
                            row['language'] = 'graphical_plcopen_xml'
                    except Exception as exc:
                        issues.append({'id': identifier, 'section': 'optional_xml_export', 'error': text(exc)})
                nodes.append(row)
                walk(obj, full)
        path = text(self.project.path)
        walk(self.project, os.path.basename(path))
        result = {'schema': 2, 'project_path': path, 'captured_at': utc(), 'nodes': nodes,
                  'hash': snapshot_hash(nodes), 'issues': issues,
                  'coverage': {'node_count': len(nodes),
                    'declarations': sum(n['declaration'] is not None for n in nodes),
                    'text_implementations': sum(n['implementation'] is not None for n in nodes),
                    'graphical_exports': sum(n['graphical_xml'] is not None for n in nodes),
                    'not_extracted': ['compiler-resolved symbol/call graph', 'binary/encrypted library bodies',
                                      'complete OEM device parameters and task settings'],
                    'relations': 'Hierarchy is exact; identifier dependency matches are lexical hints.'}}
        if persist: write_json(os.path.join(self.output, 'project_snapshot.json'), result)
        return result

    def safe(self):
        require_inside(text(self.project.path), self.work_root)
        if not self.project.primary: raise ValueError('The test project is no longer the primary project.')

    def application(self):
        app = self.project.active_application
        if app is None: raise ValueError('No active Application; select one in InoProShop.')
        return app

    def compile(self):
        self.safe()
        app = self.application()
        report = {'at': utc(), 'application': object_name(app), 'status': 'unknown',
                  'messages': [], 'errors': None, 'warnings': None, 'scope': 'active_application_build'}
        try:
            # Capture before Build: later edits or automatic rollback must not replace
            # the source against which a diagnostic was produced.
            try:
                source_snapshot = self.snapshot(False)
                report['project_path'] = source_snapshot['project_path']
                report['snapshot_hash'] = source_snapshot['hash']
            except Exception as exc:
                source_snapshot = {'nodes': []}
                report['source_capture_error'] = text(exc)
            # The category was observed on this exact installed OEM, not inferred from UI labels.
            category = next(c for c in self.system.get_message_categories(False) if text(c).lower() == BUILD_CATEGORY)
            self.system.clear_messages(category)
            app.build()
            raw = list(self.system.get_message_objects(category))
            for message in raw:
                row = {'severity': text(message.severity), 'text': text(message.text)}
                for attr in ('number', 'position', 'position_offset', 'position_text', 'length', 'object'):
                    value = get(message, attr)
                    row[attr] = None if value is None else text(value)
                native_object = get(message, 'object')
                identifier = get(native_object, 'guid')
                row['object_id'] = text(identifier) if identifier is not None else None
                if row['severity'].lower() in ('error', 'fatalerror', 'warning'):
                    row['location'] = locate(row, source_snapshot)
                report['messages'].append(row)
            errors = sum(m['severity'].lower() in ('error', 'fatalerror') for m in report['messages'])
            warnings = sum(m['severity'].lower() == 'warning' for m in report['messages'])
            summary = '\n'.join(m['text'] for m in report['messages'])
            complete = re.search(r'(?:编译完成|编译结束).*?(\d+)\s*错误.*?(\d+)\s*警告', summary)
            if complete is None: complete = re.search(r'(\d+)\s+errors?\s*[,;].*?(\d+)\s+warnings?', summary, re.I)
            if complete:
                errors = max(errors, int(complete.group(1)))
                warnings = max(warnings, int(complete.group(2)))
            report['errors'], report['warnings'] = errors, warnings
            if errors: report['status'] = 'failed'
            elif complete: report['status'] = 'passed'
            else: report['reason'] = 'No explicit compiler completion summary; do not infer success from an empty message list.'
        except Exception as exc:
            report['status'], report['error'] = 'failed', text(exc)
        diagnostics = diagnostic_messages(report)
        report['location_coverage'] = {'diagnostics': len(diagnostics),
            'located': sum(m.get('location', {}).get('status') == 'located' for m in diagnostics)}
        self.last_compile = report
        write_json(os.path.join(self.output, 'last_compile.json'), report)
        self.record('compile', report['status'], {'errors': report['errors'], 'warnings': report['warnings']})
        return report

    def apply(self, change):
        self.safe()
        snapshot = self.snapshot(False)
        current = next(n for n in snapshot['nodes'] if n['id'] == change['node_id'])
        validate_proposal(change, current, snapshot['project_path'])
        backup = {'project_path': snapshot['project_path'], 'node': current,
                  'change': change, 'created_at': utc(), 'status': 'prepared'}
        backup_path = os.path.join(self.output, 'backups', uuid.uuid4().hex + '.json')
        write_json(backup_path, backup)
        obj = self.objects[current['id']]
        try:
            obj.textual_declaration.replace(new_text=change['declaration'])
            obj.textual_implementation.replace(new_text=change['implementation'])
            if text(obj.textual_declaration.text) != change['declaration'] or text(obj.textual_implementation.text) != change['implementation']:
                raise ValueError('Write/readback mismatch.')
            backup['after_hash'] = node_hash(dict(current, declaration=change['declaration'], implementation=change['implementation']))
            backup['status'] = 'applied'
            self.last_backup = backup
            self.record('apply_patch', 'passed', {'node': current['path'], 'backup': backup_path, 'origin': change['origin']})
        except Exception:
            obj.textual_declaration.replace(new_text=current['declaration'])
            obj.textual_implementation.replace(new_text=current['implementation'])
            backup['status'] = 'restored_after_write_error'
            raise
        finally: write_json(backup_path, backup)
        return backup_path

    def rollback(self):
        self.safe()
        if not self.last_backup: raise ValueError('No reversible patch in this session.')
        backup = self.last_backup
        snapshot = self.snapshot(False)
        current = next(n for n in snapshot['nodes'] if n['id'] == backup['node']['id'])
        if snapshot['project_path'] != backup['project_path'] or node_hash(current) != backup['after_hash']:
            raise ValueError('Code changed again; automatic rollback would overwrite a later edit.')
        obj = self.objects[current['id']]
        obj.textual_declaration.replace(new_text=backup['node']['declaration'])
        obj.textual_implementation.replace(new_text=backup['node']['implementation'])
        if text(obj.textual_implementation.text) != backup['node']['implementation']:
            raise ValueError('Rollback readback mismatch.')
        self.last_backup = None
        self.record('rollback', 'passed', current['path'])

    def apply_and_compile(self, change):
        self.safe()
        baseline = self.compile()
        if baseline['status'] != 'passed':
            return {'status': 'blocked', 'reason': 'Baseline must compile successfully first.', 'baseline': baseline}
        backup = self.apply(change)
        built = self.compile()
        result = {'status': built['status'], 'compile': built, 'backup': backup,
                  'node_id': change['node_id'], 'project_path': change['project_path'], 'candidate': change}
        if built['status'] != 'passed':
            self.rollback()
            result['rollback_compile'] = self.compile()
            result['rolled_back'] = True
        else: result['rolled_back'] = False
        self.last_transaction = result
        write_json(os.path.join(self.output, 'last_transaction.json'), result)
        return result

    def export_object(self, node_id, destination):
        obj = self.objects[node_id]
        xml = native_xml(obj, destination)
        self.record('export_plcopen_xml', 'passed', destination)
        return xml

    def import_pou(self, xml, name, language):
        self.safe()
        self.snapshot(False)
        if any(object_name(obj).lower() == name.lower() for obj in self.objects.values()):
            raise ValueError('The POU name already exists; choose a new name.')
        self.application().import_xml(dataOrPath=xml)
        snapshot = self.snapshot()
        added = [n for n in snapshot['nodes'] if n['name'] == name]
        if len(added) != 1: raise ValueError('Import did not yield exactly one expected POU.')
        self.record('import_' + language.lower(), 'passed', name)
        return added[0]

    def import_ld(self, xml, name, language='LD'):
        return self.import_pou(xml, name, language)
