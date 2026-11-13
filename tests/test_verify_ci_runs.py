import contextlib
import io
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SIGN = (f'import os, sys; sys.path.insert(0, {str(ROOT / "ci")!r}); import junit_unittest; '
        'junit_unittest.sign(sys.argv[1], os.environ["COLD_VERIFY_JUNIT_KEY"])')
sys.path.insert(0, str(ROOT / 'ci'))
import verify_context
import verify_trace


class CiRuns(unittest.TestCase):
    def setUp(self):
        host_marker = patch.object(verify_context, 'COLDBOX_HOST_FILE', Path('/nonexistent/coldbox/host'))
        host_marker.start()
        self.addCleanup(host_marker.stop)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / 'repo'
        self.home = Path(folder.name) / 'home'
        self.summary = Path(folder.name) / 'summary.md'
        self.root.mkdir()
        self.home.mkdir()
        git = ['git', '-C', str(self.root), '-c', 'user.name=t', '-c', 'user.email=t@localhost']
        subprocess.run([*git, 'init', '-q'], check=True)
        (self.root / 'file.txt').write_text('one\n')
        subprocess.run([*git, 'add', 'file.txt'], check=True)
        subprocess.run([*git, 'commit', '-qm', 'one'], check=True)
        self.env = {**os.environ, 'HOME': str(self.home), 'GITHUB_ACTIONS': 'true',
                    'GITHUB_REPOSITORY': 'Owner/repo', 'GITHUB_SERVER_URL': 'https://github.example',
                    'GITHUB_RUN_ID': '42', 'GITHUB_STEP_SUMMARY': str(self.summary)}

    def run_ci(self, script, ci=True, no_cache=False, attempt='1'):
        output = io.StringIO()
        env = {**self.env, 'GITHUB_RUN_ATTEMPT': attempt}
        with patch.dict(os.environ, {'HOME': str(self.home)}), contextlib.redirect_stdout(output):
            code = verify_trace.run(['bash', '-c', script], self.root, env=env, ci=ci, no_cache=no_cache)
        return code, output.getvalue()

    def test_identical_tree_after_a_full_pass_is_verified_again(self):
        self.assertEqual(self.run_ci('echo "Ran 1 test in 0.001s"; '
                                     'echo "<testsuite><testcase name=\'t\'/></testsuite>" > "$COLD_VERIFY_JUNIT_DIR/t.xml"; '
                                     f'{sys.executable} -c {shlex.quote(SIGN)} "$COLD_VERIFY_JUNIT_DIR/t.xml"')[0], 0)
        code, output = self.run_ci('echo second-run; exit 1')
        self.assertEqual(code, 1)
        self.assertIn('second-run', output)
        self.assertIn('TESTS FAIL', output)

    def test_summary_lists_each_step_and_the_failing_excerpt(self):
        self.run_ci('echo "::group::lint"; echo "::endgroup::"; echo "::group::tests"; echo boom; echo "FAILED (1): tests"; exit 1')
        summary = self.summary.read_text()
        self.assertIn('| lint | pass |', summary)
        self.assertIn('| tests | fail |', summary)
        self.assertIn('boom', summary)

    def test_non_ci_runs_leave_the_summary_alone(self):
        self.run_ci('echo hi', ci=False)
        self.assertFalse(self.summary.exists())


if __name__ == '__main__':
    unittest.main()
