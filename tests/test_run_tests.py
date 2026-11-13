import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/run_tests.py'


def load_runner():
    spec = importlib.util.spec_from_file_location('run_tests_under_test', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RunTests(unittest.TestCase):
    def test_a_module_that_skips_at_import_prints_its_reason(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as scratch:
            tests = Path(scratch)
            (tests / 'test_rt186_skipped.py').write_text("import unittest\nraise unittest.SkipTest('rt186 widget missing \\u00e9')\n")
            (tests / 'test_rt186_ok.py').write_text(
                'import unittest\n\n\nclass Ok(unittest.TestCase):\n    def test_ok(self):\n        pass\n')
            out = io.StringIO()
            with mock.patch.object(runner, 'TESTS', tests), mock.patch('sys.argv', ['run_tests.py', '--jobs', '1']), \
                    mock.patch.object(sys, 'path', list(sys.path)), mock.patch.dict(sys.modules), \
                    contextlib.redirect_stdout(out):
                code = runner.main()
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn('Skipped 1: rt186 widget missing \\xe9\n', out.getvalue())
        self.assertIn('Ran 2 tests', out.getvalue())

    def test_running_twice_in_one_process_gives_the_same_result(self):
        self.test_a_module_that_skips_at_import_prints_its_reason()
        self.test_a_module_that_skips_at_import_prints_its_reason()


if __name__ == '__main__':
    unittest.main()
