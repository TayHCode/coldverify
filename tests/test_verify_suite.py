import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HOST = '''
import hashlib, json, os, pathlib, subprocess, sys

args = sys.argv[1:]
assert args[:2] == ['run', '--offline']
suite = pathlib.Path(args[args.index('--trusted-suite') + 1])
receipts = pathlib.Path(args[args.index('--receipts') + 1])
root = pathlib.Path(args[-1])
assert not receipts.is_relative_to(root)
commit, tree = subprocess.check_output(
    ['git', '-C', str(root), 'rev-parse', 'HEAD', 'HEAD^{tree}'], text=True).splitlines()
name = 'cbx-123-abcdef-fixture'
observation = dict(schema='coldbox.suite-observation/v1', commit=commit, tree=tree,
                   suite_sha256=hashlib.sha256(suite.read_bytes()).hexdigest(), tests_ran=2, result='PASS')
receipt = dict(schema='coldbox.receipt/v1', mode='local', id=name, sha=commit, verdict='pass',
               vm=dict(name=name, destroyed=True), suite_observation=observation)
case = os.environ['SUITE_FIXTURE']
if case == 'missing':
    print(json.dumps(receipt))
    sys.exit(0)
if case == 'fail':
    receipt['verdict'] = 'fail'
    observation['result'] = 'FAIL'
elif '=' in case:
    path, value = case.split('=', 1)
    target = receipt
    *parents, field = path.split('.')
    for parent in parents:
        target = target[parent]
    target[field] = json.loads(value)
receipts.mkdir(exist_ok=True)
report = receipts / (name + '.json')
if case == 'outside':
    report = receipts.parent / report.name
if case == 'bad-name':
    report = receipts / 'suite.json'
report.unlink(missing_ok=True)
if case == 'symlink':
    actual = receipts.parent / 'suite-receipt.json'
    actual.write_text(json.dumps(receipt))
    report.symlink_to(actual)
else:
    report.write_text(json.dumps(receipt))
print(f"coldbox: {receipt['verdict'].upper()} receipt {report}", file=sys.stderr)
sys.exit(1 if case in ('fail', 'exit') else 0)
'''


class TrustedSuite(unittest.TestCase):
    def setUp(self):
        base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.repo = base / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('-c', 'user.name=Test', '-c', 'user.email=fixture@localhost',
                 'commit', '--allow-empty', '-qm', 'fixture')
        self.suite = base / 'suite.json'
        self.suite.write_text('[{"argv":["true"],"stdout":""},{"argv":["true"],"stdout":""}]')
        self.host = base / 'coldbox'
        self.host.write_text(f'#!{sys.executable}\n' + HOST)
        self.host.chmod(0o700)
        self.env = {**os.environ, 'GITHUB_ACTIONS': '', 'COLDBOX_GUEST': '0', 'COLDBOX_STATE': str(base),
                    'PATH': str(base) + os.pathsep + os.environ['PATH']}

    def git(self, *args):
        subprocess.run(['git', *args], cwd=self.repo, check=True)

    def run_cli(self, case, *options):
        return subprocess.run([sys.executable, str(ROOT / 'coldverify'), '--trusted-suite', str(self.suite), *options],
                              cwd=self.repo, env={**self.env, 'SUITE_FIXTURE': case}, capture_output=True, text=True)

    def test_host_observed_cases_pass(self):
        result = self.run_cli('healthy')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('TESTS PASS: Coldbox observed 2 tests', result.stdout)
        report = Path(result.stdout.strip().split('Receipt ', 1)[1])
        self.assertTrue(report.is_file())

    def test_host_observed_failure_is_preserved(self):
        result = self.run_cli('fail')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('TESTS FAIL: Coldbox observed a failure', result.stdout)

    def test_unverified_observations_are_incomplete(self):
        groups = {
            'Coldbox did not identify a host receipt (exit 0)': ('missing',),
            'outside the expected host storage': ('outside', 'bad-name', 'symlink'),
            'does not identify this Coldbox run and commit': ('id="other"', 'mode="runner"', 'sha="wrong"'),
            'does not confirm VM teardown': ('vm.name="other"', 'vm.destroyed=false'),
            'does not match the revision and suite': (
                'suite_observation.schema="unknown"', 'suite_observation.commit="wrong"',
                'suite_observation.tree="wrong"', 'suite_observation.suite_sha256="wrong"',
            ),
            'did not observe a positive test count': ('suite_observation.tests_ran=0', 'suite_observation.tests_ran=true'),
            'did not reach a consistent pass or fail verdict': ('verdict="incomplete"', 'exit'),
        }
        for reason, cases in groups.items():
            for case in cases:
                with self.subTest(case=case):
                    result = self.run_cli(case)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn('TESTS INCOMPLETE:', result.stdout)
                    self.assertIn(reason, result.stdout)

    def test_suite_must_come_from_outside_candidate_checkout(self):
        self.suite = self.suite.rename(self.repo / self.suite.name)
        result = self.run_cli('healthy')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('select the suite outside the checkout', result.stdout)

    def test_trusted_suite_cannot_run_from_actions(self):
        self.env['GITHUB_ACTIONS'] = 'true'
        result = self.run_cli('healthy')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('trusted suite requires a Coldbox host', result.stdout)

    def test_trusted_suite_refuses_coldbox_state_and_executable_from_checkout(self):
        original = self.env.copy()
        self.env['COLDBOX_STATE'] = str(self.repo)
        result = self.run_cli('healthy')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('requires Coldbox state outside the checkout', result.stdout)

        self.host.rename(self.repo / self.host.name)
        self.env = {**original, 'PATH': str(self.repo) + os.pathsep + original['PATH']}
        result = self.run_cli('healthy')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('requires a host installation of Coldbox', result.stdout)

    def test_trusted_suite_cannot_be_combined_with_publishing(self):
        result = self.run_cli('healthy', '--publish-check')
        self.assertEqual(result.returncode, 2)
        self.assertIn('--trusted-suite runs on its own', result.stderr)
