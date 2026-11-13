import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / 'ci/junit_unittest.py'
sys.path.insert(0, str(ROOT / 'ci'))
import verify_trace

KEY = 'per-run-key'

MIXED = '''import os, unittest
import helper

class Mixed(unittest.TestCase):
    def test_pass(self):
        self.assertEqual(helper.VALUE, 1)

    def test_fail(self):
        self.assertEqual(1, 2)

    def test_error(self):
        raise RuntimeError('boom')

    @unittest.skip('not here')
    def test_skip(self):
        pass

    def test_subtest_fail(self):
        for value in (1, 2):
            with self.subTest(value=value):
                self.assertEqual(value, 1)

    @unittest.expectedFailure
    def test_expected_failure(self):
        self.assertEqual(1, 2)

    @unittest.expectedFailure
    def test_unexpected_success(self):
        pass

    def test_children_cannot_write_into_the_report(self):
        self.assertNotIn('COLD_VERIFY_JUNIT_DIR', os.environ)
        self.assertNotIn('COLD_VERIFY_JUNIT_KEY', os.environ)
'''
SUBTESTS = '''import unittest

class Subtests(unittest.TestCase):
    def test_all_skipped(self):
        with self.subTest(case=1):
            self.skipTest('dependency unavailable')

    def test_passed_and_skipped(self):
        with self.subTest(case=1):
            pass
        with self.subTest(case=2):
            self.skipTest('dependency unavailable')

    def test_skipped_and_failed(self):
        with self.subTest(case=1):
            self.skipTest('dependency unavailable')
        with self.subTest(case=2):
            self.assertEqual(1, 2)

class SkippedClass(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raise unittest.SkipTest('no fixture')

    def test_a(self):
        pass
'''
PASSING = 'import unittest\nclass P(unittest.TestCase):\n    def test_a(self):\n        pass\n    def test_b(self):\n        pass\n'


def run(folder, *args, junit=None):
    env = {key: value for key, value in os.environ.items() if key not in ('COLD_VERIFY_JUNIT_DIR', 'COLD_VERIFY_JUNIT_KEY')}
    if junit:
        env.update(COLD_VERIFY_JUNIT_DIR=str(junit), COLD_VERIFY_JUNIT_KEY=KEY)
    return subprocess.run([sys.executable, str(RUNNER), *args], cwd=folder, env=env, capture_output=True, text=True, timeout=120)


class JUnitUnittest(unittest.TestCase):
    def suite(self, folder, source):
        (Path(folder) / 'helper.py').write_text('VALUE = 1\n')
        (Path(folder) / 'test_mixed.py').write_text(source)

    def test_report_records_each_outcome_and_the_exit_code_matches_unittest(self):
        with tempfile.TemporaryDirectory() as folder:
            self.suite(folder, MIXED)
            junit = Path(folder) / 'junit'
            result = run(folder, 'discover', '-p', 'test_*.py', junit=junit)
            plain = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-p', 'test_*.py'], cwd=folder,
                                   capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, plain.returncode)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn('Ran 8 tests', result.stderr)
            [report] = junit.glob('*.xml')
            cases = {case.get('name'): [child.tag for child in case] for case in ET.parse(report).getroot().iter('testcase')}
            totals = verify_trace.junit_totals(junit, KEY)
        self.assertEqual(cases, {'test_pass': [], 'test_fail': ['failure'], 'test_error': ['error'], 'test_skip': ['skipped'],
                                 'test_subtest_fail': ['failure'], 'test_expected_failure': [],
                                 'test_unexpected_success': ['failure'], 'test_children_cannot_write_into_the_report': []})
        self.assertEqual(totals, {'tests': 8, 'failures': 3, 'errors': 1, 'skipped': 1, 'unreadable': [], 'unsigned': []})

    def test_a_method_whose_subtests_all_skipped_is_skipped_but_a_mixed_one_passes(self):
        with tempfile.TemporaryDirectory() as folder:
            self.suite(folder, SUBTESTS)
            junit = Path(folder) / 'junit'
            result = run(folder, 'discover', '-p', 'test_*.py', junit=junit)
            self.assertEqual(result.returncode, 1, result.stderr)
            [report] = junit.glob('*.xml')
            cases = {case.get('name'): [child.tag for child in case] for case in ET.parse(report).getroot().iter('testcase')}
            totals = verify_trace.junit_totals(junit, KEY)
        self.assertEqual(cases, {'test_all_skipped': ['skipped'], 'test_passed_and_skipped': [], 'test_skipped_and_failed': ['failure']})
        self.assertEqual(totals, {'tests': 3, 'failures': 1, 'errors': 0, 'skipped': 1, 'unreadable': [], 'unsigned': []})

    def test_a_passing_suite_exits_0_and_names_its_module(self):
        with tempfile.TemporaryDirectory() as folder:
            self.suite(folder, PASSING)
            junit = Path(folder) / 'junit'
            result = run(folder, 'test_mixed', junit=junit)
            self.assertEqual(result.returncode, 0, result.stderr)
            [report] = junit.glob('*.xml')
            classes = {case.get('classname') for case in ET.parse(report).getroot().iter('testcase')}
            self.assertEqual(verify_trace.junit_totals(junit, KEY)['tests'], 2)
        self.assertEqual(classes, {'test_mixed.P'})

    def test_a_setup_class_error_is_an_error_in_the_report(self):
        source = ('import unittest\nclass Broken(unittest.TestCase):\n    @classmethod\n    def setUpClass(cls):\n'
                  '        raise RuntimeError("no fixture")\n    def test_a(self):\n        pass\n')
        with tempfile.TemporaryDirectory() as folder:
            self.suite(folder, source)
            junit = Path(folder) / 'junit'
            result = run(folder, 'discover', junit=junit)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(verify_trace.junit_totals(junit, KEY)['errors'], 1)

    def test_empty_suite_exits_5(self):
        with tempfile.TemporaryDirectory() as folder:
            result = run(folder, 'discover')
            self.assertEqual(result.returncode, 5, result.stderr)

    def test_skipped_class_exits_0(self):
        source = ('import unittest\nclass Skipped(unittest.TestCase):\n    @classmethod\n    def setUpClass(cls):\n'
                  '        raise unittest.SkipTest("no fixture")\n    def test_a(self):\n        pass\n')
        with tempfile.TemporaryDirectory() as folder:
            self.suite(folder, source)
            result = run(folder, 'discover')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('skipped=1', result.stderr)

    def test_without_the_variable_it_writes_nothing_and_behaves_like_unittest(self):
        with tempfile.TemporaryDirectory() as folder:
            self.suite(folder, PASSING)
            result = run(folder, 'discover')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Ran 2 tests', result.stderr)
            self.assertEqual([path.name for path in Path(folder).rglob('*.xml')], [])


if __name__ == '__main__':
    unittest.main()
