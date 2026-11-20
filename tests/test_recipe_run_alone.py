import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ci'))
from verify_trace import junit_totals  # noqa: E402

PASSES = 'import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n'
FAILS = 'import unittest\n\nclass T(unittest.TestCase):\n    def test_bad(self):\n        self.fail("no")\n'
ALONE_PASSES = ('import unittest\nfrom run_alone import run_alone\n\nclass T(unittest.TestCase):\n'
                '    @run_alone\n    def test_ok(self):\n        pass\n')
ALONE_FAILS = ALONE_PASSES.replace('pass', 'self.fail("no")')
KEY = 'recipe-test-key'


class RecipeRunsTheParallelRunner(unittest.TestCase):
    def recipe(self, tests):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / 'repo'
            junit = Path(tmp) / 'junit'
            for name in ('.fleet/verify.sh', 'ci/verify_parallel.py', 'ci/junit_unittest.py', 'scripts/run_tests.py',
                         'tests/run_alone.py'):
                (repo / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(ROOT / name, repo / name)
            for name, text in tests.items():
                (repo / 'tests' / name).write_text(text)
            env = {key: value for key, value in os.environ.items() if not key.startswith(('COLD_VERIFY', 'GIT_'))}
            env.update(COLD_VERIFY_JUNIT_DIR=str(junit), COLD_VERIFY_JUNIT_KEY=KEY)
            done = subprocess.run(['bash', '.fleet/verify.sh'], cwd=repo, text=True, capture_output=True, env=env)
            return done.returncode, done.stdout + done.stderr, junit_totals(junit, KEY)

    def test_both_halves_passing_pass_with_signed_junit_for_every_test(self):
        code, output, totals = self.recipe({'test_pool.py': PASSES, 'test_alone.py': ALONE_PASSES})
        self.assertEqual(code, 0, output)
        self.assertIn('Ran 2 tests', output)
        self.assertEqual((totals['tests'], totals['failures'], totals['unsigned']), (2, 0, []), output)

    def test_no_test_file_is_nothing_ran(self):
        code, output, totals = self.recipe({})
        self.assertEqual(code, 2, output)
        self.assertIsNone(totals)

    def test_a_failing_pool_test_with_no_marked_test_fails(self):
        code, output, totals = self.recipe({'test_pool.py': FAILS})
        self.assertEqual(code, 1, output)
        self.assertEqual(totals['failures'], 1, output)

    def test_a_failing_marked_test_with_no_pool_test_fails(self):
        code, output, totals = self.recipe({'test_alone.py': ALONE_FAILS})
        self.assertEqual(code, 1, output)
        self.assertEqual(totals['failures'], 1, output)

    def test_a_marked_file_that_crashes_at_import_fails(self):
        code, output, _ = self.recipe({'test_pool.py': PASSES, 'test_alone.py': ALONE_PASSES + 'raise ImportError("boom")\n'})
        self.assertEqual(code, 1, output)
        self.assertIn('boom', output)


if __name__ == '__main__':
    unittest.main()
