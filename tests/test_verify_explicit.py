import json
import os
from pathlib import Path
import platform
import shlex
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
import verify_flows
import verify_repro
import verify_repro_transport


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

    def native_repro(self):
        details = ('Fix value', '', self.base, self.head)
        with mock.patch.dict(os.environ, COLD_VERIFY_BACKEND='local'), \
                mock.patch.object(verify_repro_transport, 'bug_fix', return_value=(details, 'fix', True)):
            return verify_repro_transport.run(self.repo, exercised=True)

    def strict_local_fixture(self):
        dependency = self.folder / 'dependency'
        dependency.mkdir()
        (dependency / 'expected.py').write_text('EXPECTED = 2\n')
        protected = self.repo / '.cold/verify-receipt.json'
        build = f'''from pathlib import Path
import tempfile
import zipfile

with tempfile.NamedTemporaryFile() as temporary:
    assert Path(temporary.name).parent == Path.home()
Path('setup-home').write_text(str(Path.home()))
try:
    Path({str(protected)!r}).write_text('changed during setup')
except OSError:
    pass
else:
    raise RuntimeError('the receipt was writable during setup')

def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    wheel_name = 'fixture_value-0.0.1-py3-none-any.whl'
    dist_info = 'fixture_value-0.0.1.dist-info'
    with zipfile.ZipFile(Path(wheel_directory) / wheel_name, 'w') as wheel:
        wheel.write('value.py')
        wheel.writestr(dist_info + '/METADATA', 'Metadata-Version: 2.1\\nName: fixture-value\\nVersion: 0.0.1\\n')
        wheel.writestr(dist_info + '/WHEEL', 'Wheel-Version: 1.0\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n')
        wheel.writestr(dist_info + '/RECORD', ''.join(name + ',,\\n' for name in wheel.namelist()))
    return wheel_name
'''
        probe = f'''import sys
from pathlib import Path
import tempfile
import unittest
from expected import EXPECTED
from value import value

def workspace():
    home = Path.home()
    with tempfile.NamedTemporaryFile() as temporary:
        assert Path(temporary.name).parent == home
    marker = home / 'first-run'
    with marker.open('x'):
        pass
    try:
        Path({str(protected)!r}).write_text('changed')
    except OSError:
        pass
    else:
        raise RuntimeError('the receipt was writable')

workspace()

class Value(unittest.TestCase):
    def test_value(self):
        self.assertNotEqual(sys.prefix, sys.base_prefix)
        self.assertNotEqual(Path('setup-home').read_text(), str(Path.home()))
        self.assertEqual(value(), EXPECTED)
'''
        flow = f'''#!/bin/sh
set -eu
# This flow needs no systemctl.
[ -z "${{XDG_RUNTIME_DIR:-}}" ] || exit 2
[ "$HOME" = "$TMPDIR" ] || exit 2
temp=$(mktemp)
case "$temp" in "$HOME"/*) ;; *) exit 2 ;; esac
(set -C; : > "$HOME/first-run") || exit 2
if (printf changed > {shlex.quote(str(protected))}) 2>/dev/null; then exit 2; fi
printf '%s' "$HOME" > "$COLD_FLOW_ARTIFACTS/home"
grep -q 'return 2' value.py
'''
        self.base = self.commit('install project on base', {
            'value.py': 'def value():\n    return 1\n', 'tests/test_value.py': '', '.gitignore': '.cold/\n',
            'build_fixture.py': build,
            'pyproject.toml': '[build-system]\nrequires = []\nbuild-backend = "build_fixture"\nbackend-path = ["."]\n'
                              f'[tool.cold.repro]\nno-deps = true\npythonpath = [{json.dumps(str(dependency))}]\n',
        })
        flow_path = self.repo / '.fleet/flows/value'
        flow_path.parent.mkdir(parents=True)
        flow_path.touch(mode=0o755)
        self.head = self.commit('fix in isolated replay', {'value.py': 'def value():\n    return 2\n',
                                'tests/test_value.py': probe, '.fleet/flows/value': flow})
        protected.parent.mkdir()
        protected.write_text('verifier evidence')
        return protected

    @needs_bwrap
    def test_native_repro_and_flows_get_private_workspaces_and_keep_the_receipt(self):
        protected = self.strict_local_fixture()
        code = self.native_repro()
        repro = json.loads((self.repo / verify_flows.REPRO).read_text())
        self.assertEqual((code, repro['result']), (2, 'INCOMPLETE'), repro)
        [test] = repro['tests']
        self.assertEqual((test['base_exit'], test['head_exit'], test['kind']), (1, 0, 'unproven'))
        self.assertIn('1 != 2', test['base_output'])
        self.assertEqual(protected.read_text(), 'verifier evidence')

        flows = verify_flows.run(self.repo)
        self.assertEqual(flows['result'], 'PASS', flows)
        [flow] = flows['flows']
        self.assertEqual((flow['base_exit'], flow['head_exit'], flow['kind']), (1, 0, 'proof'))
        kept = self.repo / verify_flows.ARTIFACTS / 'value'
        self.assertNotEqual((kept / 'base/artifacts/home').read_text(), (kept / 'head/artifacts/home').read_text())
        self.assertEqual(protected.read_text(), 'verifier evidence')

    def test_native_repro_and_flows_are_incomplete_if_bubblewrap_is_missing(self):
        with mock.patch.object(platform, 'system', return_value='Linux'), \
                mock.patch.object(shutil, 'which', return_value=None):
            self.assertEqual(self.native_repro(), 2)
            repro = json.loads((self.repo / verify_flows.REPRO).read_text())
            self.assertEqual((repro['result'], repro['tests']), ('INCOMPLETE', []))
            self.assertIn('bubblewrap (bwrap) is not installed', repro['error'])
            flow = self.repo / '.fleet/flows/value'
            flow.parent.mkdir(parents=True)
            flow.touch(mode=0o755)
            self.head = self.commit('declare flow', {'.fleet/flows/value': '#!/bin/sh\ntrue\n'})
            (self.repo / verify_flows.REPRO).write_text(json.dumps({**repro, 'head': self.head}))
            flows = verify_flows.run(self.repo)
        self.assertEqual((flows['result'], flows['flows']), ('INCOMPLETE', []))
        self.assertIn('bubblewrap (bwrap) is not installed', flows['reason'])

    def test_a_guest_systemd_flow_requires_an_observed_exit(self):
        flow = self.repo / 'flow'
        flow.write_text('#!/bin/sh\nsystemctl --user show-environment\n')
        flow.chmod(0o755)
        with mock.patch('verify_context.coldbox_guest', return_value='guest'), \
                mock.patch('verify_repro.sandbox', return_value=['bwrap', '--']), \
                mock.patch('verify_repro.run_sandboxed', return_value=1):
            with self.assertRaisesRegex(verify_repro.NoRepro, 'did not return the sandbox exit status'):
                verify_flows.run_flow(flow, self.repo, 'head', self.folder, self.folder / 'saved',
                                      sandbox_root=self.repo)

    @needs_bwrap
    def test_strict_run_records_a_real_local_flow_proof(self):
        flow = self.repo / '.fleet/flows/feature'
        flow.parent.mkdir(parents=True)
        flow.touch(mode=0o755)
        self.commit('add feature flow', {'.fleet/flows/feature': '#!/bin/sh\ntest -f feature.txt\n',
                                         '.fleet/verify.sh': '#!/bin/sh\nexit 0\n', 'feature.txt': 'on\n'})
        subprocess.run(['git', 'update-ref', 'refs/remotes/origin/main', self.base], cwd=self.repo, check=True)
        result = subprocess.run([sys.executable, str(ROOT / 'coldverify'), '--ci', '--claim', 'feature.txt exists'],
                                cwd=self.repo, env=self.env, capture_output=True, text=True)
        receipt = self.receipt(result)
        [recorded] = receipt['flows']['flows']
        self.assertEqual((recorded['name'], recorded['base_exit'], recorded['head_exit'], recorded['kind']),
                         ('feature', 1, 0, 'proof'))

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
    def test_suite_runs_and_records_the_exact_head_tree(self):
        import verify_explicit
        (self.repo / '.cold').mkdir()
        tree = subprocess.check_output(['git', '-C', str(self.repo), 'rev-parse', 'HEAD^{tree}'], text=True).strip()
        suite = (f'test "$(git rev-parse HEAD)" = {self.head} && test "$(git rev-parse HEAD^{{tree}})" = {tree}'
                 ' && python3 "$COLD_VERIFY_UNITTEST" discover')
        with mock.patch.dict(os.environ, COLDBOX_GUEST='0', COLD_VERIFY_BACKEND='local'):
            try:
                code = verify_explicit.run(self.repo, self.base, self.head, COMMAND, None, suite)
            except TypeError as error:
                self.fail(str(error))
        self.assertEqual(code, 0)
        run = json.loads((self.repo / '.cold/verify-result.json').read_text())
        self.assertEqual((run['result'], run['head'], run['tree'], run['tests_ran']), ('PASS', self.head, tree, 1))

    def test_mismatched_suite_revision_is_incomplete_without_running_the_suite(self):
        expected = self.head
        actual = self.commit('other revision', {'README': 'different tree\n'})
        result = self.coldverify('--suite-command', 'echo suite-started')
        receipt = self.receipt(result)
        self.assertEqual((receipt['result'], result.returncode, receipt['commit']), ('INCOMPLETE', 2, actual))
        self.assertEqual(receipt['checks']['tests'][0], 'INCOMPLETE')
        self.assertIn(expected, receipt['checks']['repro'][1])
        self.assertIsNone(receipt['run'])
        self.assertNotIn('suite-started\n', result.stdout)

    @needs_bwrap
    def test_installed_untracked_tests_do_not_count_as_the_requested_suite(self):
        receipt = self.receipt(self.coldverify(
            '--install-command', 'case "$PWD" in */suite) touch test_added.py ;; esac',
            '--suite-command', 'python3 "$COLD_VERIFY_UNITTEST" discover',
        ))
        self.assertEqual(receipt['checks']['repro'][0], 'PASS')
        self.assertEqual(receipt['checks']['tests'][0], 'INCOMPLETE')
        self.assertIn(self.head, receipt['checks']['tests'][1])
        self.assertEqual(receipt['run']['tests_ran'], 0)

    def test_a_suite_pass_for_another_tree_does_not_pass_the_receipt(self):
        import contextlib
        import io
        import verify_receipt

        (self.repo / '.cold').mkdir()
        other_tree = subprocess.check_output(
            ['git', '-C', str(self.repo), 'rev-parse', f'{self.base}^{{tree}}'], text=True,
        ).strip()
        run = {'schema_version': 1, 'result': 'PASS', 'reason': '1 tests ran fresh and passed',
               'head': self.head, 'tree': other_tree, 'tests_ran': 1, 'tests_source': 'junit',
               'exit_code': 0, 'command': ['true'], 'steps': []}
        (self.repo / '.cold/verify-result.json').write_text(json.dumps(run))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = verify_receipt.finish(self.repo, 'claim', None, 0, ['coldverify'],
                                         {'version': '0.1.0', 'commit': None}, suite_revision=self.head)
        receipt = self.receipt(subprocess.CompletedProcess([], code, output.getvalue(), ''))
        self.assertEqual(receipt['checks']['tests'][0], 'INCOMPLETE')
        self.assertEqual(receipt['run']['result'], 'INCOMPLETE')

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
