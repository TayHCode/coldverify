import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ci'))
import windows_lane

ENV = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}


def git(folder, *arguments):
    subprocess.run(['git', '-C', str(folder), '-c', 'user.name=t', '-c', 'user.email=t@t', *arguments], check=True, capture_output=True, env=ENV)


def repository(folder):
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(folder)], check=True, env=ENV)
    (Path(folder) / 'app.py').write_text('print("hi")\n')
    git(folder, 'add', '.')
    git(folder, 'commit', '-qm', 'base')
    git(folder, 'checkout', '-qb', 'change')


def commit(folder, name, text):
    (Path(folder) / name).parent.mkdir(parents=True, exist_ok=True)
    (Path(folder) / name).write_text(text)
    git(folder, 'add', '.')
    git(folder, 'commit', '-qm', name)


class WindowsLaneGate(unittest.TestCase):
    def reason(self, name=None, text=''):
        with tempfile.TemporaryDirectory() as folder:
            repository(folder)
            if name:
                commit(folder, name, text)
            return windows_lane.windows_reason(folder)

    def test_default_branch_push_uses_before_even_when_main_is_the_candidate(self):
        for remove in (False, True):
            with self.subTest(remove=remove), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                repository(root)
                git(root, 'checkout', '-q', 'main')
                commit(root, 'paths.py', 'import os\nIS = os.name == "nt"\n' if remove else 'IS = False\n')
                before = windows_lane.git(root, 'rev-parse', 'HEAD').stdout.decode().strip()
                commit(root, 'paths.py', 'IS = False\n' if remove else 'import os\nIS = os.name == "nt"\n')
                after = windows_lane.git(root, 'rev-parse', 'HEAD').stdout.decode().strip()
                event = root / 'push.json'
                event.write_text(json.dumps({'before': before, 'after': after}))
                with patch.dict(os.environ, GITHUB_EVENT_NAME='push', GITHUB_EVENT_PATH=str(event), GITHUB_WORKSPACE=str(root)):
                    self.assertIn('paths.py', windows_lane.windows_reason(root) or '')

    def test_push_with_unavailable_before_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repository(root)
            git(root, 'checkout', '-q', 'main')
            after = windows_lane.git(root, 'rev-parse', 'HEAD').stdout.decode().strip()
            event = root / 'push.json'
            event.write_text(json.dumps({'before': 'a' * 40, 'after': after}))
            with patch.dict(os.environ, GITHUB_EVENT_NAME='push', GITHUB_EVENT_PATH=str(event), GITHUB_WORKSPACE=str(root)):
                self.assertIsNotNone(windows_lane.windows_reason(root))

    def test_pull_request_uses_its_actual_base_for_removed_windows_code(self):
        for remove in (False, True):
            with self.subTest(remove=remove), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                subprocess.run(['git', 'init', '-q', '-b', 'trunk', folder], check=True, env=ENV)
                commit(root, 'paths.py', 'import os\nIS = os.name == "nt"\n' if remove else 'IS = False\n')
                base = windows_lane.git(root, 'rev-parse', 'HEAD').stdout.decode().strip()
                git(root, 'checkout', '-qb', 'change')
                commit(root, 'paths.py', 'IS = False\n' if remove else 'VALUE = 1\n')
                event = root / 'event.json'
                event.write_text(json.dumps({'pull_request': {'base': {'sha': base}}}))
                with patch.dict(os.environ, GITHUB_EVENT_NAME='pull_request', GITHUB_EVENT_PATH=str(event), GITHUB_WORKSPACE=str(root)):
                    reason = windows_lane.windows_reason(root)
                if remove:
                    self.assertIn('paths.py', reason or '')
                else:
                    self.assertIsNone(reason)

    def test_pull_request_with_missing_or_unavailable_base_fails_closed(self):
        for payload in ({}, {'pull_request': {'base': {'sha': 'a' * 40}}}):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                subprocess.run(['git', 'init', '-q', '-b', 'trunk', folder], check=True, env=ENV)
                commit(root, 'app.py', 'VALUE = 1\n')
                event = root / 'event.json'
                event.write_text(json.dumps(payload))
                with patch.dict(os.environ, GITHUB_EVENT_NAME='pull_request', GITHUB_EVENT_PATH=str(event), GITHUB_WORKSPACE=str(root)):
                    self.assertIn('cannot inspect', windows_lane.windows_reason(root) or '')

    def test_unanchored_actions_events_fail_closed(self):
        for event_name in ('workflow_dispatch', 'schedule', 'pull_request_target', 'merge_group'):
            with self.subTest(event=event_name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                repository(root)
                git(root, 'checkout', '-q', 'main')
                commit(root, 'paths.py', 'import os\nIS = os.name == "nt"\n')
                event = root / 'event.json'
                event.write_text('{}')
                with patch.dict(os.environ, GITHUB_EVENT_NAME=event_name, GITHUB_EVENT_PATH=str(event), GITHUB_WORKSPACE=str(root)):
                    self.assertIn('cannot inspect', windows_lane.windows_reason(root) or '')

    def test_manual_runs_scan_present_and_removed_windows_code_and_allow_portable_sources(self):
        for topic in (False, True):
            for change in ('portable', 'remove', 'present'):
                with self.subTest(topic=topic, change=change), tempfile.TemporaryDirectory() as folder:
                    root = Path(folder)
                    subprocess.run(['git', 'init', '-q', '-b', 'trunk', folder], check=True, env=ENV)
                    commit(root, 'paths.py', 'import os\nIS = os.name == "nt"\n' if change == 'remove' else 'IS = False\n')
                    git(root, 'update-ref', 'refs/remotes/origin/trunk', 'HEAD')
                    if topic:
                        git(root, 'checkout', '-qb', 'change')
                    commit(root, 'paths.py', 'import os\nIS = os.name == "nt"\n' if change == 'present' else 'IS = False\nVALUE = 1\n')
                    if topic:
                        commit(root, 'README.md', 'later portable commit\n')
                    else:
                        git(root, 'update-ref', 'refs/remotes/origin/trunk', 'HEAD')
                    event = root / 'event.json'
                    event.write_text(json.dumps({'repository': {'default_branch': 'trunk'}}))
                    with patch.dict(os.environ, GITHUB_EVENT_NAME='workflow_dispatch', GITHUB_EVENT_PATH=str(event), GITHUB_WORKSPACE=str(root)):
                        reason = windows_lane.windows_reason(root)
                    if change == 'portable':
                        self.assertIsNone(reason)
                    else:
                        self.assertIn('paths.py', reason or '')

    def test_portable_change_skips_the_windows_lane(self):
        self.assertIsNone(self.reason('lib/util.py', 'def add(a, b):\n    return a + b\n'))

    def test_windows_suffix_file_requires_the_lane(self):
        self.assertIn('build.ps1', self.reason('build.ps1', 'Write-Output hi\n'))

    def test_windows_specific_code_requires_the_lane(self):
        self.assertIn('winpath.py', self.reason('winpath.py', 'import sys\nIS = sys.platform == "win32"\n'))

    def test_json_data_mentioning_powershell_does_not_require_the_lane(self):
        self.assertIsNone(self.reason('notes.json', '{"shell": "powershell"}\n'))

    def test_repository_shipping_a_windows_recipe_always_runs_the_lane(self):
        with tempfile.TemporaryDirectory() as folder:
            repository(folder)
            git(folder, 'checkout', '-q', 'main')
            commit(folder, '.fleet/verify.ps1', 'exit 0\n')
            git(folder, 'checkout', '-qb', 'docs')
            commit(folder, 'README.md', 'docs\n')
            self.assertIn('.fleet/verify.ps1', windows_lane.windows_reason(folder) or '')

    def test_untouched_windows_file_on_the_base_does_not_require_the_lane(self):
        with tempfile.TemporaryDirectory() as folder:
            subprocess.run(['git', 'init', '-q', '-b', 'main', folder], check=True, env=ENV)
            commit(folder, 'old.bat', 'echo hi\n')
            git(folder, 'checkout', '-qb', 'change')
            commit(folder, 'lib.py', 'x = 1\n')
            self.assertIsNone(windows_lane.windows_reason(folder))

    def test_deleting_a_windows_file_requires_the_lane(self):
        with tempfile.TemporaryDirectory() as folder:
            subprocess.run(['git', 'init', '-q', '-b', 'main', folder], check=True, env=ENV)
            commit(folder, 'old.bat', 'echo hi\n')
            git(folder, 'checkout', '-qb', 'change')
            git(folder, 'rm', '-q', 'old.bat')
            git(folder, 'commit', '-qm', 'drop')
            self.assertIn('old.bat', windows_lane.windows_reason(folder))

    def test_removing_windows_specific_code_requires_the_lane(self):
        with tempfile.TemporaryDirectory() as folder:
            subprocess.run(['git', 'init', '-q', '-b', 'main', folder], check=True, env=ENV)
            commit(folder, 'paths.py', 'import sys\nIS = sys.platform == "win32"\n')
            git(folder, 'checkout', '-qb', 'change')
            commit(folder, 'paths.py', 'IS = False\n')
            self.assertIn('paths.py', windows_lane.windows_reason(folder))

    def test_removing_windows_code_from_quoted_filename_requires_the_lane(self):
        with tempfile.TemporaryDirectory() as folder:
            subprocess.run(['git', 'init', '-q', '-b', 'main', folder], check=True, env=ENV)
            commit(folder, 'path ü.py', 'import sys\nIS = sys.platform == "win32"\n')
            git(folder, 'checkout', '-qb', 'change')
            commit(folder, 'path ü.py', 'IS = False\n')
            self.assertIn('path ü.py', windows_lane.windows_reason(folder) or '')

    def test_unlistable_directory_runs_the_lane(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIn('Windows', windows_lane.windows_reason(folder))


class GenericLinuxVerify(unittest.TestCase):
    def run_generic(self, files):
        with tempfile.TemporaryDirectory() as folder:
            for name, text in files.items():
                (Path(folder) / name).parent.mkdir(parents=True, exist_ok=True)
                (Path(folder) / name).write_text(text)
            return subprocess.run([sys.executable, str(ROOT / 'ci/generic_verify.py')], cwd=folder, capture_output=True, text=True)

    def test_python_tests_pass(self):
        result = self.run_generic({'test_ok.py': 'import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertTrue(True)\n'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('python tests passed: 1', result.stdout)

    def test_failing_python_test_fails(self):
        result = self.run_generic({'test_bad.py': 'import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertTrue(False)\n'})
        self.assertNotEqual(result.returncode, 0)

    def test_root_conftest_selects_pytest_so_bare_test_functions_run(self):
        result = self.run_generic({
            'conftest.py': '',
            'test_ok.py': 'import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertTrue(True)\n',
            'test_bare.py': 'def test_function():\n    assert False\n'})
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_repository_with_no_tests_is_a_failure_not_a_pass(self):
        result = self.run_generic({'README.md': 'nothing\n'})
        self.assertEqual(result.returncode, 1)
        self.assertIn('no tests detected', result.stdout)

    def test_python_tests_that_all_skip_are_not_execution_evidence(self):
        result = self.run_generic({'test_skip.py': (
            'import unittest\nclass T(unittest.TestCase):\n'
            '    def test_skip(self): self.skipTest(\'not available (skipped=0)\')\n'
        )})
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('no tests executed', result.stdout)

    def test_python_mixed_pass_and_skip_has_execution_evidence(self):
        result = self.run_generic({'test_mixed.py': (
            'import unittest\nclass T(unittest.TestCase):\n'
            '    def test_subtests(self):\n'
            '        with self.subTest(\'skip\'):\n'
            '            self.skipTest(\'not available\')\n'
            '        with self.subTest(\'pass\'):\n'
            '            self.assertTrue(True)\n'
        )})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('python tests passed: 1', result.stdout)

    def test_python_pass_with_skipped_class_fixture_has_execution_evidence(self):
        result = self.run_generic({'test_fixtures.py': (
            'import unittest\n'
            'class Passing(unittest.TestCase):\n'
            '    def test_pass(self): self.assertTrue(True)\n'
            'class Skipped(unittest.TestCase):\n'
            '    @classmethod\n'
            '    def setUpClass(cls): raise unittest.SkipTest(\'class unavailable\')\n'
            '    def test_skip(self): pass\n'
        )})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('python tests passed: 1', result.stdout)
        self.assertIn("skipped 'class unavailable'", result.stdout)

    def test_python_pass_with_skipped_module_fixture_has_execution_evidence(self):
        result = self.run_generic({
            'test_pass.py': (
                'import unittest\nclass T(unittest.TestCase):\n'
                '    def test_pass(self): self.assertTrue(True)\n'
            ),
            'test_skip_module.py': (
                'import unittest\n'
                'def setUpModule(): raise unittest.SkipTest(\'module unavailable\')\n'
                'class T(unittest.TestCase):\n'
                '    def test_skip(self): pass\n'
            ),
        })
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('python tests passed: 1', result.stdout)
        self.assertIn("skipped 'module unavailable'", result.stdout)

    def test_python_only_skipped_class_fixture_is_not_execution_evidence(self):
        result = self.run_generic({'test_skip.py': (
            'import unittest\n'
            'class Skipped(unittest.TestCase):\n'
            '    @classmethod\n'
            '    def setUpClass(cls): raise unittest.SkipTest(\'class unavailable\')\n'
            '    def test_skip(self): pass\n'
        )})
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('no tests executed', result.stdout)
        self.assertIn("skipped 'class unavailable'", result.stdout)

    def test_python_only_skipped_module_fixture_is_not_execution_evidence(self):
        result = self.run_generic({'test_skip_module.py': (
            'import unittest\n'
            'def setUpModule(): raise unittest.SkipTest(\'module unavailable\')\n'
            'class T(unittest.TestCase):\n'
            '    def test_skip(self): pass\n'
        )})
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('no tests executed', result.stdout)
        self.assertIn("skipped 'module unavailable'", result.stdout)

    def test_all_skipped_subtests_keep_method_execution_and_skip_reporting(self):
        result = self.run_generic({'test_subtests.py': (
            'import unittest\nclass T(unittest.TestCase):\n'
            '    def test_subtests(self):\n'
            '        with self.subTest(\'first\'): self.skipTest(\'first skip\')\n'
            '        with self.subTest(\'second\'): self.skipTest(\'second skip\')\n'
        )})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('python tests passed: 1', result.stdout)
        self.assertIn("skipped 'first skip'", result.stdout)
        self.assertIn("skipped 'second skip'", result.stdout)


if __name__ == '__main__':
    unittest.main()
