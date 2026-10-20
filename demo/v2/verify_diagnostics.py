# encoding: utf-8
"""Native error detection matrix; runs only in a fresh offline project copy."""
from __future__ import unicode_literals
import hashlib, os, sys, shutil, traceback, uuid
HERE = os.path.dirname(os.path.abspath(globals().get('__file__', sys.argv[0])))
sys.path.insert(0, HERE)
from core import utc, write_json, proposal, node_hash
from host import Host, text
from diagnostics import diagnostic_messages, format_location
from boolean_ld import parse_st, to_st_xml

ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
WORK = os.path.join(ROOT, 'work', 'v2')
OUT = os.path.join(ROOT, 'artifacts', 'v2', 'diagnostics_' + uuid.uuid4().hex[:10])
SOURCE = r'D:\Inoproshop\CODESYS\Projects\Demo for AM600\002_Marquee(ST).project'
report = {'started_at': utc(), 'status': 'running', 'output': OUT, 'cases': [],
          'scope': 'Native offline active Application Build; no PLC execution or download.'}
project = None


def checkpoint(stage):
    report['stage'] = stage
    write_json(os.path.join(OUT, 'report.json'), report)
    write_json(os.path.join(ROOT, 'artifacts', 'v2', 'latest_diagnostics.json'), report)


def file_hash(path):
    with open(path, 'rb') as handle: return hashlib.sha256(handle.read()).hexdigest()


try:
    checkpoint('opening_disposable_copy')
    if not os.path.isdir(WORK): os.makedirs(WORK)
    target = os.path.join(WORK, 'Diagnostics_' + uuid.uuid4().hex[:8] + '.project')
    original_hash = file_hash(SOURCE)
    shutil.copy2(SOURCE, target)
    report['project'] = target
    system.delay(1500)
    try: system.prompt_handling = getattr(PromptHandling, 'None')
    except Exception: pass
    project = projects.open(target)
    bridge = Host(project, system, WORK, OUT)
    snap = bridge.snapshot()
    base = next(n for n in snap['nodes'] if n['name'] == 'POU1' and n['implementation'] is not None)
    report['baseline'] = bridge.compile()
    assert report['baseline']['status'] == 'passed', 'Baseline failed.'
    declaration, implementation = base['declaration'], base['implementation']

    def extra_decl(value): return declaration.replace('END_VAR', value + '\nEND_VAR', 1)

    cases = [
        ('undeclared_variable', '未声明变量', declaration, implementation + '\nINOAI_UNDECLARED := 1;', 'failed'),
        ('missing_expression', '赋值表达式缺失', declaration, implementation + '\nA := ;', 'failed'),
        ('type_mismatch', '字符串赋给整数', declaration, implementation + "\nA := 'not_an_integer';", 'failed'),
        ('duplicate_declaration', '重复声明变量', extra_decl('A : INT;'), implementation, 'failed'),
        ('unknown_type', '不存在的数据类型', extra_decl('inoBad : INOAI_MISSING_TYPE;'), implementation, 'failed'),
        ('non_boolean_condition', 'IF 条件使用整数', declaration, implementation + '\nIF 123 THEN A := 1; END_IF;', 'observe'),
        ('unknown_function', '不存在的函数', declaration, implementation + '\nA := INOAI_NO_FUNCTION(1);', 'failed'),
        ('missing_argument', '函数缺少参数', declaration, implementation + '\nA := ABS();', 'failed'),
        ('constant_array_bounds', '数组常量下标越界', extra_decl('inoArray : ARRAY[0..2] OF INT;'), implementation + '\nA := inoArray[9];', 'observe'),
        ('constant_divide_zero', '常量除零', declaration, implementation + '\nA := 10 / 0;', 'observe'),
        ('byte_overflow', 'BYTE 常量超出范围', declaration, implementation + '\nb := 300;', 'observe'),
        ('implicit_narrowing', 'DINT 变量赋给 INT', extra_decl('inoWide : DINT;'), implementation + '\nA := inoWide;', 'observe'),
        ('multiple_errors', '同一程序多处错误', declaration, implementation + "\nINOAI_UNDECLARED := 1;\nA := 'bad';", 'failed'),
        ('unicode_tab_location', '中文注释与制表符后的定位', declaration, implementation + '\n// 中文注释用于检查定位\n\tINOAI_UNDECLARED := 1;', 'failed'),
        ('logic_wrong_period', '周期数值写错但语法合法', declaration, implementation.replace('1000/20', '2000/20'), 'passed'),
        ('dynamic_divide_zero', '变量除数可能为零', extra_decl('inoDivisor : INT;'), implementation + '\nA := A / inoDivisor;', 'observe'),
        ('dynamic_array_bounds', '数组变量下标可能越界', extra_decl('inoArray : ARRAY[0..2] OF INT;\ninoIndex : INT;'), implementation + '\nA := inoArray[inoIndex];', 'observe'),
    ]
    for identifier, title, decl, impl, expected in cases:
        checkpoint(identifier)
        snap = bridge.snapshot(False)
        change = proposal(snap, base['id'], decl, impl, 'compiler_diagnostics_matrix')
        transaction = bridge.apply_and_compile(change)
        built = transaction.get('compile', transaction.get('baseline', {}))
        if bridge.last_backup:
            bridge.rollback()
            recovery = bridge.compile()
        else:
            recovery = transaction.get('rollback_compile')
        restored = next(n for n in bridge.snapshot(False)['nodes'] if n['id'] == base['id'])
        messages = diagnostic_messages(built)
        item = {'id': identifier, 'title': title, 'expected': expected,
                'compile': built, 'candidate': change, 'recovery': recovery,
                'source_restored': node_hash(restored) == node_hash(base),
                'diagnostic_previews': [format_location(m) for m in messages]}
        # Observational cases measure the compiler's boundary rather than assuming rejection.
        item['status'] = 'passed' if (built.get('status') in ('passed', 'failed') and
            (expected == 'observe' or built.get('status') == expected) and
            recovery and recovery['status'] == 'passed' and item['source_restored'] and
            all(m.get('location', {}).get('status') == 'located' for m in messages)) else 'failed'
        report['cases'].append(item)
        write_json(os.path.join(OUT, identifier + '.json'), item)
        checkpoint(identifier + '_complete')

    # Verify the Build scope with the same faulty FB unreferenced, then referenced.
    checkpoint('unreferenced_and_referenced_fb')
    model = parse_st('FUNCTION_BLOCK FB_InoAI_DiagnosticScope\nVAR_INPUT\nx : BOOL;\nEND_VAR\nVAR_OUTPUT\ny : BOOL;\nEND_VAR', 'y := x;')
    added = bridge.import_ld(to_st_xml(model), model['name'], language='ST')
    fb = bridge.objects[added['id']]
    fb_original = text(fb.textual_implementation.text)
    try:
        fb.textual_implementation.replace(new_text='INOAI_SCOPE_UNDECLARED := 1;')
        unreferenced = bridge.compile()
        snap = bridge.snapshot(False)
        link = proposal(snap, base['id'], extra_decl('inoScope : FB_InoAI_DiagnosticScope;'),
                        implementation + '\ninoScope(x := TRUE);', 'diagnostic_scope_test')
        bridge.apply(link)
        referenced = bridge.compile()
        bridge.rollback()
    finally:
        fb.textual_implementation.replace(new_text=fb_original)
    report['build_scope'] = {'unreferenced': unreferenced, 'referenced': referenced,
        'recovery': bridge.compile(), 'status': 'passed' if referenced['status'] == 'failed' else 'failed'}
    report['source_unchanged'] = original_hash == file_hash(SOURCE)
    report['final_compile'] = bridge.compile()
    project.save()
    report['summary'] = {'cases': len(report['cases']),
        'failed_builds': sum(c['compile']['status'] == 'failed' for c in report['cases']),
        'passed_builds': sum(c['compile']['status'] == 'passed' for c in report['cases']),
        'diagnostics': sum(c['compile']['location_coverage']['diagnostics'] for c in report['cases']),
        'located': sum(c['compile']['location_coverage']['located'] for c in report['cases'])}
    report['status'] = 'passed' if (all(c['status'] == 'passed' for c in report['cases']) and
        report['build_scope']['status'] == 'passed' and report['final_compile']['status'] == 'passed' and
        report['source_unchanged']) else 'failed'
    checkpoint('complete')
except Exception as exc:
    report.update(status='failed', error=text(exc), traceback=traceback.format_exc())
    checkpoint('failed')
finally:
    if project is not None:
        try: project.close()
        except Exception: pass
    system.exit()
