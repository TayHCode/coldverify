import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from test_verify_filter import FakeCodex
IDENTITY = ['-c', 'user.name=t', '-c', 'user.email=t@localhost']
READS_HIDDEN = 'assert open("hidden.txt").read() == "good\\n", "hidden input changed"'


class SkipNeverPasses(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name, 'repo')
        self.repo.mkdir()
        self.git('init', '-q')
        (self.repo / '.fleet').mkdir()
        (self.repo / '.fleet/verify.sh').write_text('recipe\n')
        (self.repo / 'declared.txt').write_text('declared\n')
        (self.repo / 'hidden.txt').write_text('good\n')
        self.commit('first')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *IDENTITY, *args], text=True, capture_output=True, check=True).stdout.strip()

    def commit(self, message):
        self.git('add', '-A')
        self.git('commit', '-q', '-m', message)

    def verify(self, *flags, command=READS_HIDDEN):
        step = [sys.executable, str(ROOT / 'ci/verify_cache.py'), 'step', 'reads hidden', '--input', 'declared.txt',
                '--', sys.executable, '-c', command]
        result = subprocess.run([sys.executable, str(ROOT / 'ci/verify_trace.py'), '--root', str(self.repo), *flags, '--', *step],
                                cwd=self.repo, text=True, capture_output=True)
        trace = [json.loads(line) for line in (self.repo / '.cold/verify-trace.jsonl').read_text().splitlines()]
        return result, [row['status'] for row in trace]

    def entry(self):
        (path,) = (self.repo / '.cold/verify-cache').glob('*.json')
        return path, json.loads(path.read_text())

    def assertRanAndFailed(self, outcome):
        result, statuses = outcome
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(statuses, ['fail'])
        self.assertNotIn('::skip::', result.stdout)

    def test_gate_runs_when_an_undeclared_input_is_committed_after_a_pass(self):
        self.assertEqual(self.verify('--gate')[1], ['pass'])
        (self.repo / 'hidden.txt').write_text('bad\n')
        self.commit('change an input the step reads but does not declare')
        self.assertRanAndFailed(self.verify('--gate'))

    def test_gate_runs_when_an_undeclared_input_is_uncommitted(self):
        self.assertEqual(self.verify('--gate')[1], ['pass'])
        (self.repo / 'hidden.txt').write_text('bad\n')
        self.assertRanAndFailed(self.verify('--gate'))

    def test_gate_runs_when_the_undeclared_input_is_gitignored(self):
        (self.repo / '.gitignore').write_text('hidden.txt\n')
        self.git('rm', '-q', '--cached', 'hidden.txt')
        self.commit('ignore hidden.txt')
        self.assertEqual(self.verify('--gate')[1], ['pass'])
        (self.repo / 'hidden.txt').write_text('bad\n')
        self.assertRanAndFailed(self.verify('--gate'))

    def test_gate_ignores_an_entry_recorded_for_another_tree(self):
        self.assertEqual(self.verify('--gate')[1], ['pass'])
        (self.repo / 'hidden.txt').write_text('bad\n')
        self.commit('bad input')
        path, entry = self.entry()
        self.assertTrue(entry['tree'])
        self.assertRanAndFailed(self.verify('--gate'))

    def test_gate_ignores_an_entry_with_no_tree(self):
        self.verify()
        path, entry = self.entry()
        path.write_text(json.dumps({**entry, 'tree': None}))
        (self.repo / 'hidden.txt').write_text('bad\n')
        self.commit('bad input')
        self.assertRanAndFailed(self.verify('--gate'))

    def test_run_without_a_prior_pass_executes_every_step(self):
        result, statuses = self.verify('--gate')
        self.assertEqual((result.returncode, statuses), (2, ['pass']))
        self.assertNotIn('::skip::', result.stdout)
        self.assertIn('TESTS INCOMPLETE: no test count found', result.stdout)

    def test_a_failing_run_is_never_all_skips(self):
        recipe = 'print("::skip::fake step"); raise SystemExit(1)'
        result = subprocess.run([sys.executable, str(ROOT / 'ci/verify_trace.py'), '--root', str(self.repo), '--',
                                 sys.executable, '-c', recipe], cwd=self.repo, text=True, capture_output=True)
        trace = [json.loads(line) for line in (self.repo / '.cold/verify-trace.jsonl').read_text().splitlines()]
        self.assertEqual(result.returncode, 1)
        self.assertIn('fail', [row['status'] for row in trace])


SKIPS_EVERYTHING = f"""
import sys
sys.path.insert(0, {str(ROOT / 'ci')!r})
import verify_filter as f
filters = (*f.FILTERS[:-1], f.Filter('canary-model', lambda test, change: f.Decision(False, 'canary says unrelated'), model=True))
command = [sys.executable, '-m', 'unittest', 'discover', '-s', '{{dir}}', '-p', '{{name}}']
raise SystemExit(f.run(['tests/test_*.py'], command, '.', filters=filters))
"""
USES_LUNA = SKIPS_EVERYTHING.replace("filters = (*f.FILTERS[:-1], f.Filter('canary-model', lambda test, change: f.Decision(False, 'canary says unrelated'), model=True))",
                                     'filters = f.FILTERS')
BROKEN_TEST = 'import unittest\nclass T(unittest.TestCase):\n    def test_broken(self):\n        self.fail("broken")\n'


class FilterNeverPasses(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name, 'repo')
        (self.repo / 'tests').mkdir(parents=True)
        self.git('init', '-q', '-b', 'main')
        (self.repo / 'tests/test_broken.py').write_text(BROKEN_TEST)
        (self.repo / 'src.py').write_text('x = 1\n')
        self.commit('main has a broken test')
        self.git('checkout', '-q', '-b', 'feature')
        (self.repo / 'src.py').write_text('x = 2\n')
        self.commit('feature edits src only')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *IDENTITY, *args], text=True, capture_output=True, check=True).stdout.strip()

    def commit(self, message):
        self.git('add', '-A')
        self.git('commit', '-q', '-m', message)

    def verify(self, script=SKIPS_EVERYTHING, **env):
        result = subprocess.run([sys.executable, str(ROOT / 'ci/verify_trace.py'), '--root', str(self.repo), '--',
                                 sys.executable, '-c', script], cwd=self.repo, text=True, capture_output=True,
                                env={**os.environ, **env})
        trace = [json.loads(line) for line in (self.repo / '.cold/verify-trace.jsonl').read_text().splitlines()]
        return result, [row['status'] for row in trace]

    def test_skipping_every_test_is_incomplete_not_a_pass(self):
        result, statuses = self.verify()
        self.assertEqual(result.returncode, 2)
        self.assertIn('skip  tests/test_broken.py  [canary-model] canary says unrelated', result.stdout)
        self.assertIn('INCOMPLETE: nothing left to run', result.stdout)
        self.assertNotIn('pass', statuses)

    def test_a_changed_test_file_runs_despite_a_model_that_skips_everything(self):
        (self.repo / 'tests/test_broken.py').write_text(BROKEN_TEST + '\n')
        result, statuses = self.verify()
        self.assertEqual(result.returncode, 1)
        self.assertIn('FAILED (1): tests tests/test_*.py (1 of 1)', result.stdout)
        self.assertEqual(statuses, ['fail'])

    def test_a_config_change_runs_everything_despite_the_model(self):
        (self.repo / 'pyproject.toml').write_text('[project]\n')
        result, statuses = self.verify()
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('canary says unrelated', result.stdout)
        self.assertEqual(statuses, ['fail'])

    def test_an_untrusted_diff_disables_the_model_and_runs_everything(self):
        result, statuses = self.verify(COLD_VERIFY_UNTRUSTED='1')
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('canary says unrelated', result.stdout)
        self.assertIn('model_calls=0', result.stdout)
        self.assertEqual(statuses, ['fail'])

    def test_without_a_main_branch_the_diff_is_unknown_and_everything_runs(self):
        self.git('branch', '-m', 'main', 'trunk')
        result, statuses = self.verify()
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('canary says unrelated', result.stdout)
        self.assertEqual(statuses, ['fail'])

    def luna(self, **fake_env):
        fake = FakeCodex(self)
        return self.verify(USES_LUNA, **{**fake.environ(), **fake_env})

    def test_a_luna_that_rates_everything_unrelated_is_incomplete_not_a_pass(self):
        result, statuses = self.luna(FAKE_CODEX_REPLY='{"probability": 0.0, "reason": "docs only"}')
        self.assertEqual(result.returncode, 2)
        self.assertIn('skip  tests/test_broken.py  [luna] rated 0.00 likely affected: docs only', result.stdout)
        self.assertIn('INCOMPLETE: nothing left to run', result.stdout)
        self.assertNotIn('pass', statuses)

    def test_a_luna_that_is_unsure_runs_the_broken_test_and_fails(self):
        result, statuses = self.luna(FAKE_CODEX_REPLY='{"probability": 0.2, "reason": "unsure"}')
        self.assertEqual(result.returncode, 1)
        self.assertIn('fail  tests/test_broken.py', result.stdout)
        self.assertEqual(statuses, ['fail'])

    def test_a_luna_that_errors_runs_the_broken_test_and_fails(self):
        result, statuses = self.luna(FAKE_CODEX_FAIL='1')
        self.assertEqual(result.returncode, 1)
        self.assertIn('model_failures=1', result.stdout)
        self.assertEqual(statuses, ['fail'])


if __name__ == '__main__':
    unittest.main()
