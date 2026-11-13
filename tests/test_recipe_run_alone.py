import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PASSES = 'import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n'
FAILS = 'import unittest\n\nclass T(unittest.TestCase):\n    def test_bad(self):\n        self.fail("no")\n'
ALONE_PASSES = ('import unittest\nfrom run_alone import run_alone\n\nclass T(unittest.TestCase):\n'
                '    @run_alone\n    def test_ok(self):\n        pass\n')
ALONE_FAILS = ALONE_PASSES.replace('pass', 'self.fail("no")')


class RecipeRunsMarkedFilesAlone(unittest.TestCase):
    def recipe(self, tests, pool=None, alone=None):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            for name in ('.fleet/verify.sh', 'ci/verify_filter.py', 'ci/codex_sandbox.py', 'ci/verify_parallel.py', 'ci/verify_cache.py',
                         'ci/junit_unittest.py', 'tests/run_alone.py'):
                (repo / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(ROOT / name, repo / name)
            for name, text in tests.items():
                (repo / 'tests' / name).write_text(text)
            subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
            env = {key: value for key, value in os.environ.items() if not key.startswith(('COLD_VERIFY', 'GIT_'))}
            env['COLD_VERIFY_GATE'] = '1'
            if pool is not None:
                stub = repo / 'ci/stub_filter.py'
                stub.write_text('import os, sys\nprint("stub", os.environ.get("COLD_VERIFY_JOBS"), sys.argv[1:])\n'
                                'sys.exit(int(os.environ["ALONE" if os.environ.get("COLD_VERIFY_JOBS") == "1" else "POOL"]))\n')
                env.update(COLD_VERIFY_FILTER=str(stub), POOL=str(pool), ALONE=str(alone))
            done = subprocess.run(['bash', '.fleet/verify.sh'], cwd=repo, text=True, capture_output=True, env=env)
            return done.returncode, done.stdout + done.stderr

    def test_the_two_halves_combine_to_a_pass_only_when_tests_ran_and_none_failed(self):
        files = {'test_pool.py': PASSES, 'test_alone.py': ALONE_PASSES}
        for pool, alone, expected in ((2, 2, 2), (1, 2, 1), (2, 1, 1), (1, 0, 1), (0, 1, 1), (3, 0, 3), (0, 3, 3),
                                      (0, 2, 0), (2, 0, 0), (0, 0, 0)):
            with self.subTest(pool=pool, alone=alone):
                code, output = self.recipe(files, pool, alone)
                self.assertEqual(code, expected, output)
                self.assertIn("stub None ['run', '--suite', 'tests/test_pool.py'", output)
                self.assertIn("stub 1 ['run', '--suite', 'tests/test_alone.py'", output)

    def test_a_file_name_with_a_space_stays_one_suite(self):
        code, output = self.recipe({'test a b.py': PASSES, 'test_alone.py': ALONE_PASSES}, 0, 0)
        self.assertEqual(code, 0, output)
        self.assertIn("stub None ['run', '--suite', 'tests/test a b.py'", output)

    def test_a_file_name_that_reads_as_a_glob_fails_instead_of_matching_other_files(self):
        code, output = self.recipe({'testx.py': PASSES, 'test[x].py': FAILS})
        self.assertEqual(code, 1, output)
        self.assertIn('INCOMPLETE: tests/test[x].py has a glob character in its name', output)

    def test_both_halves_passing_pass(self):
        code, output = self.recipe({'test_pool.py': PASSES, 'test_alone.py': ALONE_PASSES})
        self.assertEqual(code, 0, output)
        self.assertIn('tests/test_pool.py (1 of 1)', output)
        self.assertIn('tests/test_alone.py (1 of 1)', output)

    def test_no_test_file_in_either_half_is_nothing_ran(self):
        code, output = self.recipe({})
        self.assertEqual(code, 2, output)

    def test_a_failing_pool_file_with_no_marked_file_fails(self):
        code, output = self.recipe({'test_pool.py': FAILS})
        self.assertEqual(code, 1, output)

    def test_a_failing_marked_file_with_no_pool_file_fails(self):
        code, output = self.recipe({'test_alone.py': ALONE_FAILS})
        self.assertEqual(code, 1, output)

    def test_a_marked_file_that_crashes_at_import_fails(self):
        code, output = self.recipe({'test_pool.py': PASSES, 'test_alone.py': ALONE_PASSES + 'raise ImportError("boom")\n'})
        self.assertEqual(code, 1, output)
        self.assertIn('boom', output)


if __name__ == '__main__':
    unittest.main()
