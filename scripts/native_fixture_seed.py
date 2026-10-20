# encoding: utf-8
"""Private IronPython test preflight, before the production bridge is loaded.

The launcher provides a disposable source path; no public MCP capability is
extended and this script never connects to a PLC or a model.
"""
from __future__ import unicode_literals
import io
import json
import os
import traceback
import uuid
import xml.etree.ElementTree as ET
from xml.sax.saxutils import quoteattr


def inspect_language(obj):
    result = {'name': unicode(obj.get_name()), 'object_id': unicode(obj.guid)}
    for field in ('type', 'has_textual_implementation', 'embedded_object_types'):
        try:
            value = getattr(obj, field)
            result[field] = [unicode(x) for x in value] if field == 'embedded_object_types' else unicode(value)
        except Exception as exc: result[field + '_error'] = unicode(exc)
    result['object_members'] = [unicode(k) for k in dir(obj) if 'language' in k.lower() or 'implementation' in k.lower()]
    try:
        document = obj.textual_implementation
        result['document_members'] = [unicode(k) for k in dir(document)]
        try: result['document_type'] = unicode(document.GetType().FullName)
        except Exception as exc: result['document_type_error'] = unicode(exc)
    except Exception as exc: result['document_error'] = unicode(exc)
    try:
        xml = obj.export_xml(recursive=False)
        root = ET.fromstring(unicode(xml).encode('utf-8'))
        bodies = root.findall('.//{http://www.plcopen.org/xml/tc6_0200}pou/{http://www.plcopen.org/xml/tc6_0200}body')
        result['export_body_tags'] = [child.tag.rsplit('}', 1)[-1] for body in bodies for child in body]
    except Exception as exc: result['export_error'] = unicode(exc)
    return result


def probe_languages(project, app, st_object):
    report = {'ST': inspect_language(st_object), 'enumeration': {}}
    enumeration = globals().get('ImplementationLanguages')
    for field in ('st', 'instruction_list', 'ladder', 'fbd', 'cfc'):
        try: report['enumeration'][field] = unicode(getattr(enumeration, field))
        except Exception as exc: report['enumeration'][field] = {'error': unicode(exc)}
    for language in ('IL', 'LD'):
        name = 'MCP_Probe_' + language + '_' + uuid.uuid4().hex[:8]
        obj = None
        attempts = []
        try:
            try:
                obj = app.create_program(name, language=getattr(enumeration, 'instruction_list' if language == 'IL' else 'ladder'))
                attempts.append({'route': 'create_program_explicit_language', 'status': 'passed'})
            except Exception as exc:
                attempts.append({'route': 'create_program_explicit_language', 'error': unicode(exc)})
            body = '<IL><xhtml xmlns="http://www.w3.org/1999/xhtml">LD TRUE\nST probeValue</xhtml></IL>' if language == 'IL' else '<LD/>'
            xml = ('<?xml version="1.0" encoding="utf-8"?><project xmlns="http://www.plcopen.org/xml/tc6_0200">'
                   '<fileHeader companyName="Synthetic" productName="Fixture" productVersion="1" creationDateTime="2026-01-01T00:00:00Z"/>'
                   '<contentHeader name="Fixture"><coordinateInfo><fbd><scaling x="1" y="1"/></fbd><ld><scaling x="1" y="1"/></ld><sfc><scaling x="1" y="1"/></sfc></coordinateInfo></contentHeader>'
                   '<types><dataTypes/><pous><pou name=' + quoteattr(name) + ' pouType="program"><interface><localVars><variable name="probeValue"><type><BOOL/></type></variable></localVars></interface><body>' + body +
                   '</body></pou></pous></types><instances><configurations/></instances></project>')
            if obj is None:
                app.import_xml(dataOrPath=xml)
                found = list(project.find(name, True))
                if len(found) != 1: raise ValueError('Language probe did not yield one POU.')
                obj = found[0]
                attempts.append({'route': 'plcopen_' + language, 'status': 'passed'})
            report[language] = inspect_language(obj)
        except Exception as exc: report[language] = {'probe_error': unicode(exc)}
        finally:
            report[language]['attempts'] = attempts
            if obj is not None:
                obj.remove()
                report[language]['removed'] = not bool(list(project.find(name, True)))
    return report


def seed_fixture():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    source = os.path.realpath(INOPROSHOP_FIXTURE_SOURCE)
    allowed = os.path.realpath(os.path.join(root, 'work', 'mcp_p0', 'projects'))
    if not os.path.normcase(source).startswith(os.path.normcase(allowed + os.sep)):
        raise ValueError('Fixture setup must use the launcher-owned disposable copy.')
    with io.open(os.path.join(root, 'services', 'inoproshop-mcp', 'fixtures', 'evaluation', 'sources.json'), encoding='utf-8') as handle:
        fixtures = dict((n['id'], n) for n in json.load(handle)['nodes'])
    report = {'status': 'running', 'source': source, 'scope': 'private_synthetic_fixture_seed', 'objects': []}
    project = None
    try:
        project = projects.open(source)
        # Allow the OEM's device initialization to finish on its normal message
        # loop before this startup-only fixture project is saved and closed.
        system.delay(30000)
        app = project.active_application
        gvl = fixtures['gvl-settings']
        try:
            obj = app.create_gvl(gvl['name'])
            route = 'create_gvl'
        except AttributeError:
            # CODESYS exports GVLs as an application-level PLCopen addData item.
            xml = ('<?xml version="1.0" encoding="utf-8"?>'
                   '<project xmlns="http://www.plcopen.org/xml/tc6_0200">'
                   '<fileHeader companyName="Synthetic" productName="Fixture" productVersion="1" creationDateTime="2026-01-01T00:00:00Z"/>'
                   '<contentHeader name="Fixture"><coordinateInfo><fbd><scaling x="1" y="1"/></fbd><ld><scaling x="1" y="1"/></ld><sfc><scaling x="1" y="1"/></sfc></coordinateInfo></contentHeader>'
                   '<types><dataTypes/><pous/></types><instances><configurations/></instances>'
                   '<addData><data name="http://www.3s-software.com/plcopenxml/globalvars" handleUnknown="implementation">'
                   '<globalVars name=' + quoteattr(gvl['name']) + '><variable name="xEnabled"><type><BOOL/></type></variable></globalVars>'
                   '</data></addData></project>')
            app.import_xml(dataOrPath=xml)
            found = list(project.find(gvl['name'], True))
            if len(found) != 1: raise ValueError('GVL import did not produce exactly one fixture GVL.')
            obj, route = found[0], 'plcopen_globalvars'
        obj.textual_declaration.replace(new_text=gvl['declaration'])
        if unicode(obj.textual_declaration.text) != gvl['declaration']:
            raise ValueError('GVL source did not round-trip exactly.')
        report['objects'].append({'fixture_id': gvl['id'], 'object_id': unicode(obj.guid), 'route': route})
        spare = fixtures['program-spare']
        try:
            obj = app.create_program(spare['name'])
            route = 'create_program'
        except AttributeError:
            xml = ('<?xml version="1.0" encoding="utf-8"?>'
                   '<project xmlns="http://www.plcopen.org/xml/tc6_0200">'
                   '<fileHeader companyName="Synthetic" productName="Fixture" productVersion="1" creationDateTime="2026-01-01T00:00:00Z"/>'
                   '<contentHeader name="Fixture"><coordinateInfo><fbd><scaling x="1" y="1"/></fbd><ld><scaling x="1" y="1"/></ld><sfc><scaling x="1" y="1"/></sfc></coordinateInfo></contentHeader>'
                   '<types><dataTypes/><pous><pou name=' + quoteattr(spare['name']) + ' pouType="program"><interface/>'
                   '<body><ST><xhtml xmlns="http://www.w3.org/1999/xhtml"> </xhtml></ST></body></pou></pous></types>'
                   '<instances><configurations/></instances></project>')
            app.import_xml(dataOrPath=xml)
            found = list(project.find(spare['name'], True))
            if len(found) != 1: raise ValueError('Program import did not produce exactly one fixture POU.')
            obj, route = found[0], 'plcopen_program'
        obj.textual_declaration.replace(new_text=spare['declaration'])
        obj.textual_implementation.replace(new_text=spare['implementation'])
        if unicode(obj.textual_declaration.text) != spare['declaration'] or unicode(obj.textual_implementation.text) != spare['implementation']:
            raise ValueError('Spare POU did not round-trip exactly.')
        report['objects'].append({'fixture_id': spare['id'], 'object_id': unicode(obj.guid), 'route': route})
        report['language_probe'] = probe_languages(project, app, obj)
        project.save()
        report['status'] = 'passed'
    except Exception:
        report.update(status='failed', traceback=traceback.format_exc())
        raise
    finally:
        with io.open(INOPROSHOP_FIXTURE_REPORT, 'w', encoding='utf-8') as handle:
            handle.write(json.dumps(report, ensure_ascii=False, indent=2))
        if project is not None:
            project.close()


seed_fixture()
