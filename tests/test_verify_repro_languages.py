import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures/repro'
sys.path.insert(0, str(ROOT / 'ci'))
spec = importlib.util.spec_from_file_location('verify_repro', ROOT / 'ci/verify_repro.py')
repro = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repro)

JEST_PACKAGE = '{"name": "calc", "private": true, "devDependencies": {"jest": "29.7.0"}}\n'


def fixture_gate(case, exercised=False, keep_bug=False, extra=None, base_extra=None):
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name) / 'repo'

    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
                                        *args], stderr=subprocess.DEVNULL, text=True).strip()
    shutil.copytree(FIXTURES / case / 'base', root, copy_function=shutil.copyfile)
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
    with patch.dict('os.environ', {'GIT_DIR': '/nonexistent/cold-repro-git'}):
        code = repro.gate(root, True, None, ['Fix add', '', base, head], exercised)
    receipt = json.loads((root / '.cold/verify-repro.json').read_text())
    tmp.cleanup()
    return code, receipt


def outcomes(receipt):
    return [(t['name'], t['base_exit'], t['head_exit'], t['kind']) for t in receipt['tests']]


class FixtureRepos(unittest.TestCase):
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
    def test_strict_mode_cannot_trace_a_go_proof(self):
        code, receipt = fixture_gate('go', exercised=True)
        self.assertEqual((code, receipt['result']), (2, 'INCOMPLETE'), receipt)
        self.assertEqual(outcomes(receipt), [('calc/calc_test.go::TestAdd', 1, 0, 'unproven')])
        self.assertIn('cannot trace Go, Rust or JavaScript tests yet', receipt['error'])

    @unittest.skipUnless(shutil.which('cargo'), 'cargo is not installed')
    def test_cargo_test_proves_red_on_base_and_green_on_head(self):
        code, receipt = fixture_gate('cargo')
        self.assertEqual((code, receipt['result']), (0, 'PASS'), receipt)
        self.assertEqual(outcomes(receipt), [('tests/add.rs::adds', 1, 0, 'proof')])

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


class Selection(unittest.TestCase):
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
