import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_verify_explicit import needs_bwrap
import verify_receipt

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures/repro'
sys.path.insert(0, str(ROOT / 'ci'))
spec = importlib.util.spec_from_file_location('verify_repro', ROOT / 'ci/verify_repro.py')
repro = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repro)

JEST_PACKAGE = '{"name": "calc", "private": true, "devDependencies": {"jest": "29.7.0"}}\n'


def fixture_gate(case, exercised=False, keep_bug=False, extra=None, base_extra=None, node_modules=None, *, local=True):
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name) / 'repo'

    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
                                        *args], stderr=subprocess.DEVNULL, text=True).strip()
    shutil.copytree(FIXTURES / case / 'base', root, copy_function=shutil.copyfile)
    if case == 'vitest':
        (root / 'package.json.fixture').rename(root / 'package.json')
    for name, text in (base_extra or {}).items():
        (root / name).write_text(text)
    git('init', '-q', '-b', 'main')
    git('add', '.')
    git('commit', '-qm', 'base')
    base = git('rev-parse', 'HEAD')
    git('switch', '-qc', 'fix/add')
    for file in (FIXTURES / case / 'head').rglob('*'):
        target = root / file.relative_to(FIXTURES / case / 'head')
        if file.is_file() and not (keep_bug and (FIXTURES / case / 'base' / file.relative_to(FIXTURES / case / 'head')).is_file()
                                   and 'test' not in file.name):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, target)
    for name, text in (extra or {}).items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    git('add', '.')
    git('commit', '-qm', 'Fix add')
    head = git('rev-parse', 'HEAD')
    if node_modules:
        shutil.copytree(node_modules, root / 'node_modules', symlinks=True)
    with patch.dict('os.environ', {'GIT_DIR': '/nonexistent/cold-repro-git'}):
        code = repro.gate(root, True, None, ['Fix add', '', base, head], exercised, local=local)
    receipt = json.loads((root / '.cold/verify-repro.json').read_text())
    tmp.cleanup()
    return code, receipt


def outcomes(receipt):
    return [(t['name'], t['base_exit'], t['head_exit'], t['kind']) for t in receipt['tests']]


def bug_fix_ci_exit(repro_receipt):
    result = repro_receipt['result']
    return verify_receipt.ci_exit({'result': result, 'repro': repro_receipt, 'run': {},
                                   'checks': {'tests': ('PASS', ''), 'repro': (result, repro_receipt.get('error', ''))}},
                                  bug_fix=True)


class BashFixture(unittest.TestCase):
    @needs_bwrap
    def test_new_shell_test_replays_against_both_service_units(self):
        code, receipt = fixture_gate('bash', exercised=True)
        self.assertEqual((code, receipt['result']), (2, 'INCOMPLETE'), receipt)
        self.assertEqual(outcomes(receipt), [('deploy/test_archive.sh::bash', 1, 0, 'unproven')])
        self.assertEqual(receipt['tests'][0]['commands'], ['bash -- deploy/test_archive.sh'] * 2)
        self.assertIn('Bash replay has no behavior evidence', receipt['error'])
        self.assertEqual(bug_fix_ci_exit(receipt), 1)

    @needs_bwrap
    def test_bash_setup_failure_blocks_ci_even_with_a_python_replay(self):
        which = shutil.which
        for setup_failure in ('missing command', 'missing shell', None):
            with self.subTest(setup_failure=setup_failure):
                extra = {
                    'value.py': 'def value():\n    return 2\n',
                    'test_value.py': 'import unittest\nfrom value import value\n\n'
                                     'class Value(unittest.TestCase):\n'
                                     '    def test_value(self):\n'
                                     '        self.assertEqual(value(), 2)\n',
                }
                if setup_failure == 'missing command':
                    extra['deploy/test_archive.sh'] = '#!/bin/bash\nexec coldverify-fixture-missing-command\n'
                with patch.object(shutil, 'which', side_effect=lambda name, *args, **kwargs:
                                  None if setup_failure == 'missing shell' and name == 'bash' else which(name, *args, **kwargs)):
                    code, receipt = fixture_gate('bash', exercised=True, extra=extra,
                                                 base_extra={'value.py': 'def value():\n    return 1\n'})
                self.assertIn(('test_value.py::Value.test_value', 1, 0, 'unproven'), outcomes(receipt))
                self.assertEqual((code, receipt['result']), (2, 'INCOMPLETE'), receipt)
                if setup_failure:
                    self.assertIn('Bash test could not run', receipt['error'])
                    self.assertEqual(bug_fix_ci_exit(receipt), 1)
                else:
                    self.assertIn(('deploy/test_archive.sh::bash', 1, 0, 'unproven'), outcomes(receipt))
                    self.assertIn('python-coverage-not-runner-observed', receipt['error'])
                    self.assertNotIn('Bash test could not run', receipt['error'])
                    self.assertEqual(bug_fix_ci_exit(receipt), 0)

    @needs_bwrap
    def test_missing_setup_command_retains_its_exit(self):
        code, receipt = fixture_gate('bash', exercised=True, extra={
            'deploy/test_archive.sh': '#!/bin/bash\nexec coldverify-fixture-missing-command\n',
        })
        self.assertEqual((code, receipt['result']), (2, 'INCOMPLETE'), receipt)
        self.assertEqual(outcomes(receipt), [('deploy/test_archive.sh::bash', 127, 127, 'unproven')])

    @needs_bwrap
    def test_missing_shell_setup_on_either_side_is_incomplete(self):
        for side, overrides, exits in (
            ('base', {'base_extra': {'deploy/archive.service': ''}}, (2, 0)),
            ('head', {'extra': {'deploy/archive.service': ''}}, (1, 2)),
        ):
            with self.subTest(side=side):
                code, receipt = fixture_gate('bash', exercised=True, **overrides)
                self.assertEqual((code, receipt['result']), (2, 'INCOMPLETE'), receipt)
                test, = receipt['tests']
                self.assertEqual((test['base_exit'], test['head_exit'], test['kind']), (*exits, 'unproven'))
                self.assertIn(f'Bash test could not run on {side}', receipt['error'])

    def test_missing_bash_is_incomplete(self):
        which = shutil.which
        with patch.object(shutil, 'which', side_effect=lambda name, **kwargs: None if name == 'bash' else which(name, **kwargs)):
            code, receipt = fixture_gate('bash', exercised=True)
        self.assertEqual((code, receipt['result']), (2, 'INCOMPLETE'), receipt)
        self.assertIn('Bash is required', receipt['error'])

    def test_shell_test_without_bash_shebang_is_unsupported(self):
        code, receipt = fixture_gate('bash', exercised=True, extra={'deploy/test_archive.sh': '#!/bin/sh\nexit 0\n'})
        self.assertEqual((code, receipt['result']), (2, 'INCOMPLETE'), receipt)
        self.assertEqual(receipt['tests'], [])
        self.assertIn('cannot reproduce .sh tests yet', receipt['error'])

    def test_compat_keeps_bash_replay_unsupported(self):
        code, receipt = fixture_gate('bash')
        self.assertEqual((code, receipt['result']), (2, 'INCOMPLETE'), receipt)
        self.assertEqual(receipt['tests'], [])

    def test_unsupported_shell_receipt_explains_the_strict_proof_route(self):
        code, receipt = fixture_gate('bash', exercised=True, extra={'deploy/test_archive.sh': '#!/bin/sh\nexit 0\n'})
        result, reason = verify_receipt.repro_check(code, receipt)
        self.assertEqual(result, 'INCOMPLETE')
        self.assertIn('deploy/test_archive.sh', reason)
        self.assertIn('Bash replay', reason)
        self.assertIn('unproven', reason)
        self.assertIn('changed production line', reason)
        self.assertNotIn('--no-repro-reason', reason)


class FixtureRepos(unittest.TestCase):
    def test_typescript_without_a_declared_runner_reports_incomplete(self):
        code, receipt = fixture_gate('vitest', exercised=True, base_extra={'package.json': '{"name": "calc"}'})
        self.assertEqual((code, receipt['result']), (2, 'INCOMPLETE'), receipt)
        self.assertIn('TypeScript repro requires a package that declares Jest or Vitest', receipt['error'])

    def test_vitest_without_node_reports_incomplete(self):
        which = shutil.which
        with patch.object(shutil, 'which', side_effect=lambda name, *args: None if name == 'node' else which(name, *args)):
            code, receipt = fixture_gate('vitest', exercised=True)
        self.assertEqual((code, receipt['result']), (2, 'INCOMPLETE'), receipt)
        self.assertIn('Node.js is required', receipt['error'])

    def test_vitest_without_prepared_dependencies_reports_incomplete(self):
        which = shutil.which
        with patch.object(shutil, 'which', side_effect=lambda name, *args: sys.executable if name == 'node' else which(name, *args)):
            code, receipt = fixture_gate('vitest', exercised=True)
        self.assertEqual((code, receipt['result']), (2, 'INCOMPLETE'), receipt)
        self.assertIn('Vitest dependencies are missing', receipt['error'])

    @unittest.skipUnless(shutil.which('go'), 'go is not installed')
    def test_go_test_run_proves_red_on_base_and_green_on_head(self):
        code, receipt = fixture_gate('go')
        self.assertEqual((code, receipt['result']), (0, 'PASS'), receipt)
        self.assertEqual(outcomes(receipt), [('calc/calc_test.go::TestAdd', 1, 0, 'proof')])

    @unittest.skipUnless(shutil.which('go'), 'go is not installed')
    def test_go_fix_that_still_fails_on_head_is_a_regression(self):
        code, receipt = fixture_gate('go', keep_bug=True)
        self.assertEqual((code, receipt['result']), (1, 'FAIL'), receipt)
        self.assertEqual(outcomes(receipt), [('calc/calc_test.go::TestAdd', 1, 1, 'regression')])

    @unittest.skipUnless(shutil.which('go'), 'go is not installed')
    def test_strict_go_requires_coverage_from_the_selected_test(self):
        code, receipt = fixture_gate('go', exercised=True, local=False)
        self.assertEqual((code, receipt['result']), (0, 'PASS'), receipt)
        self.assertEqual(outcomes(receipt), [('calc/calc_test.go::TestAdd', 1, 0, 'proof')])

        source_check = """package calc

import (
    "os"
    "strings"
    "testing"
)

func TestSource(t *testing.T) {
    source, err := os.ReadFile("calc.go")
    if err != nil {
        t.Fatal(err)
    }
    if !strings.Contains(string(source), "return a + b") {
        t.Fatal("source has the old expression")
    }
}
"""
        code, receipt = fixture_gate('go', exercised=True, local=False, extra={'calc/calc_test.go': source_check},
                                     base_extra={'calc/init.go': 'package calc\n\nfunc init() { Add(0, 0) }\n'})
        self.assertEqual((code, receipt['result']), (2, 'INCOMPLETE'), receipt)
        self.assertEqual(outcomes(receipt), [('calc/calc_test.go::TestSource', 1, 0, 'unproven')])
        self.assertIn('proof does not exercise the change', receipt['error'])

        constant_check = """package calc

import "testing"

func TestComment(t *testing.T) {
    if Add(0, 0) != 1 {
        t.Fatal("wrong offset")
    }
}
"""
        add = 'package calc\n\nfunc Add(a, b int) int {\n%s\n\treturn a - b + offset\n}\n'
        for comment in ('\t/* New comment\n\t   across two lines */\n', None):
            with self.subTest(comment=comment):
                source = add % comment if comment is not None else add.replace('%s\n', '')
                code, receipt = fixture_gate(
                    'go', exercised=True, local=False,
                    base_extra={'calc/calc.go': add % '\t// Old comment', 'calc/offset.go': 'package calc\nconst offset = 0\n'},
                    extra={'calc/calc.go': source, 'calc/offset.go': 'package calc\nconst offset = 1\n',
                           'calc/calc_test.go': constant_check})
                self.assertEqual((code, receipt['result']), (2, 'INCOMPLETE'), receipt)
                self.assertEqual(outcomes(receipt), [('calc/calc_test.go::TestComment', 1, 0, 'unproven')])

    @unittest.skipUnless(shutil.which('cargo'), 'cargo is not installed')
    def test_cargo_test_proves_red_on_base_and_green_on_head(self):
        code, receipt = fixture_gate('cargo')
        self.assertEqual((code, receipt['result']), (0, 'PASS'), receipt)
        self.assertEqual(outcomes(receipt), [('tests/add.rs::adds', 1, 0, 'proof')])

    @needs_bwrap
    def test_strict_cargo_runs_the_selected_integration_target(self):
        with tempfile.TemporaryDirectory() as tools:
            cargo = Path(tools) / 'cargo'
            cargo.write_text('''#!/bin/sh
if [ "$1" = fetch ]; then exit 0; fi
[ "$*" = 'test --test add -- --exact adds' ] || exit 2
if grep -Fq 'a + b' src/lib.rs; then
    echo 'test adds ... ok'
    exit 0
fi
echo 'test adds ... FAILED'
exit 101
''')
            cargo.chmod(0o755)
            which = shutil.which
            with (patch.dict(os.environ, PATH=tools + os.pathsep + os.environ['PATH']),
                  patch.object(shutil, 'which', side_effect=lambda name, *args, **kwargs:
                               None if name in ('llvm-profdata', 'llvm-cov') else which(name, *args, **kwargs))):
                code, receipt = fixture_gate('cargo', exercised=True)
        self.assertEqual((code, receipt['result']), (2, 'INCOMPLETE'), receipt)
        self.assertEqual(outcomes(receipt), [('tests/add.rs::adds', 1, 0, 'unproven')])
        self.assertIn('Cargo trace: LLVM coverage tools are unavailable', receipt['tests'][0]['head_output'])

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_node_test_proves_red_on_base_and_green_on_head(self):
        code, receipt = fixture_gate('node')
        self.assertEqual((code, receipt['result']), (0, 'PASS'), receipt)
        self.assertEqual(outcomes(receipt), [('test/calc.test.js::adds two numbers', 1, 0, 'proof')])

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_node_fix_that_still_fails_on_head_is_a_regression(self):
        code, receipt = fixture_gate('node', keep_bug=True)
        self.assertEqual((code, receipt['result']), (1, 'FAIL'), receipt)
        self.assertEqual(outcomes(receipt), [('test/calc.test.js::adds two numbers', 1, 1, 'regression')])

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_a_changed_test_the_gate_cannot_select_blocks_a_pass(self):
        nested = "const test = require('node:test');\ntest.describe('group', () => {\n  test.it('nested', () => {});\n});\n"
        code, receipt = fixture_gate('node', extra={'test/nested.test.js': nested})
        self.assertEqual((code, receipt['result']), (2, 'INCOMPLETE'), receipt)
        self.assertEqual(outcomes(receipt), [('test/calc.test.js::adds two numbers', 1, 0, 'proof')])
        self.assertIn('test/nested.test.js', receipt['error'])

    @unittest.skipUnless(shutil.which('node'), 'node is not installed')
    def test_a_changed_test_that_cannot_run_on_head_blocks_a_pass(self):
        broken = "const test = require('node:test');\nconst missing = require('./missing');\n\ntest('loads', () => {});\n"
        code, receipt = fixture_gate('node', extra={'test/broken.test.js': broken})
        self.assertEqual((code, receipt['result']), (2, 'INCOMPLETE'), receipt)
        self.assertIn(('test/broken.test.js::loads', 2, 2, 'unchanged'), outcomes(receipt))
        self.assertIn('could not run these tests on head: test/broken.test.js::loads', receipt['error'])

    @unittest.skipUnless(shutil.which('npm'), 'npm is not installed')
    def test_jest_proves_red_on_base_and_green_on_head(self):
        code, receipt = fixture_gate('jest', base_extra={'package.json': JEST_PACKAGE})
        self.assertEqual((code, receipt['result']), (0, 'PASS'), receipt)
        self.assertEqual(outcomes(receipt), [('__tests__/calc.test.js::adds two numbers', 1, 0, 'proof')])

    @needs_bwrap
    def test_strict_node_records_the_changed_javascript_line(self):
        self.check_strict_javascript('node', 'test/calc.test.js', {})

    @needs_bwrap
    def test_strict_node_ignores_comments_inside_a_called_function(self):
        path = 'test/calc.test.js'
        test = (FIXTURES / 'node' / 'head' / path).read_text(encoding='utf-8')
        test = test.replace('assert.strictEqual', "const answer = require('../answer.json');\n  assert.strictEqual")
        test = test.replace('add(2, 3), 5', 'add(2, 3) + answer, 5')
        source = 'exports.add = (a, b) => {\n  /*\n    old label\n  */\n  return a + b;\n};\n'
        code, receipt = fixture_gate('node', exercised=True, base_extra={'calc.js': source, 'answer.json': '1'},
                                     extra={'calc.js': source.replace('old label', 'new label'), 'answer.json': '0', path: test})
        self.assertEqual((code, receipt['result']), (2, 'INCOMPLETE'), receipt)
        self.assertEqual(outcomes(receipt), [(path + '::adds two numbers', 1, 0, 'unproven')])

    @needs_bwrap
    def check_strict_jest_records_the_changed_javascript_line(self):
        self.check_strict_javascript('jest', '__tests__/calc.test.js', {'package.json': JEST_PACKAGE})

    @needs_bwrap
    def test_strict_node_matches_multiline_source_with_an_at_sign_in_its_path(self):
        path = 'test/calc.test.js'
        test = (FIXTURES / 'node' / 'head' / path).read_text(encoding='utf-8').replace('../calc', '../calc@')
        unchanged_source = (FIXTURES / 'node/base/calc.js').read_text(encoding='utf-8')
        source = '// 🧪\r\nexports.add = (a, b) => {\r\n  /* before */ return a %s b; /* after */\r\n};\r\n'
        code, receipt = fixture_gate('node', exercised=True, base_extra={'calc@.js': source % '-'},
                                     extra={'calc.js': unchanged_source, 'calc@.js': source % '+', path: test})
        self.assertEqual((code, receipt['result']), (0, 'PASS'), receipt)
        self.assertEqual(outcomes(receipt), [(path + '::adds two numbers', 1, 0, 'proof')])
        self.assertIn('calc@.js:3', receipt['tests'][0]['head_output'])

    def check_strict_javascript(self, runner, path, base):
        source = 'exports.add = (a, b) => a %s b;\n'
        code, receipt = fixture_gate(runner, exercised=True, base_extra=base)
        self.assertEqual((code, receipt['result']), (0, 'PASS'), receipt)
        self.assertEqual(outcomes(receipt), [(path + '::adds two numbers', 1, 0, 'proof')])
        self.assertIn('calc.js:1', receipt['tests'][0]['head_output'])

        test = (FIXTURES / runner / 'head' / path).read_text(encoding='utf-8')
        source_read = "require('node:fs').readFileSync(require.resolve('../calc'), 'utf8')"
        cases = {
            'unused function': (source.replace('%s', '-') + 'exports.unused = () => {\n  return %d;\n};\n',
                                test.replace('add(2, 3)', f'(add(2, 3), {source_read}.includes("return 5") ? 5 : 4)')),
            'import only': ('function total() {\n  return %d;\n}\nexports.total = total();\n',
                            test.replace('{ add }', '{ total }').replace('add(2, 3)', 'total')),
        }
        for scenario, (source, test) in cases.items():
            with self.subTest(scenario=scenario):
                code, receipt = fixture_gate(runner, exercised=True, base_extra={**base, 'calc.js': source % 4},
                                             extra={'calc.js': source % 5, path: test})
                self.assertEqual((code, receipt['result']), (2, 'INCOMPLETE'), receipt)
                self.assertEqual(outcomes(receipt), [(path + '::adds two numbers', 1, 0, 'unproven')])


class Selection(unittest.TestCase):
    def test_vitest_selects_a_changed_case_inside_a_suite(self):
        source = "describe('group', () => {\n  it('old', () => expect(1).toBe(1));\n  it('new', () => {\n    expect(add()).toBe(3);\n  });\n});\n"
        before, _ = repro.declared_tests('vitest', 'calc.test.ts', source.replace('toBe(3)', 'toBe(2)'))
        after, rest = repro.declared_tests('vitest', 'calc.test.ts', source)
        self.assertEqual([name for name, case in after.items() if before[name] != case], ['new'])
        self.assertEqual(rest, '')

    def test_only_new_or_changed_tests_are_selected(self):
        before, _ = repro.declared_tests('go', 'a_test.go', 'package a\n\nfunc TestOld(t *testing.T) {}\n')
        after, _ = repro.declared_tests('go', 'a_test.go', 'package a\n\nfunc TestOld(t *testing.T) {}\n\nfunc TestNew(t *testing.T) {\n\tt.Fatal()\n}\n')
        self.assertEqual([name for name, chunk in after.items() if before.get(name) != chunk], ['TestNew'])

    def test_a_helper_after_the_last_test_does_not_select_it(self):
        source = 'package a\n\nfunc TestLast(t *testing.T) {\n\tcheck(t)\n}\n\nfunc check(t *testing.T) {\n\t%s\n}\n'
        before, before_rest = repro.declared_tests('go', 'a_test.go', source % 't.Log()')
        after, after_rest = repro.declared_tests('go', 'a_test.go', source % 't.Fatal()')
        self.assertEqual(before, after)
        self.assertEqual((before_rest, after_rest), ('', ''))

    def test_changed_tests_outside_top_level_declarations_are_reported(self):
        _, before = repro.declared_tests('jest', 'a.test.js', "describe('g', () => {\n  it('a', () => {});\n});\n")
        _, after = repro.declared_tests('jest', 'a.test.js', "describe('g', () => {\n  it('a', () => {});\n  it('b', () => {});\n});\n")
        self.assertNotEqual(before, after)
        _, edited = repro.declared_tests('jest', 'a.test.js', "describe('g', () => {\n  it('a', () => { expect(1).toBe(2); });\n});\n")
        self.assertNotEqual(before, edited)
        cases, crowded = repro.declared_tests('node', 'a.test.js', "test('a', () => {}); test('b', () => { throw 1; });\n")
        self.assertEqual((cases, crowded), ({}, "test('a', () => {}); test('b', () => { throw 1; });\n"))
        _, nested = repro.declared_tests('cargo', 'tests/common/mod.rs', '#[test]\nfn hidden() {}\n')
        self.assertEqual(nested, '#[test]\nfn hidden() {}\n')

    def test_rust_attributes_and_nested_test_modules(self):
        source = '#[test]\nfn plain() {}\n#[tokio::test]\n#[ignore]\nasync fn awaited() {}\nfn helper() {}\n'
        self.assertEqual(list(repro.declared_tests('cargo', 'tests/it.rs', source)[0]), ['plain', 'awaited'])
        self.assertEqual(repro.declared_tests('cargo', 'tests/common/mod.rs', source)[0], {})

    def test_only_top_level_javascript_tests_are_selected(self):
        source = "test('top', () => {});\ndescribe('group', () => {\n  it('nested', () => {});\n});\nit(\"other\", () => {});\n"
        self.assertEqual(list(repro.declared_tests('jest', 'a.test.ts', source)[0]), ['top', 'other'])

    def test_runner_needs_the_language_manifest(self):
        tree = {'go.mod', 'pkg/a_test.go', 'tests/it.rs', 'web/package.json', 'web/a.test.ts', 'b.test.js'}
        with patch.object(repro, 'git', return_value=b'{"devDependencies": {"jest": "29.7.0"}}'):
            self.assertEqual(repro.foreign_runner('.', 'HEAD', tree, 'pkg/a_test.go'), 'go')
            self.assertIsNone(repro.foreign_runner('.', 'HEAD', tree, 'tests/it.rs'))
            self.assertEqual(repro.foreign_runner('.', 'HEAD', tree, 'web/a.test.ts'), 'jest')
            self.assertIsNone(repro.foreign_runner('.', 'HEAD', tree, 'b.test.js'))
        with patch.object(repro, 'git', return_value=b'{"name": "web"}'):
            self.assertEqual(repro.foreign_runner('.', 'HEAD', tree, 'web/a.test.js'), 'node')
            self.assertIsNone(repro.foreign_runner('.', 'HEAD', tree, 'web/a.test.ts'))
        with patch.object(repro, 'git', return_value=b'{"dependencies": ["jest"]}'), self.assertRaises(ValueError):
            repro.foreign_runner('.', 'HEAD', tree, 'web/a.test.js')


class Results(unittest.TestCase):
    def test_vitest_counts_changed_lines_executed_beyond_collection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            executed, collected = root / 'executed.json', root / 'collected.json'
            file = {
                'statementMap': {'0': {'start': {'line': 2}}},
                's': {'0': 1},
                'fnMap': {'0': {'loc': {'start': {'line': 1}, 'end': {'line': 3}}}},
                'f': {'0': 1},
            }
            coverage = {str(root / 'calc.ts'): file}
            collected.write_text(json.dumps(coverage))
            executed.write_text(json.dumps(coverage))
            self.assertFalse(repro.vitest_exercised(executed, collected, root, {'calc.ts': [2]}))
            file['s']['0'] = file['f']['0'] = 2
            executed.write_text(json.dumps(coverage))
            self.assertTrue(repro.vitest_exercised(executed, collected, root, {'calc.ts': [2]}))
            self.assertFalse(repro.vitest_exercised(executed, collected, root, {'calc.ts': [4]}))

    TAP = "TAP version 13\n# Subtest: adds\n{status} 1 - adds{directive}\n  ---\n  duration_ms: 1\n{code}  ...\n1..1\n# tests 1\n"

    def tap(self, status='ok', directive='', code=''):
        return repro.foreign_result('node', 'adds', self.TAP.format(status=status, directive=directive, code=code), None, int(status != 'ok'))

    def test_node_red_needs_an_assertion_failure(self):
        self.assertEqual(self.tap(), 0)
        self.assertEqual(self.tap('not ok', code="  code: 'ERR_ASSERTION'\n"), 1)
        self.assertEqual(self.tap('not ok', code="  code: 'ERR_TEST_FAILURE'\n  name: 'TypeError'\n"), 2)
        self.assertEqual(self.tap(directive=' # SKIP'), 2)
        self.assertEqual(repro.foreign_result('node', 'adds', 'TAP version 13\n1..0\n', None, 0), 2)

    def test_jest_red_needs_an_assertion_failure(self):
        def result(status, details):
            with tempfile.TemporaryDirectory() as tmp:
                report = Path(tmp) / 'jest.json'
                report.write_text(json.dumps({'testResults': [{'assertionResults': [
                    {'title': 'adds', 'status': status, 'failureDetails': details},
                    {'title': 'other', 'status': 'pending', 'failureDetails': []}]}]}))
                return repro.foreign_result('jest', 'adds', '', report, int(status != 'passed'))
        self.assertEqual(result('passed', []), 0)
        self.assertEqual(result('failed', [{'matcherResult': {'pass': False}}]), 1)
        self.assertEqual(result('failed', [{'code': 'ERR_ASSERTION'}]), 1)
        self.assertEqual(result('failed', [{}]), 2)
        self.assertEqual(repro.foreign_result('jest', 'adds', '', Path('/nonexistent/jest.json'), 1), 2)

    def test_go_needs_exactly_one_result_for_the_test(self):
        def result(*actions, code=None):
            final = [action for test, action in actions if test == 'TestAdd' and action != 'run']
            code = code if code is not None else int(final != ['pass'])
            return repro.foreign_result('go', 'TestAdd', '\n'.join(json.dumps({'Action': action, 'Test': test}) for test, action in actions), None, code)
        self.assertEqual(result(('TestAdd', 'run'), ('TestAdd', 'pass')), 0)
        self.assertEqual(result(('TestAdd/sub', 'fail'), ('TestAdd', 'fail')), 1)
        self.assertEqual(result(('TestAdd', 'skip')), 2)
        self.assertEqual(result(), 2)

    def test_a_reported_pass_from_a_failed_command_is_not_green(self):
        passed = json.dumps({'Action': 'pass', 'Test': 'TestAdd'})
        self.assertEqual(repro.foreign_result('go', 'TestAdd', passed, None, 1), 2)
        self.assertEqual(repro.foreign_result('cargo', 'adds', 'test adds ... ok\n', None, 101), 2)
        self.assertEqual(repro.foreign_result('cargo', 'adds', 'test adds ... FAILED\n', None, 0), 2)
        self.assertEqual(repro.foreign_result('cargo', 'adds', 'test adds ... FAILED\n', None, 2), 2)

    def test_cargo_needs_exactly_one_result_for_the_test(self):
        self.assertEqual(repro.foreign_result('cargo', 'adds', 'running 1 test\ntest adds ... ok\n', None, 0), 0)
        self.assertEqual(repro.foreign_result('cargo', 'adds', 'test adds ... FAILED\n', None, 101), 1)
        self.assertEqual(repro.foreign_result('cargo', 'adds', 'test adds ... ignored\n', None, 0), 2)
        self.assertEqual(repro.foreign_result('cargo', 'adds', 'error[E0425]: cannot find function\n', None, 101), 2)


if __name__ == '__main__':
    unittest.main()
