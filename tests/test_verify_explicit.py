import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from test_verify_receipt import schema_errors

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ci'))
TEST = 'import unittest\n\nfrom value import value\n\n\nclass Value(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(value(), 2)\n'
COMMAND = 'python3 -m unittest tests.test_value'
from verify_context import coldbox_guest


def bwrap_problem():
    bwrap = shutil.which('bwrap')
    if bwrap is None:
        return 'bwrap is not installed'
    try:
        probe = subprocess.run([bwrap, '--unshare-all', '--ro-bind', '/', '/', 'true'], capture_output=True, text=True)
    except OSError as error:
        return str(error)
    return probe.returncode and (probe.stderr.strip() or f'bwrap exited {probe.returncode}')


def sandbox_skip_reason():
    if os.environ.get('GITHUB_ACTIONS') == 'true' or coldbox_guest():
        return None
    return bwrap_problem()


problem = sandbox_skip_reason()
needs_bwrap = unittest.skipIf(problem, f'explicit mode runs the test command only in bubblewrap, which cannot start here: {problem}')


class ExplicitMode(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.repo = self.folder / 'repo'
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.base = self.commit('base', {'value.py': 'def value():\n    return 1\n', 'tests/__init__.py': ''})
        self.head = self.commit('fix', {'value.py': 'def value():\n    return 2\n', 'tests/test_value.py': TEST})
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(('COLD_', 'GIT_', 'GITHUB_'))}
        self.env.update(COLDBOX_GUEST='0', COLD_VERIFY_BACKEND='local', GITHUB_TOKEN='ghp_' + 'x' * 36)

    def commit(self, message, files):
        for name, text in files.items():
            (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / name).write_text(text)
        subprocess.run(['git', '-C', str(self.repo), 'add', '-A'], check=True)
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
                        'commit', '-q', '-m', message], check=True)
        return subprocess.check_output(['git', '-C', str(self.repo), 'rev-parse', 'HEAD'], text=True).strip()

    def coldverify(self, *args, command=COMMAND, **env):
        return subprocess.run([sys.executable, str(ROOT / 'coldverify'), '--claim', 'value() returns 2',
                               '--base', self.base, '--head', self.head, '--test-command', command, *args],
                              cwd=self.repo, env={**self.env, **env}, text=True, capture_output=True)

    def receipt(self, result):
        path = self.repo / '.cold/verify-receipt.json'
        self.assertTrue(path.is_file(), result.stdout + result.stderr)
        receipt = json.loads(path.read_text())
        self.assertEqual(schema_errors(receipt), [])
        return receipt

    @needs_bwrap
    def test_the_head_test_fails_on_base_and_passes_on_head_without_the_callers_secrets(self):
        result = self.coldverify(command=f'[ -z "${{GITHUB_TOKEN:-}}" ] && echo no-caller-token; {COMMAND}')
        receipt = self.receipt(result)
        test = receipt['repro']['tests'][0]
        self.assertEqual(receipt['checks']['repro'], ['PASS', '1 repro tests failed on base and passed on head'])
        self.assertEqual((test['name'], test['base_exit'], test['head_exit'], test['kind']), ('test command', 1, 0, 'proof'))
        self.assertIn('AssertionError: 1 != 2', test['base_output'])
        self.assertEqual((receipt['repro']['base'], receipt['repro']['head'], receipt['commit']), (self.base, self.head, self.head))
        self.assertEqual(receipt['checks']['tests'], ['INCOMPLETE', 'the tests did not run'])
        self.assertEqual((receipt['result'], result.returncode), ('INCOMPLETE', 2))
        self.assertIn('no-caller-token', test['base_output'])
        self.assertIn('no-caller-token', test['head_output'])

    @needs_bwrap
    def test_a_head_that_still_fails_is_a_fail(self):
        self.head = self.commit('broken', {'value.py': 'def value():\n    return 3\n'})
        result = self.coldverify()
        receipt = self.receipt(result)
        self.assertEqual(receipt['repro']['tests'][0]['kind'], 'regression')
        self.assertEqual((receipt['result'], result.returncode), ('FAIL', 1))

    @needs_bwrap
    def test_a_test_that_already_passes_on_base_is_a_fail(self):
        self.base = self.commit('already', {'value.py': 'def value():\n    return 1 + 1\n'})
        self.head = self.commit('test', {'tests/test_value.py': TEST + '        self.assertTrue(value())\n'})
        result = self.coldverify()
        receipt = self.receipt(result)
        self.assertEqual(receipt['repro']['tests'][0]['kind'], 'acceptance')
        self.assertEqual((receipt['result'], result.returncode), ('FAIL', 1))

    @needs_bwrap
    def test_an_exit_other_than_one_on_base_is_incomplete(self):
        result = self.coldverify(command=f'git rev-parse HEAD | grep -q {self.base} && exit 3; {COMMAND}')
        receipt = self.receipt(result)
        self.assertEqual(receipt['repro']['tests'][0]['kind'], 'unproven')
        self.assertEqual(receipt['checks']['repro'][0], 'INCOMPLETE')
        self.assertEqual(result.returncode, 2)

    def test_the_command_does_not_run_outside_a_guest_without_the_local_backend(self):
        ran = self.folder / 'ran'
        for backend in ('', 'ssh:runner-a'):
            with self.subTest(backend=backend):
                result = self.coldverify(command=f'touch {ran}', COLD_VERIFY_BACKEND=backend)
                receipt = self.receipt(result)
                self.assertEqual(receipt['repro']['tests'], [])
                self.assertIn('the test command is untrusted', receipt['checks']['repro'][1])
                self.assertEqual(result.returncode, 2)
                self.assertFalse(ran.exists())

    def test_inputs_that_do_not_describe_the_checkout_are_incomplete(self):
        fix = self.head
        untested = self.commit('docs', {'README': 'notes\n'})
        orphan = subprocess.check_output(['git', '-C', str(self.repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
                                          'commit-tree', untested + '^{tree}', '-m', 'orphan'], text=True).strip()
        cases = (('no test file is added or changed', fix, untested), ('is not an ancestor', orphan, untested))
        for reason, self.base, self.head in cases:
            with self.subTest(reason=reason):
                result = self.coldverify()
                self.assertIn(reason, self.receipt(result)['checks']['repro'][1])
                self.assertEqual(result.returncode, 2)

    @needs_bwrap
    def test_run_records_the_proof_as_the_repro(self):
        import verify_explicit
        (self.repo / '.cold').mkdir()
        with mock.patch.dict(os.environ, COLDBOX_GUEST='0', COLD_VERIFY_BACKEND='local'):
            code = verify_explicit.run(self.repo, self.base, self.head, COMMAND)
        repro = json.loads((self.repo / '.cold/verify-repro.json').read_text())
        self.assertEqual((code, repro['result'], repro['tests'][0]['kind']), (0, 'PASS', 'proof'))

    @needs_bwrap
    def test_the_base_run_cannot_change_the_head_checkout(self):
        self.head = self.commit('broken', {'value.py': 'def value():\n    return 3\n'})
        fixed = 'def value():\\n    return 2\\n'
        result = self.coldverify(command=f'git rev-parse HEAD | grep -q {self.base} && mkdir -p ../head && '
                                         f'printf "{fixed}" > ../head/value.py; {COMMAND}')
        self.assertNotEqual(self.receipt(result)['checks']['repro'][0], 'PASS')

    @needs_bwrap
    def test_a_process_the_base_run_leaves_behind_cannot_change_the_head_checkout(self):
        self.head = self.commit('broken', {'value.py': 'def value():\n    return 3\n'})
        fixed = 'def value():\\n    return 2\\n'
        lurker = (f'(i=0; while [ $i -lt 1000 ]; do [ -f ../head/value.py ] && printf "{fixed}" > ../head/value.py; '
                  'i=$((i + 1)); sleep 0.01; done) > /dev/null 2>&1 &')
        result = self.coldverify(command=f'git rev-parse HEAD | grep -q {self.base} && {lurker} {COMMAND}')
        self.assertNotEqual(self.receipt(result)['checks']['repro'][0], 'PASS', result.stdout)

    @needs_bwrap
    def test_the_command_cannot_forge_the_suite_or_flow_evidence(self):
        forged = f"mkdir -p {self.repo}/.cold/flows; echo '{{\"schema_version\": 0}}' | tee {self.repo}/.cold/verify-result.json > {self.repo}/.cold/verify-flows.json; "
        receipt = self.receipt(self.coldverify(command=forged + COMMAND))
        self.assertEqual(receipt['checks']['tests'], ['INCOMPLETE', 'the tests did not run'])
        self.assertNotIn('flows', receipt['checks'])
        self.assertFalse((self.repo / '.cold/flows').exists())

    def test_a_test_path_that_is_not_utf8_is_listed(self):
        (self.repo / os.fsdecode(b'tests/test_\xff.py')).write_text('')
        self.head = self.commit('odd name', {})
        import verify_explicit
        kept, deleted = verify_explicit.changed_tests(self.repo, self.base, self.head)
        self.assertEqual((sorted(kept), deleted), (sorted(['tests/test_value.py', os.fsdecode(b'tests/test_\xff.py')]), []))

    def test_a_checkout_that_is_not_head_is_a_usage_error_without_a_receipt(self):
        subprocess.run(['git', '-C', str(self.repo), 'checkout', '-q', self.base], check=True)
        result = self.coldverify()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(f'HEAD is {self.base}, not --head {self.head}', result.stderr)
        self.assertFalse((self.repo / '.cold/verify-receipt.json').exists())

    def test_bad_arguments_are_usage_errors_without_a_receipt(self):
        tool = [sys.executable, str(ROOT / 'coldverify')]
        cases = ((['--base', self.base[:12], '--head', self.head, '--test-command', 'true'], 'is not a full lowercase commit SHA'),
                 (['--base', self.base, '--head', self.head], 'go together'),
                 (['--base', self.base, '--head', self.head, '--test-command', ' '], 'cannot be empty'),
                 (['--base', self.base, '--head', self.head, '--test-command', 'true', '--compat'], 'cannot be combined with --compat'))
        for args, message in cases:
            with self.subTest(args=args):
                result = subprocess.run([*tool, *args], cwd=self.repo, env=self.env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(message, result.stderr)
                self.assertFalse((self.repo / '.cold/verify-receipt.json').exists())


class SandboxSkip(unittest.TestCase):
    def test_the_sandbox_tests_skip_only_outside_ci_and_coldbox_guests(self):
        here = sys.modules[__name__]
        cases = (({'GITHUB_ACTIONS': ''}, None, 'blocked'), ({'GITHUB_ACTIONS': 'true'}, None, None),
                 ({'GITHUB_ACTIONS': ''}, 'runner-a', None))
        for env, guest, expected in cases:
            with self.subTest(env=env, guest=guest), mock.patch.dict(os.environ, env), \
                    mock.patch.object(here, 'coldbox_guest', return_value=guest), \
                    mock.patch.object(here, 'bwrap_problem', return_value='blocked'):
                self.assertEqual(here.sandbox_skip_reason(), expected)


if __name__ == '__main__':
    unittest.main()
