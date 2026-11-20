import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import runpy
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import criteria_shim

ROOT = Path(__file__).resolve().parents[1]
GIT_LOCATION = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR',
                'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES')
REFUSE = '#!/usr/bin/env bash\necho "$0 $*" >> "$SHIM_CALLS"\nexit 255\n'
NO_REMOTE_GH = '#!/usr/bin/env bash\necho "$0 $*" >> "$SHIM_CALLS"\necho "no git remotes found" >&2\nexit 1\n'
ADD_FLOW = '.fleet/flows/check-add'
OBSERVE_ADD = 'from calc import add; value = add(2, 3); '
PYTEST_RECIPE = ('set -euo pipefail\nscratch="$(mktemp -d)"\ntrap \'rm -rf "$scratch"\' EXIT\n'
                 'python3 -m venv "$scratch/venv"\n"$scratch/venv/bin/pip" install -q pytest\n'
                 '"$scratch/venv/bin/python" "$COLD_VERIFY_PYTEST" -q\n')


@unittest.skipUnless(os.name == 'posix', 'drives the native snapshot export')
class OutsiderRunsLocally(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        base = Path(folder.name)
        self.repo, self.calls = base / 'sample', base / 'shim-calls'
        shims, home = base / 'shims', base / 'home'
        for directory in (self.repo, shims, home):
            directory.mkdir()
        (shims / 'sitecustomize.py').write_text('import platform\nplatform.node = lambda: "outsider-box"\n')
        for name, script in (('ssh', REFUSE), ('coldbox', REFUSE), ('gh', NO_REMOTE_GH)):
            (shims / name).write_text(script)
            (shims / name).chmod(0o755)
        self.env = {key: value for key, value in os.environ.items()
                    if key not in GIT_LOCATION and not key.startswith(('GITHUB_', 'COLD_VERIFY_')) and key != 'PYTHONPATH'}
        self.env.update(criteria_shim.install(shims, base / 'criteria-calls'))
        self.env.update(PATH=f'{shims}:{self.env["PATH"]}', PYTHONPATH=str(shims), HOME=str(home), SHIM_CALLS=str(self.calls), COLDBOX_GUEST='0',
                        GIT_AUTHOR_NAME='Outsider', GIT_AUTHOR_EMAIL='outsider@localhost',
                        GIT_COMMITTER_NAME='Outsider', GIT_COMMITTER_EMAIL='outsider@localhost')
        self.write('calc.py', 'def add(a, b):\n    return a - b\n')
        self.write('tests/test_other.py', 'import unittest\nclass Other(unittest.TestCase):\n'
                   '    def test_truth(self):\n        self.assertTrue(True)\n')
        self.write('.fleet/verify.sh', 'set -euo pipefail\npython3 "$COLD_VERIFY_UNITTEST" discover -s tests -v\n')
        self.git('init', '-q', '-b', 'main')
        self.git('add', '-A')
        self.git('commit', '-qm', 'base')
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.git('switch', '-qc', 'fix/add')
        self.write('calc.py', 'def add(a, b):\n    return a + b\n')
        self.write('tests/test_calc.py', 'import sys, unittest\nsys.path.insert(0, ".")\nimport calc\n'
                   'class Add(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(calc.add(2, 3), 5)\n')
        self.git('add', '-A')
        self.git('commit', '-qm', 'Fix add')

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def git(self, *args):
        subprocess.run(['git', '-C', str(self.repo), '-c', 'commit.gpgsign=false', *args], check=True, env=self.env)

    def run_tool(self, *args, **extra):
        return subprocess.run([sys.executable, *args], cwd=self.repo, env={**self.env, **extra},
                              text=True, capture_output=True, timeout=300)

    def shim_calls(self):
        return self.calls.read_text() if self.calls.exists() else ''

    def test_unconfigured_checkout_selects_the_local_platform(self):
        selected = self.run_tool(str(ROOT / 'ci/linux-verify.py'), '--platform', str(self.repo))
        self.assertEqual((selected.returncode, selected.stdout.strip()), (0, 'local'), selected.stderr)
        configured = self.run_tool(str(ROOT / 'ci/linux-verify.py'), '--platform', str(self.repo), COLD_VERIFY_HOST='runner-a')
        self.assertEqual(configured.stdout.strip(), 'linux-generic', configured.stderr)

    def test_python_bug_fix_replays_on_this_machine_without_ssh(self):
        result = self.run_tool(str(ROOT / 'coldverify'), '--ci', '--builder-model', criteria_shim.BUILDER, '--claim', 'add adds')
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 2, output)
        self.assertIn('COLDVERIFY INCOMPLETE', output)
        self.assertIn('tests/test_calc.py::Add.test_add: base=1 head=0 (unproven)', output)
        self.assertIn('python-coverage-not-runner-observed', output)
        self.assertIn('2 tests ran fresh and passed', output)
        self.assertNotIn('ssh', self.shim_calls())

    def commit_branch(self, branch, files):
        self.git('switch', '-qc', branch, 'main')
        for name, text in files.items():
            self.write(name, text)
        self.git('add', '-A')
        self.git('commit', '-qm', branch)

    def verify_add_flow(self, branch, script, covers):
        self.git('switch', '-qc', branch, 'main')
        self.write('calc.py', 'def add(a, b):\n    return a + b\n')
        self.write(ADD_FLOW, f"#!/bin/sh\npython3 -c '{OBSERVE_ADD}{script}'\n")
        (self.repo / ADD_FLOW).chmod(0o755)
        self.git('add', '-A')
        self.git('commit', '-qm', 'Add using a flow as proof')
        claim = 'add(2, 3) returns 5'
        printed = io.StringIO()
        with mock.patch.dict(os.environ, {**self.env, 'SHIM_CRITERIA': json.dumps([[claim, covers]])}, clear=True), \
                mock.patch.object(sys, 'path', sys.path.copy()), contextlib.chdir(self.repo), contextlib.redirect_stdout(printed):
            code = runpy.run_path(str(ROOT / 'coldverify'))['main'](['--ci', '--builder-model', criteria_shim.BUILDER,
                                                                   '--claim', claim])
        return code, json.loads((self.repo / '.cold/verify-receipt.json').read_text()), printed.getvalue()

    def test_an_observed_flow_can_prove_a_claim_without_a_changed_test(self):
        code, receipt, output = self.verify_add_flow('flow-proof', 'print(value); raise SystemExit(value != 5)', ADD_FLOW)
        self.assertEqual(receipt['repro']['tests'], [])
        [flow] = receipt['flows']['flows']
        self.assertEqual((flow['base_exit'], flow['base_output'], flow['head_exit'], flow['head_output']),
                         (1, '-1', 0, '5'))
        self.assertEqual(receipt['acceptance_criteria'],
                         [{'id': 'C1', 'text': 'add(2, 3) returns 5', 'covered_by': [ADD_FLOW]}])
        self.assertEqual((code, receipt['result']), (0, 'PASS'), output)
        prompts = [call['prompt'] for call in criteria_shim.calls(self.env['CRITERIA_LOG']) if call['tool'] == 'codex']
        self.assertIn(f'FLOW_OBSERVATIONS_JSON: {json.dumps({ADD_FLOW: {"base": "-1", "head": "5"}})}', prompts[-1])

    def test_nonqualifying_flows_leave_the_claim_unverified(self):
        cases = (
            ('regression-only', 'print(value)', ADD_FLOW, (0, 0)),
            ('silent', 'raise SystemExit(value != 5)', ADD_FLOW, (1, 0)),
            ('irrelevant', 'print(value < 0); raise SystemExit(value < 0)', '', (1, 0)),
            ('unprovisionable', 'print(value); raise SystemExit(125 if value != 5 else 0)', ADD_FLOW, (125, 0)),
        )
        for branch, script, covers, exits in cases:
            with self.subTest(flow=branch):
                code, receipt, output = self.verify_add_flow(branch, script, covers)
                [flow] = receipt['flows']['flows']
                self.assertEqual((flow['base_exit'], flow['head_exit']), exits, output)
                self.assertEqual((code, receipt['result']), (2, 'INCOMPLETE'), output)
                self.assertEqual(receipt['acceptance_criteria'][0]['covered_by'], [])

    def test_compat_runs_the_bug_fix_repro_without_a_claim_or_receipt(self):
        result = self.run_tool(str(ROOT / 'coldverify'), '--compat', '--ci')
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn('Repro gate: PASS', output)
        self.assertNotIn('COLDVERIFY', output)
        self.assertFalse((self.repo / '.cold/verify-receipt.json').exists())
        self.assertNotIn('pr comment', self.shim_calls())

    def test_compat_passes_a_change_that_is_not_a_bug_fix_where_strict_is_incomplete(self):
        self.commit_branch('feat/sub', {'calc.py': 'def add(a, b):\n    return a - b\n\n\ndef sub(a, b):\n    return a - b\n',
                                        '.fleet/verify.sh': 'echo recipe ran without a test count\n'})
        strict = self.run_tool(str(ROOT / 'coldverify'), '--ci', '--builder-model', criteria_shim.BUILDER, '--claim', 'sub subtracts')
        self.assertEqual(strict.returncode, 2, strict.stdout + strict.stderr)
        compat = self.run_tool(str(ROOT / 'coldverify'), '--compat', '--ci')
        output = compat.stdout + compat.stderr
        self.assertEqual(compat.returncode, 0, output)
        self.assertNotIn('Repro gate', output)

    def test_compat_fails_a_bug_fix_without_a_repro_unless_it_opts_out(self):
        self.commit_branch('fix/add-only', {'calc.py': 'def add(a, b):\n    return a + b\n'})
        missing = self.run_tool(str(ROOT / 'coldverify'), '--compat', '--ci')
        self.assertEqual(missing.returncode, 1, missing.stdout + missing.stderr)
        self.assertIn('FAIL: --compat', missing.stderr)
        opted_out = self.run_tool(str(ROOT / 'coldverify'), '--compat', '--ci', '--no-repro-reason', 'needs real hardware')
        self.assertEqual(opted_out.returncode, 0, opted_out.stdout + opted_out.stderr)

    def test_a_js_test_change_is_incomplete_and_the_tests_still_run(self):
        self.commit_branch('fix/js', {'calc.py': 'def add(a, b):\n    return a + b\n', 'test/a.test.js': 'require("node:test")\n'})
        result = self.run_tool(str(ROOT / 'coldverify'), '--ci', '--builder-model', criteria_shim.BUILDER, '--claim', 'add adds')
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 2, output)
        self.assertIn('COLDVERIFY INCOMPLETE', output)
        receipt = json.loads((self.repo / '.cold/verify-receipt.json').read_text())
        self.assertEqual(receipt['checks']['repro'][0], 'INCOMPLETE')
        self.assertIn('coldverify cannot reproduce .js tests yet; pass --no-repro-reason', receipt['checks']['repro'][1])
        self.assertEqual(receipt['checks']['tests'], ['PASS', '1 tests ran fresh and passed'])
        compat = self.run_tool(str(ROOT / 'coldverify'), '--compat', '--ci')
        self.assertEqual(compat.returncode, 1, compat.stdout + compat.stderr)
        self.assertIn('FAIL: --compat', compat.stderr)

    def verdicts(self):
        strict = self.run_tool(str(ROOT / 'coldverify'), '--ci', '--builder-model', criteria_shim.BUILDER, '--claim', 'add adds')
        compat = self.run_tool(str(ROOT / 'coldverify'), '--compat', '--ci')
        preview = self.run_tool(str(ROOT / 'coldverify'), '--preview')
        verdict = [line.split(':')[0] for line in (strict.stdout + strict.stderr).splitlines() if line.startswith('COLDVERIFY ')]
        return strict.returncode, verdict[-1:], compat.returncode, preview.returncode, preview.stdout

    def test_preview_messages_leave_real_run_verdicts_alone(self):
        strict, verdict, compat, preview, output = self.verdicts()
        self.assertEqual((strict, verdict, compat, preview), (2, ['COLDVERIFY INCOMPLETE'], 0, 2), output)
        self.assertIn('does not support --preview', output)
        self.git('rm', '-q', '.fleet/verify.sh')
        self.git('commit', '-qm', 'Fix add without a recipe')
        strict, verdict, compat, preview, output = self.verdicts()
        self.assertEqual((strict, verdict, compat, preview), (2, ['COLDVERIFY INCOMPLETE'], 0, 2), output)
        self.assertIn('no .fleet/verify.sh recipe', output)

    def test_a_recipe_whose_only_subtest_skipped_is_incomplete_even_with_a_red_green_repro(self):
        regression = (self.repo / 'tests/test_calc.py').read_text()
        self.git('switch', '-q', 'main')
        self.write('checks/test_external.py', 'import unittest\nclass External(unittest.TestCase):\n'
                   '    def test_external(self):\n        with self.subTest(case=1):\n'
                   '            self.skipTest("dependency unavailable")\n')
        self.git('add', '-A')
        self.git('commit', '-qm', 'Add external checks')
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.commit_branch('fix/add-external', {
            'calc.py': 'def add(a, b):\n    return a + b\n',
            'tests/test_calc.py': regression,
            '.fleet/verify.sh': 'set -euo pipefail\npython3 "$COLD_VERIFY_UNITTEST" discover -s checks\n'})
        result = self.run_tool(str(ROOT / 'coldverify'), '--ci', '--builder-model', criteria_shim.BUILDER, '--claim', 'add adds')
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 2, output)
        self.assertIn('COLDVERIFY INCOMPLETE', output)
        receipt = json.loads((self.repo / '.cold/verify-receipt.json').read_text())
        self.assertEqual(receipt['checks']['repro'][0], 'INCOMPLETE')
        self.assertIn('python-coverage-not-runner-observed', receipt['checks']['repro'][1])
        self.assertIn('tests/test_calc.py::Add.test_add: base=1 head=0 (unproven)', output)
        self.assertEqual(receipt['checks']['tests'], ['INCOMPLETE', 'the JUnit XML records no test that ran'])

    def use_recipe_on_main(self, recipe):
        self.git('switch', '-q', 'main')
        self.write('.fleet/verify.sh', recipe)
        self.git('add', '-A')
        self.git('commit', '-qm', 'Change the recipe')
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')

    def test_pytest_bug_fix_replays_with_the_right_counts(self):
        recipe = (f'{shlex.quote(sys.executable)} "$COLD_VERIFY_PYTEST" -q\n'
                  if importlib.util.find_spec('pytest') else PYTEST_RECIPE)
        self.use_recipe_on_main(recipe)
        self.commit_branch('fix/add-pytest', {'calc.py': 'def add(a, b):\n    return a + b\n',
                                              'tests/test_calc_pytest.py': 'import calc\n\n\ndef test_add():\n    assert calc.add(2, 3) == 5\n'})
        result = self.run_tool(str(ROOT / 'coldverify'), '--ci', '--builder-model', criteria_shim.BUILDER, '--claim', 'add adds')
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 2, output)
        self.assertIn('COLDVERIFY INCOMPLETE', output)
        receipt = json.loads((self.repo / '.cold/verify-receipt.json').read_text())
        self.assertEqual(receipt['checks']['repro'][0], 'INCOMPLETE')
        self.assertIn('python-coverage-not-runner-observed', receipt['checks']['repro'][1])
        self.assertEqual(receipt['checks']['tests'], ['PASS', '2 tests ran fresh and passed'])
        repro = json.loads((self.repo / '.cold/verify-repro.json').read_text())
        self.assertEqual([(t['name'], t['base_exit'], t['head_exit']) for t in repro['tests']],
                         [('tests/test_calc_pytest.py::test_add', 1, 0)])
        self.assertNotIn('ssh', self.shim_calls())

    def test_pytest_function_that_passes_on_base_stays_a_failure(self):
        self.use_recipe_on_main(PYTEST_RECIPE)
        self.commit_branch('fix/add-pytest-green', {'calc.py': 'def add(a, b):\n    return a + b\n',
                                                    'tests/test_calc_pytest.py': 'import calc\n\n\ndef test_zero():\n    assert calc.add(0, 0) == 0\n'})
        result = self.run_tool(str(ROOT / 'coldverify'), '--ci', '--builder-model', criteria_shim.BUILDER, '--claim', 'add adds')
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1, output)
        self.assertIn('COLDVERIFY FAIL', output)
        receipt = json.loads((self.repo / '.cold/verify-receipt.json').read_text())
        self.assertEqual(receipt['checks']['repro'][0], 'FAIL')
        compat = self.run_tool(str(ROOT / 'coldverify'), '--compat', '--ci')
        self.assertEqual(compat.returncode, 1, compat.stdout + compat.stderr)

    def test_repro_a_a_recipe_that_fakes_its_junit_never_passes(self):
        self.write('app.py', 'def login(expired):\n    raise RuntimeError("login broken")\n\n\nVERSION = 1\n')
        self.write('tests/test_app.py', 'import sys, unittest\nsys.path.insert(0, ".")\nimport app\n'
                   'class Login(unittest.TestCase):\n    def test_login(self):\n        self.assertTrue(app.login(True))\n')
        self.use_recipe_on_main('set -euo pipefail\npython3 "$COLD_VERIFY_UNITTEST" discover -s tests -v\n')
        fake = ('printf \'<testsuite><testcase name="a"/><testcase name="b"/></testsuite>\' '
                '> "$COLD_VERIFY_JUNIT_DIR/fake.xml"\n')
        forged = (fake + 'python3 -c \'import hashlib, hmac, os, sys; p = sys.argv[1]; '
                  'open(p + ".sig", "w").write(hmac.new(os.environ["COLD_VERIFY_JUNIT_KEY"].encode(), '
                  'open(p, "rb").read(), hashlib.sha256).hexdigest())\' "$COLD_VERIFY_JUNIT_DIR/fake.xml"\n')
        expected = {'unsigned': ('INCOMPLETE', "JUnit XML not written by coldverify's test adapter"),
                    'forged': ('PASS', '2 tests ran fresh and passed')}
        for name, recipe in (('unsigned', fake), ('forged', forged)):
            with self.subTest(recipe=name):
                self.commit_branch(f'fix/login-{name}', {
                    'app.py': 'def login(expired):\n    raise RuntimeError("login broken")\n\n\nVERSION = 2\n',
                    'tests/test_fixed.py': 'import sys, unittest\nsys.path.insert(0, ".")\nimport app\n'
                                           'class Fixed(unittest.TestCase):\n    def test_login_fixed(self):\n'
                                           '        self.assertEqual(app.VERSION, 2)\n',
                    '.fleet/verify.sh': recipe + 'exit 0\n'})
                result = self.run_tool(str(ROOT / 'coldverify'), '--ci', '--builder-model', criteria_shim.BUILDER, '--claim',
                                       "Fix login failure when a user's session has expired")
                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, 2, output)
                self.assertNotIn('COLDVERIFY PASS', output)
                receipt = json.loads((self.repo / '.cold/verify-receipt.json').read_text())
                self.assertEqual(receipt['run']['result'], expected[name][0])
                self.assertIn(expected[name][1], receipt['run']['reason'])
                if name == 'forged':
                    self.assertEqual(receipt['checks']['tamper'], ['INCOMPLETE', 'recipe changed; needs review: .fleet/verify.sh'])
                else:
                    self.assertEqual(receipt['checks']['tests'][0], 'INCOMPLETE')

    def test_an_explicit_ssh_backend_still_dispatches_over_ssh(self):
        result = self.run_tool(str(ROOT / 'coldverify'), '--ci', '--builder-model', criteria_shim.BUILDER, '--claim', 'add adds',
                               COLD_VERIFY_BACKEND='ssh:runner-a')
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('ssh -o BatchMode=yes runner-a', self.shim_calls())


if __name__ == '__main__':
    unittest.main()
