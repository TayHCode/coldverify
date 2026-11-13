import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ci'))
import verify_cache

IDENTITY = ['-c', 'user.name=t', '-c', 'user.email=t@localhost']


ONE_CASE = '<testsuite><testcase name="t"/></testsuite>'


class VerifyCache(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name, 'repo')
        self.counter = Path(self.temp.name, 'runs')
        self.repo.mkdir()
        self.git('init', '-q')
        (self.repo / '.fleet').mkdir()
        (self.repo / '.fleet/verify.sh').write_text('recipe one\n')
        (self.repo / 'input.txt').write_text('input one\n')
        self.commit('first')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *IDENTITY, *args], text=True, capture_output=True, check=True).stdout.strip()

    def commit(self, message):
        self.git('add', '-A')
        self.git('commit', '-q', '--allow-empty', '-m', message)

    def verify(self, *flags, command='pass', inputs=('input.txt',), prepare=''):
        step = [sys.executable, str(ROOT / 'ci/verify_cache.py'), 'step', 'check']
        for path in inputs:
            step += ['--input', path]
        step += ['--', sys.executable, '-c', f'import os; open({str(self.counter)!r}, "a").write("x"); {command}; print("Ran 1 test in 0.001s"); '
                 f'open(os.path.join(os.environ["COLD_VERIFY_JUNIT_DIR"], "t.xml"), "w").write({ONE_CASE!r}); '
                 f'import sys; sys.path.insert(0, {str(ROOT / "ci")!r}); import junit_unittest; '
                 'junit_unittest.sign(os.path.join(os.environ["COLD_VERIFY_JUNIT_DIR"], "t.xml"), os.environ["COLD_VERIFY_JUNIT_KEY"])']
        if prepare:
            step = [sys.executable, '-c', f'import subprocess, sys; {prepare}; sys.exit(subprocess.call({step!r}))']
        result = subprocess.run([sys.executable, str(ROOT / 'ci/verify_trace.py'), '--root', str(self.repo), *flags, '--', *step],
                                cwd=self.repo, text=True, capture_output=True)
        trace = [json.loads(line) for line in (self.repo / '.cold/verify-trace.jsonl').read_text().splitlines()]
        return result, trace

    def executions(self):
        return len(self.counter.read_text()) if self.counter.exists() else 0

    def entries(self):
        return sorted((self.repo / '.cold/verify-cache').glob('*.json'))

    def test_unchanged_inputs_hit_and_changed_input_invalidates(self):
        first, trace = self.verify()
        self.assertEqual((first.returncode, self.executions(), trace[0]['status']), (0, 1, 'pass'))
        second, hit = self.verify()
        self.assertEqual((second.returncode, self.executions(), hit[0]['status']), (2, 1, 'skip'))
        self.assertIn('TESTS INCOMPLETE: not run fresh (skipped or cached): check', second.stdout)
        self.assertEqual(hit[0]['inputs_hash'], trace[0]['inputs_hash'])
        (self.repo / 'input.txt').write_text('input two\n')
        third, miss = self.verify()
        self.assertEqual((third.returncode, self.executions(), miss[0]['status']), (0, 2, 'pass'))
        self.assertNotEqual(miss[0]['inputs_hash'], trace[0]['inputs_hash'])

    def test_recipe_change_invalidates(self):
        self.verify()
        (self.repo / '.fleet/verify.sh').write_text('recipe two\n')
        self.verify()
        self.assertEqual(self.executions(), 2)

    def test_no_cache_runs_the_step_and_a_later_run_still_hits(self):
        self.verify()
        _, trace = self.verify('--no-cache')
        self.assertEqual((self.executions(), trace[0]['status']), (2, 'pass'))
        self.verify()
        self.assertEqual(self.executions(), 2)

    def test_gate_reuses_only_the_exact_same_tree(self):
        self.verify()
        self.verify('--gate')
        self.assertEqual(self.executions(), 1)
        (self.repo / 'unrelated.txt').write_text('new tree\n')
        self.commit('unrelated change')
        self.verify()
        self.assertEqual(self.executions(), 1)
        _, trace = self.verify('--gate')
        self.assertEqual((self.executions(), trace[0]['status']), (2, 'pass'))
        self.verify('--gate')
        self.assertEqual(self.executions(), 2)

    def test_gate_runs_fully_on_a_dirty_tree(self):
        self.verify()
        (self.repo / 'scratch.txt').write_text('uncommitted\n')
        self.verify('--gate')
        self.assertEqual(self.executions(), 2)
        self.verify()
        self.assertEqual(self.executions(), 2)

    def test_gate_rechecks_the_tree_when_the_recipe_dirties_it_first(self):
        (self.repo / 'unrelated.txt').write_text('tracked\n')
        self.commit('track a file')
        self.verify()
        self.verify('--gate')
        self.assertEqual(self.executions(), 1)
        self.verify('--gate', prepare='open("unrelated.txt", "a").write("edit\\n")')
        self.assertEqual(self.executions(), 2)

    def test_step_that_dirties_the_tree_is_not_bound_to_it(self):
        (self.repo / 'unrelated.txt').write_text('tracked\n')
        self.commit('track a file')
        self.verify(command='open("unrelated.txt", "a").write("edit\\n")')
        self.assertIsNone(json.loads(self.entries()[0].read_text())['tree'])

    def test_step_that_changes_its_own_input_is_not_recorded(self):
        result, _ = self.verify(command='open("input.txt", "a").write("changed\\n")')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.entries(), [])
        self.verify()
        self.assertEqual(self.executions(), 2)

    def test_failed_step_is_never_cached(self):
        self.verify(command='raise SystemExit(3)')
        result, trace = self.verify(command='raise SystemExit(3)')
        self.assertEqual((result.returncode, self.executions(), trace[0]['status']), (3, 2, 'fail'))
        self.assertEqual(self.entries(), [])

    def test_corrupt_entries_run_the_step(self):
        self.verify()
        entry = self.entries()[0]
        good = entry.read_text()
        for broken in ('', 'not json', '[]', '{}', json.dumps({**json.loads(good), 'key': 'other'}),
                       json.dumps({**json.loads(good), 'head': ''}), json.dumps({**json.loads(good), 'schema_version': 2})):
            entry.write_text(broken)
            before = self.executions()
            self.verify()
            self.assertEqual(self.executions(), before + 1, broken)

    def test_missing_or_escaping_input_runs_the_step_without_caching(self):
        for inputs in (('absent.txt',), ('../escape',), (str(self.repo / 'input.txt'),)):
            self.verify(inputs=inputs)
            self.verify(inputs=inputs)
        self.assertEqual(self.executions(), 6)
        self.assertEqual(self.entries(), [])

    def test_repository_without_a_commit_is_never_cached(self):
        self.git('update-ref', '-d', 'HEAD')
        self.verify()
        self.verify()
        self.assertEqual(self.executions(), 2)

    def test_outside_cold_verify_the_step_just_runs(self):
        for _ in range(2):
            subprocess.run([sys.executable, str(ROOT / 'ci/verify_cache.py'), 'step', 'check', '--input', 'input.txt', '--',
                            sys.executable, '-c', f'open({str(self.counter)!r}, "a").write("x")'],
                           cwd=self.repo, check=True, capture_output=True,
                           env={name: value for name, value in os.environ.items()
                                if not name.startswith('COLD_VERIFY_')})
        self.assertEqual(self.executions(), 2)

    def test_directory_input_covers_every_file_in_it(self):
        (self.repo / 'data').mkdir()
        (self.repo / 'data/a.txt').write_text('a\n')
        self.verify(inputs=('data',))
        (self.repo / 'data/b.txt').write_text('b\n')
        self.verify(inputs=('data',))
        self.assertEqual(self.executions(), 2)

    def test_symlink_inside_a_directory_input_runs_the_step_without_caching(self):
        (self.repo / 'data').mkdir()
        (self.repo / 'actual').mkdir()
        (self.repo / 'actual/value').write_text('pass\n')
        (self.repo / 'data/link').symlink_to('../actual')
        (self.repo / 'data/file-link').symlink_to('../input.txt')
        self.verify(inputs=('data',))
        self.verify(inputs=('data',))
        self.assertEqual(self.executions(), 2)
        self.assertEqual(self.entries(), [])

    def test_key_has_no_path_or_content_ambiguity(self):
        keys = {
            verify_cache.inputs_hash({}, 'step', {'a': b'bc'}),
            verify_cache.inputs_hash({}, 'step', {'ab': b'c'}),
            verify_cache.inputs_hash({}, 'step', {'a': b'b', 'c': b''}),
            verify_cache.inputs_hash({}, 'step', {'a\0b': b'c'}),
            verify_cache.inputs_hash({}, 'step', {'a': b'\0b\0c'}),
            verify_cache.inputs_hash({'a': b'bc'}, 'step', {}),
            verify_cache.inputs_hash({}, 'ste', {'p': b''}),
            verify_cache.inputs_hash({}, 'other', {'a': b'bc'}),
        }
        self.assertEqual(len(keys), 8)
        self.assertEqual(verify_cache.inputs_hash({'r': b'1'}, 's', {'a': b'x', 'b': b'y'}),
                         verify_cache.inputs_hash({'r': b'1'}, 's', {'b': b'y', 'a': b'x'}))


if __name__ == '__main__':
    unittest.main()
