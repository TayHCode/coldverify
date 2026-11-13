import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FLOW = ROOT / 'examples/flows/coldfix-recheck-unconfirmed'


class ColdfixRecheckFlowTests(unittest.TestCase):
    def test_flow_is_executable(self):
        self.assertTrue(os.access(FLOW, os.X_OK))

    def run_flow(self, checkout):
        with tempfile.TemporaryDirectory() as artifacts:
            run = subprocess.run([sys.executable, str(FLOW)], cwd=checkout, text=True, capture_output=True,
                                 env={'PATH': '/usr/bin:/bin', 'COLD_FLOW_ARTIFACTS': artifacts})
        self.assertEqual(run.returncode, 125, run.stdout + run.stderr)
        self.assertTrue(run.stdout.strip().splitlines()[-1].startswith('cannot provision: '), run.stdout)

    def test_outside_a_coldfix_checkout_it_reports_it_could_not_run_never_red(self):
        with tempfile.TemporaryDirectory() as folder:
            self.run_flow(folder)

    def test_a_recheck_that_crashes_after_the_fixture_is_built_is_red(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'coldfix.py').write_text(
                "import json, os, sys\n"
                "os.makedirs(sys.argv[2], exist_ok=True)\n"
                "replies = {'capture': {}, 'propose': {'groups': [{'group_id': 'g'}]}, 'lesson': {'lesson_id': 'l', 'id': 'r'}}\n"
                "if sys.argv[3] == 'recheck':\n"
                "    sys.exit('Traceback: recheck crashed')\n"
                "print(json.dumps(replies[sys.argv[3]]))\n")
            (Path(folder) / 'lesson_delivery.py').write_text('')
            with tempfile.TemporaryDirectory() as artifacts:
                run = subprocess.run([sys.executable, str(FLOW)], cwd=folder, text=True, capture_output=True,
                                     env={'PATH': '/usr/bin:/bin', 'COLD_FLOW_ARTIFACTS': artifacts})
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertIn('recheck crashed', run.stdout)

    def test_a_coldfix_whose_output_cannot_build_the_fixture_reports_it_could_not_run_never_red(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'coldfix.py').write_text("print('not json')\n")
            (Path(folder) / 'lesson_delivery.py').write_text('')
            self.run_flow(folder)


if __name__ == '__main__':
    unittest.main()
