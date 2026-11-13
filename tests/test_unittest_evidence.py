import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / 'ci/unittest_evidence.py'
sys.path.insert(0, str(ROOT / 'ci'))
import generic_verify
import verify_trace


class UnittestEvidenceTest(unittest.TestCase):
    def test_counts_a_test_in_a_tests_folder_without_init(self):
        with tempfile.TemporaryDirectory() as folder:
            tests = Path(folder) / 'tests'
            tests.mkdir()
            (tests / 'test_x.py').write_text(
                'import unittest\n\n\nclass X(unittest.TestCase):\n    def test_x(self):\n        pass\n')
            result = subprocess.run([sys.executable, str(RUNNER), '.', 'tests'], cwd=folder,
                                    capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('GENERIC_EXECUTED_TESTS=1', result.stdout.splitlines())

    def test_writes_junit_xml_when_coldverify_asks_for_it(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'test_x.py').write_text(
                'import unittest\n\n\nclass X(unittest.TestCase):\n    def test_x(self):\n        pass\n'
                '    @unittest.skip("no")\n    def test_y(self):\n        pass\n')
            junit = Path(folder) / 'junit'
            result = subprocess.run([sys.executable, str(RUNNER), '.'], cwd=folder, capture_output=True, text=True, timeout=60,
                                    env={**os.environ, 'COLD_VERIFY_JUNIT_DIR': str(junit), 'COLD_VERIFY_JUNIT_KEY': 'k'})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('GENERIC_EXECUTED_TESTS=1', result.stdout.splitlines())
            self.assertEqual(verify_trace.junit_totals(junit, 'k'), {'tests': 2, 'failures': 0, 'errors': 0, 'skipped': 1, 'unreadable': [], 'unsigned': []})

    def test_generic_detection_hides_the_key_from_project_installs(self):
        seen = []

        def record(arguments, capture=False, env=None):
            seen.append((arguments, dict(env or os.environ)))
            return subprocess.CompletedProcess([], 0, 'GENERIC_EXECUTED_TESTS=1\n')
        with tempfile.TemporaryDirectory() as folder, patch.object(generic_verify, 'run', side_effect=record), \
                patch.object(generic_verify.Path, 'cwd', return_value=Path(folder)), \
                patch.dict(os.environ, COLD_VERIFY_JUNIT_DIR='/junit', COLD_VERIFY_JUNIT_KEY='k'):
            (Path(folder) / 'setup.py').write_text('')
            self.assertEqual(generic_verify.main(), 0)
            self.assertNotIn('COLD_VERIFY_JUNIT_KEY', os.environ)
        installs = [env for arguments, env in seen if 'pip' in arguments or 'venv' in arguments]
        self.assertTrue(installs)
        self.assertFalse([env for env in installs if 'COLD_VERIFY_JUNIT_KEY' in env])
        [(_, env)] = [call for call in seen if str(call[0][1]).endswith('unittest_evidence.py')]
        self.assertEqual((env['COLD_VERIFY_JUNIT_DIR'], env['COLD_VERIFY_JUNIT_KEY']), ('/junit', 'k'))

    def test_generic_pytest_writes_junit_xml_when_coldverify_asks_for_it(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(generic_verify, 'require'), \
                patch.object(generic_verify, 'run') as run:
            junit = Path(folder) / 'junit'
            junit.mkdir()
            (Path(folder) / 'conftest.py').write_text('')

            def pytest(arguments, capture=False):
                (junit / 'pytest.xml').write_text('<testsuite><testcase name="t"/></testsuite>')
                return subprocess.CompletedProcess([], 0, '1 passed in 0.01s\n')
            run.side_effect = pytest
            self.assertEqual(generic_verify.python_tests(Path(folder), (str(junit), 'k')), (0, 1))
            self.assertEqual(run.call_args.args[0][-3:], ['pytest', '-q', f'--junit-xml={junit}/pytest.xml'])
            self.assertEqual(verify_trace.junit_totals(junit, 'k')['tests'], 1)


if __name__ == '__main__':
    unittest.main()
