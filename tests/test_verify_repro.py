import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ci'))
spec = importlib.util.spec_from_file_location('verify_repro', ROOT / 'ci/verify_repro.py')
repro = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repro)


class ChangedCases(unittest.TestCase):
    def test_other_language_test_entrypoints_are_not_treated_as_assets(self):
        self.assertTrue(repro.unsupported_test('tests/test_regression.ps1'))
        self.assertTrue(repro.unsupported_test('tests/RegressionTest.cs'))
        self.assertTrue(repro.unsupported_test('Example.Tests/RegressionTests.cs'))
        self.assertTrue(repro.unsupported_test('src/RegressionTest.java'))
        self.assertTrue(repro.unsupported_test('tests/integration.rs'))
        self.assertTrue(repro.unsupported_test('src/test/java/ExampleTest.java'))
        self.assertFalse(repro.unsupported_test('tests/fixtures/input.js'))
        self.assertFalse(repro.unsupported_test('tests/fixtures/example.rs'))

    def test_body_distinguishes_a_bug_fix_from_discussion(self):
        self.assertFalse(repro.is_bug_fix('Add a gate', 'feat/gate', 'Bug-fix tests can pass before a fix.'))
        self.assertTrue(repro.is_bug_fix('Update behavior', 'work/change', 'This PR fixes a bug.'))
        self.assertTrue(repro.is_bug_fix('Update behavior', 'work/change', 'type: bug'))

    def test_abstract_test_helpers_are_not_run_as_cases(self):
        cases = repro.methods('class SharedAssertions(ABC):\n def test_value(self):\n  assert False\nclass Check(SharedAssertions, unittest.TestCase):\n pass\n')
        self.assertEqual(list(cases), ['Check.test_value'])

    def test_inherited_test_is_selected_for_concrete_case(self):
        cases = repro.methods('class Mixin:\n def test_value(self):\n  assert False\nclass Check(Mixin, unittest.TestCase):\n pass\n')
        self.assertEqual(list(cases), ['Check.test_value'])

    def test_qualified_base_does_not_collide_with_local_class_name(self):
        cases = repro.methods('class TestCase(unittest.TestCase):\n def test_value(self):\n  assert True\n')
        self.assertEqual(list(cases), ['TestCase.test_value'])

    def test_multiple_inheritance_respects_method_resolution_order(self):
        source = 'class Root(unittest.TestCase):\n def test_value(self):\n  assert VALUE == 0\nclass Override(Root):\n def test_value(self):\n  assert True\nclass Plain(Root):\n pass\nclass Combined(Plain, Override):\n pass\n'
        before = repro.methods(source)
        after = repro.methods(source.replace('VALUE == 0', 'VALUE == 1'))
        self.assertNotEqual(before['Root.test_value'], after['Root.test_value'])
        self.assertEqual(before['Combined.test_value'], after['Combined.test_value'])

    def test_method_changes_are_detected_without_running_unchanged_cases(self):
        before = repro.methods('class TestThing(unittest.TestCase):\n def test_old(self):\n  assert True\n')
        after = repro.methods('class TestThing(unittest.TestCase):\n def test_old(self):\n  assert True\n def test_new(self):\n  assert False\n')
        self.assertEqual([name for name, node in after.items() if before.get(name) != node], ['TestThing.test_new'])


    def test_only_new_or_changed_pytest_functions_are_selected(self):
        before = repro.methods('def test_old():\n    assert True\ndef helper():\n    pass\n')
        after = repro.methods('def test_old():\n    assert True\ndef test_new():\n    assert False\ndef helper():\n    pass\n')
        self.assertEqual(sorted(after), ['test_new', 'test_old'])
        self.assertEqual([name for name, node in after.items() if before.get(name) != node], ['test_new'])


class GateBehavior(unittest.TestCase):
    def test_unexpected_success_is_not_a_passing_repro(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            test = Path(tmp) / 'test_expected.py'
            test.write_text('import unittest\nclass Check(unittest.TestCase):\n @unittest.expectedFailure\n def test_value(self):\n  pass\n')
            self.assertEqual(repro.run_case(test, 'Check.test_value'), 2)

    def test_hung_repro_is_an_error(self):
        import os
        import sys
        from unittest.mock import patch
        with patch.object(repro, 'CASE_TIMEOUT', 0.1):
            self.assertEqual(repro.run_test([sys.executable, '-c', 'import time; time.sleep(30)'], Path.cwd(), dict(os.environ)), 2)

    def test_import_time_exit_is_not_a_test_result(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            test = Path(tmp) / 'test_exit.py'
            for exit_code in (0, 1):
                test.write_text(f'raise SystemExit({exit_code})\n')
                self.assertEqual(repro.run_case(test, 'Check.test_value'), 2)

    def test_dataclass_and_package_relative_import_run(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp, patch.dict('sys.modules'):
            package = Path(tmp) / 'repro_package'
            package.mkdir()
            (package / '__init__.py').write_text('')
            (package / 'helper.py').write_text('VALUE = 1\n')
            test = package / 'test_value.py'
            test.write_text('from __future__ import annotations\nfrom dataclasses import dataclass\nfrom .helper import VALUE\nimport unittest\n@dataclass\nclass Item:\n value: int\nclass Check(unittest.TestCase):\n def test_value(self):\n  self.assertEqual(Item(VALUE).value, 1)\n')
            self.assertEqual(repro.run_case(test, 'Check.test_value'), 0)

    def test_detached_checkout_can_verify_without_pr_metadata(self):
        import os
        import subprocess
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            subprocess.run(['git', '-C', tmp, 'init', '-q'], check=True)
            subprocess.run(['git', '-C', tmp, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost', 'commit', '--allow-empty', '-qm', 'base'], check=True)
            subprocess.run(['git', '-C', tmp, 'checkout', '-q', '--detach'], check=True)
            self.assertEqual(repro.gate(Path(tmp)), 0)

    def test_changed_package_support_files_are_overlaid_on_base(self):
        import subprocess
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                return subprocess.check_output(['git', '-C', tmp, *args], stderr=subprocess.DEVNULL, text=True).strip()
            git('init', '-q', '-b', 'main')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@localhost')
            (root / '.gitignore').write_text('.cold/\n')
            (root / 'value.py').write_text('VALUE = 0\n')
            git('add', '.')
            git('commit', '-qm', 'base')
            base = git('rev-parse', 'HEAD')
            git('switch', '-qc', 'fix/fixture')
            (root / 'value.py').write_text('VALUE = 1\n')
            (root / 'tests').mkdir()
            (root / 'tests/__init__.py').write_text('')
            (root / 'tests/helpers').mkdir()
            (root / 'tests/helpers/__init__.py').write_text('')
            (root / 'tests/helpers/expected.py').write_text('import json\nfrom pathlib import Path\nEXPECTED = json.loads(Path(__file__).with_name("link.json").read_text())["value"]\n')
            (root / 'tests/helpers/expected.json').write_text('{"value": 1}')
            (root / 'tests/helpers/link.json').symlink_to('expected.json')
            (root / 'tests/helpers/input.js').write_text('fixture data')
            (root / 'tests/unit').mkdir()
            (root / 'tests/unit/__init__.py').write_text('')
            (root / 'tests/unit/test_value.py').write_text('import os\nimport unittest\nfrom value import VALUE\nfrom tests.helpers.expected import EXPECTED\nclass Check(unittest.TestCase):\n def test_value(self):\n  self.assertNotIn("COLD_TEST_SECRET", os.environ)\n  self.assertEqual(VALUE, EXPECTED)\n')
            git('add', '.')
            git('commit', '-qm', 'fix')
            head = git('rev-parse', 'HEAD')
            with patch.dict('os.environ', {'COLD_TEST_SECRET': 'fixture-secret'}), patch.object(repro, 'metadata', return_value=('fix fixture', '', base, head)):
                self.assertEqual(repro.gate(root), 0)

    def test_imported_test_base_runs_its_inherited_repro(self):
        import subprocess
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                return subprocess.check_output(['git', '-C', tmp, *args], stderr=subprocess.DEVNULL, text=True).strip()
            git('init', '-q', '-b', 'main')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@localhost')
            (root / '.gitignore').write_text('.cold/\n')
            (root / 'value.py').write_text('VALUE = 0\n')
            git('add', '.')
            git('commit', '-qm', 'base')
            base = git('rev-parse', 'HEAD')
            git('switch', '-qc', 'fix/fixture')
            (root / 'value.py').write_text('VALUE = 1\n')
            (root / 'tests').mkdir()
            (root / 'tests/__init__.py').write_text('')
            (root / 'tests/helper.py').write_text('import unittest\nfrom value import VALUE\nclass BaseCase(unittest.TestCase):\n def test_value(self):\n  self.assertEqual(VALUE, 1)\n')
            (root / 'tests/test_value.py').write_text('from .helper import BaseCase\nclass Check(BaseCase):\n pass\n')
            git('add', '.')
            git('commit', '-qm', 'fix')
            head = git('rev-parse', 'HEAD')
            with patch.object(repro, 'metadata', return_value=('fix fixture', '', base, head)):
                self.assertEqual(repro.gate(root), 0)

    def repro_run_paths(self, tmp, runner_temp):
        import os
        import subprocess
        from unittest.mock import patch
        root = Path(tmp) / 'repo'
        seen = Path(tmp) / 'seen.txt'
        root.mkdir()
        def git(*args):
            return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL, text=True).strip()
        git('init', '-q', '-b', 'main')
        git('config', 'user.name', 'Fixture')
        git('config', 'user.email', 'fixture@localhost')
        (root / 'value.py').write_text('VALUE = 0\n')
        git('add', '.')
        git('commit', '-qm', 'base')
        base = git('rev-parse', 'HEAD')
        git('switch', '-qc', 'fix/fixture')
        (root / 'value.py').write_text('VALUE = 1\n')
        (root / 'test_value.py').write_text(f'import unittest\nfrom value import VALUE\nclass Check(unittest.TestCase):\n def test_value(self):\n  open({str(seen)!r}, "a").write(__file__ + "\\n")\n  self.assertEqual(VALUE, 1)\n')
        git('add', '.')
        git('commit', '-qm', 'fix')
        head = git('rev-parse', 'HEAD')
        with patch.dict(os.environ, {'RUNNER_TEMP': runner_temp}), patch.object(repro, 'metadata', return_value=('fix fixture', '', base, head)):
            self.assertEqual(repro.gate(root), 0)
        runs = [Path(path).resolve() for path in seen.read_text().splitlines()]
        self.assertEqual(len(runs), 2)
        return runs

    def test_work_dir_follows_runner_temp(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            runner_temp = Path(tmp) / 'runner-temp'
            runner_temp.mkdir()
            for path in self.repro_run_paths(tmp, str(runner_temp)):
                self.assertTrue(path.is_relative_to(runner_temp.resolve()), path)

    def test_empty_runner_temp_keeps_the_system_temp_dir(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            for path in self.repro_run_paths(tmp, ''):
                work_dir = path.parents[1]
                self.assertTrue(work_dir.name.startswith('cold-repro-'), path)
                self.assertEqual(work_dir.parent, Path(tempfile.gettempdir()).resolve())

    def test_new_inherited_case_in_existing_test_file_is_selected(self):
        import subprocess
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                return subprocess.check_output(['git', '-C', tmp, *args], stderr=subprocess.DEVNULL, text=True).strip()
            git('init', '-q', '-b', 'main')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@localhost')
            (root / '.gitignore').write_text('.cold/\n')
            (root / 'value.py').write_text('VALUE = 0\n')
            (root / 'tests').mkdir()
            (root / 'tests/__init__.py').write_text('')
            (root / 'tests/helper.py').write_text('import unittest\nfrom value import VALUE\nclass BaseCase(unittest.TestCase):\n def test_value(self):\n  self.assertEqual(VALUE, 1)\n')
            (root / 'tests/test_value.py').write_text('from .helper import BaseCase\n')
            git('add', '.')
            git('commit', '-qm', 'base')
            base = git('rev-parse', 'HEAD')
            git('switch', '-qc', 'fix/fixture')
            (root / 'value.py').write_text('VALUE = 1\n')
            (root / 'tests/test_value.py').write_text('from .helper import BaseCase\nclass Check(BaseCase):\n pass\n')
            git('add', '.')
            git('commit', '-qm', 'fix')
            head = git('rev-parse', 'HEAD')
            with patch.object(repro, 'metadata', return_value=('fix fixture', '', base, head)):
                self.assertEqual(repro.gate(root), 0)

    def test_new_support_module_next_to_test_is_overlaid(self):
        import subprocess
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                return subprocess.check_output(['git', '-C', tmp, *args], stderr=subprocess.DEVNULL, text=True).strip()
            git('init', '-q', '-b', 'main')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@localhost')
            (root / '.gitignore').write_text('.cold/\n')
            (root / 'value.py').write_text('VALUE = 0\n')
            git('add', '.')
            git('commit', '-qm', 'base')
            base = git('rev-parse', 'HEAD')
            git('switch', '-qc', 'fix/fixture')
            (root / 'value.py').write_text('VALUE = 1\n')
            (root / 'install').mkdir()
            (root / 'install/helper.py').write_text('EXPECTED = 1\n')
            (root / 'install/test_value.py').write_text('import unittest\nfrom value import VALUE\nfrom helper import EXPECTED\nclass Check(unittest.TestCase):\n def test_value(self):\n  self.assertEqual(VALUE, EXPECTED)\n')
            git('add', '.')
            git('commit', '-qm', 'fix')
            head = git('rev-parse', 'HEAD')
            with patch.object(repro, 'metadata', return_value=('fix fixture', '', base, head)):
                self.assertEqual(repro.gate(root), 0)

    def test_outer_pr_event_does_not_apply_to_fixture_workspace(self):
        import os
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = root / 'fixture'
            fixture.mkdir()
            with patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'pull_request', 'GITHUB_WORKSPACE': str(root)}):
                self.assertTrue(repro.pr_event_applies(root))
                self.assertFalse(repro.pr_event_applies(fixture))
                self.assertEqual(repro.gate(fixture), 0)

    def test_archive_without_pr_metadata_does_not_block_verification(self):
        import os
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            self.assertEqual(repro.gate(Path(tmp)), 0)

    def test_github_merge_checkout_tests_the_actual_head(self):
        import json
        import os
        import subprocess
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                return subprocess.check_output(['git', '-C', tmp, *args], stderr=subprocess.DEVNULL, text=True).strip()
            git('init', '-q', '-b', 'main')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@localhost')
            (root / '.gitignore').write_text('.cold/\n')
            (root / 'value.py').write_text('VALUE = 0\n')
            (root / 'test_existing.py').write_text('import unittest\nclass Existing(unittest.TestCase):\n def test_existing(self):\n  self.assertEqual(1, 1)\n')
            git('add', '.')
            git('commit', '-qm', 'base')
            git('switch', '-qc', 'fix/fixture')
            (root / 'value.py').write_text('VALUE = 1\n')
            (root / 'test_value.py').write_text('import unittest\nfrom value import VALUE\nclass Check(unittest.TestCase):\n def test_value(self):\n  self.assertEqual(VALUE, 1)\n')
            git('add', '.')
            git('commit', '-qm', 'fix')
            head = git('rev-parse', 'HEAD')
            git('switch', '-q', 'main')
            (root / 'unrelated').write_text('base update')
            (root / 'test_existing.py').write_text('import unittest\nclass Existing(unittest.TestCase):\n def test_existing(self):\n  self.assertEqual(2, 2)\n')
            git('add', '.')
            git('commit', '-qm', 'base update')
            base = git('rev-parse', 'HEAD')
            git('merge', '--no-ff', '-qm', 'merge PR', head)
            with patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'pull_request', 'GITHUB_HEAD_REF': 'fix/fixture', 'GITHUB_WORKSPACE': str(root)}), patch.object(repro, 'metadata', return_value=('fix fixture', '', base, head)):
                self.assertEqual(repro.gate(root), 0)
            receipt = json.loads((root / '.cold/verify-repro.json').read_text())
            self.assertEqual(receipt['head'], head)
            self.assertEqual(len(receipt['tests']), 1)
            self.assertEqual(receipt['tests'][0]['base_exit'], 1)
            self.assertEqual(receipt['tests'][0]['head_exit'], 0)

    def test_base_pass_does_not_prove_the_fix(self):
        self.check_gate(1, 1, 0, 'FAIL')

    def test_base_failure_and_head_pass_prove_the_fix(self):
        self.check_gate(0, 0, 1, 'PASS')

    def test_import_error_does_not_count_as_reproduction(self):
        self.check_gate(None, 1, 2, 'FAIL')

    def check_gate(self, base_value, expected, base_exit, result):
        import json
        import subprocess
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                return subprocess.check_output(['git', '-C', tmp, *args], stderr=subprocess.DEVNULL, text=True).strip()
            git('init', '-q', '-b', 'main')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@localhost')
            (root / '.gitignore').write_text('.cold/\n')
            if base_value is not None:
                (root / 'value.py').write_text(f'VALUE = {base_value}\n')
            git('add', '.')
            git('commit', '-qm', 'base')
            base = git('rev-parse', 'HEAD')
            git('switch', '-qc', 'fix/fixture')
            (root / 'value.py').write_text('VALUE = 1\n')
            (root / 'test_value.py').write_text('import unittest\nfrom value import VALUE\nclass Check(unittest.TestCase):\n def test_value(self):\n  self.assertEqual(VALUE, 1)\n')
            git('add', '.')
            git('commit', '-qm', 'fix')
            head = git('rev-parse', 'HEAD')
            with patch.dict('os.environ', {'GIT_DIR': '/nonexistent/cold-repro-git'}), patch.object(repro, 'metadata', return_value=('fix fixture', '', base, head)):
                self.assertEqual(repro.gate(root), expected)
            receipt = json.loads((root / '.cold/verify-repro.json').read_text())
            self.assertEqual(receipt['result'], result)
            self.assertEqual(receipt['tests'][0]['base_exit'], base_exit)
            self.assertEqual(receipt['tests'][0]['head_exit'], 0)


class ReproOutputExcerpts(unittest.TestCase):
    def test_each_side_keeps_a_redacted_excerpt_of_its_output(self):
        import subprocess
        import tempfile
        from unittest.mock import patch
        token = 'ghp_' + 'A1b2C3d4' * 4
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                return subprocess.check_output(['git', '-C', tmp, *args], stderr=subprocess.DEVNULL, text=True).strip()
            git('init', '-q', '-b', 'main')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@localhost')
            (root / '.gitignore').write_text('.cold/\n')
            (root / 'value.py').write_text('VALUE = 0\n')
            git('add', '.')
            git('commit', '-qm', 'base')
            base = git('rev-parse', 'HEAD')
            git('switch', '-qc', 'fix/fixture')
            (root / 'value.py').write_text('VALUE = 1\n')
            (root / 'test_value.py').write_text(
                'import unittest\nfrom value import VALUE\nclass Check(unittest.TestCase):\n def test_value(self):\n'
                f'  print("using {token}")\n  self.assertEqual(VALUE, 1, "value is wrong")\n')
            git('add', '.')
            git('commit', '-qm', 'fix')
            head = git('rev-parse', 'HEAD')
            with patch.object(repro, 'metadata', return_value=('fix fixture', '', base, head)):
                self.assertEqual(repro.gate(root), 0)
            test = json.loads((root / '.cold/verify-repro.json').read_text())['tests'][0]
        self.assertIn('AssertionError: 0 != 1 : value is wrong', test['base_output'])
        self.assertIn('OK', test['head_output'])
        for side in ('base_output', 'head_output'):
            self.assertIn('using [REDACTED]', test[side])
            self.assertNotIn(token, test[side])
            self.assertLessEqual(len(test[side]), repro.OUTPUT_LIMIT + 3)


    def test_a_long_log_keeps_its_ends_and_never_shows_a_secret_cut_at_a_chunk_edge(self):
        import os
        import sys
        token = 'ghp_' + 'Z9y8X7w6' * 4
        key = '-----BEGIN RSA PRIVATE ' 'KEY-----\nMIIEsecretkeymaterial\n-----END RSA PRIVATE KEY-----'
        script = ('import sys; w = sys.stdout.write; w("first " + "x" * 50 + " "); w(sys.argv[1] + " " + "y" * 5000 + " ");'
                  ' w(sys.argv[2] + "\\npassword: hunter2hunter2\\nlast")')
        captured = []
        self.assertEqual(repro.run_test([sys.executable, '-c', script, token, key], Path.cwd(), dict(os.environ), captured, limit=60), 0)
        found = captured[0]
        self.assertTrue(found.startswith('first') and found.endswith('last'), found)
        for secret in (token[:12], 'secretkeymaterial', 'hunter2'):
            self.assertNotIn(secret, found)

    def test_a_log_cut_inside_an_unfinished_private_key_hides_the_key_body(self):
        import os
        import sys
        script = ('import sys; w = sys.stdout.write; w("started " + "x" * 5000 + "\\n-----BEGIN RSA PRIVATE ' 'KEY-----\\n");'
                  ' w("MIIEsecret12\\nabcSecret9\\n" * 50)')
        captured = []
        self.assertEqual(repro.run_test([sys.executable, '-c', script], Path.cwd(), dict(os.environ), captured, limit=60), 0)
        self.assertNotIn('ecret', captured[0])
        self.assertTrue(captured[0].startswith('started'), captured[0])

    def test_a_background_child_holding_the_pipe_does_not_lose_printed_output(self):
        import os
        import sys
        script = ('import subprocess, sys; subprocess.Popen([sys.executable, "-c", "import time; time.sleep(8)"]);'
                  ' print("Ran 1 test OK", flush=True)')
        captured = []
        self.assertEqual(repro.run_test([sys.executable, '-c', script], Path.cwd(), dict(os.environ), captured), 0)
        self.assertIn('Ran 1 test OK', captured[0])

    def test_a_whitespace_only_or_short_log_is_kept_whole(self):
        self.assertEqual(repro.output_excerpt(b' ' * 70, b' ' * 70, True), '…')
        self.assertEqual(repro.output_excerpt(b'ok\n', b'', False), 'ok')

    def test_a_captured_run_holds_at_most_two_chunks_in_memory(self):
        import os
        import sys
        captured = []
        script = 'import sys; sys.stdout.write("a " * 2_000_000 + "end")'
        repro.run_test([sys.executable, '-c', script], Path.cwd(), dict(os.environ), captured, limit=1000)
        self.assertTrue(captured[0].endswith('end'))
        self.assertLessEqual(len(captured[0]), repro.OUTPUT_LIMIT + 3)


class NothingReproducedIsIncomplete(unittest.TestCase):
    def repo(self, tmp, change_tests):
        import subprocess
        root = Path(tmp)
        def git(*args):
            return subprocess.check_output(['git', '-C', tmp, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
                                            *args], stderr=subprocess.DEVNULL, text=True).strip()
        git('init', '-q', '-b', 'main')
        (root / '.gitignore').write_text('.cold/\n')
        (root / 'value.py').write_text('VALUE = 0\n')
        git('add', '.')
        git('commit', '-qm', 'base')
        base = git('rev-parse', 'HEAD')
        git('switch', '-qc', 'feature')
        (root / 'value.py').write_text('VALUE = 1\n')
        if change_tests:
            (root / 'test_value.py').write_text('import unittest\nfrom value import VALUE\nclass Check(unittest.TestCase):\n def test_value(self):\n  self.assertEqual(VALUE, 1)\n')
        git('add', '.')
        git('commit', '-qm', 'change')
        return root, base, git('rev-parse', 'HEAD')

    def gate(self, root, details, reason=None):
        from unittest.mock import patch
        with patch.dict('os.environ', {'GIT_DIR': '/nonexistent/cold-repro-git'}):
            code = repro.gate(root, True, reason, details)
        return code, json.loads((root / '.cold/verify-repro.json').read_text())

    def test_a_change_without_a_new_test_is_incomplete_not_a_failure_or_a_pass(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root, base, head = self.repo(tmp, change_tests=False)
            code, receipt = self.gate(root, ['Change value', '', base, head])
            self.assertEqual(code, 2)
            self.assertEqual(receipt['result'], 'INCOMPLETE')
            self.assertIn('nothing reproduces the claim', receipt['error'])

    def test_an_opt_out_is_recorded_and_is_incomplete_not_a_pass(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root, base, head = self.repo(tmp, change_tests=True)
            code, receipt = self.gate(root, ['Change value', '', base, head], reason='only a docs change')
            self.assertEqual(code, 2)
            self.assertEqual(receipt['result'], 'INCOMPLETE')
            self.assertEqual(receipt['opt_out_reason'], 'only a docs change')
            self.assertEqual(receipt['tests'], [])

    def test_uncommitted_work_is_incomplete(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root, base, head = self.repo(tmp, change_tests=True)
            (root / 'value.py').write_text('VALUE = 2\n')
            code, receipt = self.gate(root, ['Change value', '', base, head])
            self.assertEqual(code, 2)
            self.assertIn('commit all changes', receipt['error'])


class InfrastructureErrorsAreIncomplete(unittest.TestCase):
    repo = NothingReproducedIsIncomplete.repo
    gate = NothingReproducedIsIncomplete.gate

    def test_a_failed_fetch_is_incomplete_with_the_git_error(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root, _, head = self.repo(tmp, change_tests=True)
            code, receipt = self.gate(root, ['Change value', '', 'f' * 40, head])
            self.assertEqual((code, receipt['result']), (2, 'INCOMPLETE'))
            self.assertTrue(receipt['error'].startswith('git fetch --depth=1 failed: '), receipt['error'])
            self.assertIn('origin', receipt['error'])

    def test_a_failed_worktree_add_is_incomplete_with_the_git_error(self):
        import subprocess
        import tempfile
        from unittest.mock import patch
        real = repro.git

        def git(root, *args):
            if args[:2] == ('worktree', 'add') and args[-1] == head:
                raise subprocess.CalledProcessError(128, ['git', *args], stderr=b'fatal: could not create work tree dir')
            return real(root, *args)

        with tempfile.TemporaryDirectory() as tmp:
            root, base, head = self.repo(tmp, change_tests=True)
            with patch.object(repro, 'git', side_effect=git):
                code, receipt = self.gate(root, ['Change value', '', base, head])
            self.assertEqual((code, receipt['result']), (2, 'INCOMPLETE'))
            self.assertEqual(receipt['error'], 'git worktree add failed: fatal: could not create work tree dir')
            self.assertEqual(real(root, 'worktree', 'list', '--porcelain').decode().count('worktree '), 1)


class MixedReproCases(unittest.TestCase):
    def check_mixed_gate(self, cases, expected):
        import contextlib
        import io
        import subprocess
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                return subprocess.check_output(['git', '-C', tmp, *args], stderr=subprocess.DEVNULL, text=True).strip()
            git('init', '-q', '-b', 'main')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@localhost')
            (root / '.gitignore').write_text('.cold/\n')
            (root / 'value.py').write_text('VALUE = 0\n')
            git('add', '.')
            git('commit', '-qm', 'base')
            base = git('rev-parse', 'HEAD')
            (root / 'value.py').write_text('VALUE = 1\n')
            bodies = {
                'proof': 'self.assertEqual(VALUE, 1)',
                'acceptance': 'self.assertIn(VALUE, (0, 1))',
                'red': 'self.assertEqual(VALUE, 0)',
                'unchanged': 'self.skipTest("historical skip")',
                'lost': 'self.skipTest("head skip") if VALUE else self.assertEqual(VALUE, 0)',
                'unfixed': 'self.skipTest("head skip") if VALUE else self.fail("still broken")',
            }
            source = 'import unittest\nfrom value import VALUE\nclass Check(unittest.TestCase):\n'
            source += ''.join(f' def test_{name}(self):\n  {bodies[name]}\n' for name in cases)
            (root / 'test_value.py').write_text(source)
            git('add', '.')
            git('commit', '-qm', 'fix')
            head = git('rev-parse', 'HEAD')
            output = io.StringIO()
            with patch.object(repro, 'metadata', return_value=('fix fixture', '', base, head)), contextlib.redirect_stdout(output):
                code = repro.gate(root)
            receipt = json.loads((root / '.cold/verify-repro.json').read_text())
            self.assertEqual((code, receipt['result']), expected)
            kinds = {t['name'].split('test_')[-1]: t.get('kind') for t in receipt['tests']}
            self.assertEqual(kinds, {name: name if name in ('proof', 'acceptance', 'unchanged') else 'regression' for name in cases})
            for test in receipt['tests']:
                self.assertIn(f"{test['name']}: base={test['base_exit']} head={test['head_exit']} ({test['kind']})", output.getvalue())

    def test_proof_and_acceptance_pass(self):
        self.check_mixed_gate(('proof', 'acceptance'), (0, 'PASS'))

    def test_acceptance_only_fails(self):
        self.check_mixed_gate(('acceptance',), (1, 'FAIL'))

    def test_proof_and_red_head_fail(self):
        self.check_mixed_gate(('proof', 'red'), (1, 'FAIL'))

    def test_proof_and_unchanged_skip_pass(self):
        self.check_mixed_gate(('proof', 'unchanged'), (0, 'PASS'))

    def test_base_pass_to_head_skip_fails(self):
        self.check_mixed_gate(('proof', 'lost'), (1, 'FAIL'))

    def test_base_failure_to_head_skip_fails(self):
        self.check_mixed_gate(('proof', 'unfixed'), (1, 'FAIL'))



class ProofMustExerciseTheChange(unittest.TestCase):
    BASE_APP = 'def login(expired):\n    return not expired\nVERSION = 1\n'
    FIXED_LOGIN = 'def login(expired):\n    return True\nVERSION = 2\n'
    VERSION_TEST = 'import unittest\nimport app\nclass V(unittest.TestCase):\n def test_login_fixed(self):\n  self.assertEqual(app.VERSION, 2)\n'
    LOGIN_TEST = 'import unittest\nimport app\nclass V(unittest.TestCase):\n def test_login_fixed(self):\n  self.assertTrue(app.login(True))\n'

    def gate(self, app, test, exercised=True, base_app=None, app_name='app.py', extra=None, head_extra=None, rename=None):
        import contextlib
        import io
        import subprocess
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        def git(*args):
            return subprocess.check_output(['git', '-C', tmp.name, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
                                            *args], stderr=subprocess.DEVNULL, text=True).strip()
        git('init', '-q', '-b', 'main')
        (root / '.gitignore').write_text('.cold/\n')
        (root / (rename or app_name)).write_text(base_app or self.BASE_APP)
        for name, text in (extra or {}).items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)
        git('add', '.')
        git('commit', '-qm', 'base')
        base = git('rev-parse', 'HEAD')
        git('switch', '-qc', 'fix/login')
        if rename:
            git('mv', rename, app_name)
        (root / app_name).write_text(app)
        for name, text in (head_extra or {}).items():
            (root / name).write_text(text)
        (root / 'tests').mkdir()
        (root / 'tests/test_fixed.py').write_text(test)
        git('add', '.')
        git('commit', '-qm', 'fix: login with expired session')
        head = git('rev-parse', 'HEAD')
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = repro.gate(root, details=('fix: login with expired session', '', base, head), exercised=exercised)
        receipt = json.loads((root / '.cold/verify-repro.json').read_text())
        return code, receipt

    def test_repro_a_constant_bump_is_not_proof(self):
        code, receipt = self.gate(self.BASE_APP.replace('VERSION = 1', 'VERSION = 2'), self.VERSION_TEST)
        self.assertEqual((code, receipt['result']), (2, 'INCOMPLETE'))
        self.assertIn('proof does not exercise the change', receipt['error'])
        self.assertEqual([(t['base_exit'], t['head_exit'], t['kind']) for t in receipt['tests']], [(1, 0, 'unproven')])

    def test_changed_code_run_only_while_importing_is_not_proof(self):
        for value in ('[2 for _ in range(1)][0]', 'next(2 for _ in range(1))', '(lambda: 2)()'):
            with self.subTest(value=value):
                code, receipt = self.gate(self.BASE_APP.replace('VERSION = 1', 'VERSION = ' + value), self.VERSION_TEST)
                self.assertEqual((code, receipt['result']), (2, 'INCOMPLETE'))
                self.assertIn('proof does not exercise the change', receipt['error'])

    def test_compat_runs_keep_their_existing_verdict(self):
        code, receipt = self.gate(self.BASE_APP.replace('VERSION = 1', 'VERSION = 2'), self.VERSION_TEST, exercised=False)
        self.assertEqual((code, receipt['result']), (0, 'PASS'))
        self.assertEqual([t['kind'] for t in receipt['tests']], ['proof'])

    def test_a_constant_test_beside_a_real_fix_is_still_not_proof(self):
        code, receipt = self.gate(self.FIXED_LOGIN, self.VERSION_TEST)
        self.assertEqual((code, receipt['result']), (2, 'INCOMPLETE'))
        self.assertIn('proof does not exercise the change', receipt['error'])

    def test_candidate_hit_reports_are_not_strict_proof(self):
        from unittest.mock import patch

        def replay(root, writable, command, directory, env, capture=None, **kwargs):
            if command[2] == '--list':
                Path(command[-1]).write_text('["V.test_login_fixed"]')
                return 0
            capture.append('')
            if directory.name == 'base':
                return 1
            (Path(env['HOME']) / 'hits.json').write_text(report)
            return 0

        for report in ('[]', 'not json', '["app.py", 100]', '["app.py", 2]'):
            with self.subTest(report=report), patch.object(repro.platform, 'system', return_value='Linux'), \
                    patch.object(repro, 'isolated_test', replay):
                code, receipt = self.gate(self.FIXED_LOGIN, self.VERSION_TEST)
                self.assertEqual((code, receipt['result']), (2, 'INCOMPLETE'))
                self.assertEqual([(t['base_exit'], t['head_exit'], t['kind']) for t in receipt['tests']],
                                 [(1, 0, 'unproven')])

    def assert_unobserved_replay(self, code, receipt):
        self.assertEqual((code, receipt['result']), (2, 'INCOMPLETE'))
        self.assertIn('python-coverage-not-runner-observed', receipt['error'])
        self.assertEqual([(t['base_exit'], t['head_exit'], t['kind']) for t in receipt['tests']], [(1, 0, 'unproven')])

    def test_a_real_python_replay_records_both_exits_without_proof(self):
        code, receipt = self.gate(self.FIXED_LOGIN, self.LOGIN_TEST)
        self.assert_unobserved_replay(code, receipt)

    def test_a_fix_run_through_a_python_subprocess_replays_without_proof(self):
        test = ('import os, subprocess, sys, unittest\nROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\n'
                'class V(unittest.TestCase):\n def test_login_fixed(self):\n'
                '  out = subprocess.run([sys.executable, "-c", "import app; print(app.login(True))"], cwd=ROOT, capture_output=True, text=True).stdout\n'
                '  self.assertEqual(out.strip(), "True")\n')
        code, receipt = self.gate(self.FIXED_LOGIN, test)
        self.assert_unobserved_replay(code, receipt)

    def test_a_replay_subprocess_still_loads_the_projects_own_sitecustomize(self):
        test = ('import os, subprocess, sys, unittest\nROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\n'
                'class V(unittest.TestCase):\n def test_login_fixed(self):\n'
                '  env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [os.environ.get("PYTHONPATH"), os.path.join(ROOT, "hooks")])))\n'
                '  out = subprocess.run([sys.executable, "-c", "import app, builtins; print(app.login(True) and builtins.PROJECT_HOOK)"],'
                ' cwd=ROOT, env=env, capture_output=True, text=True).stdout\n'
                '  self.assertEqual(out.strip(), "True")\n')
        app = self.FIXED_LOGIN
        code, receipt = self.gate(app, test, extra={'hooks/sitecustomize.py': 'import builtins\nbuiltins.PROJECT_HOOK = True\n'})
        self.assert_unobserved_replay(code, receipt)

    def test_a_changed_file_with_a_quoted_git_path_replays_without_proof(self):
        test = self.LOGIN_TEST.replace('import app\n', 'import importlib\napp = importlib.import_module("appé")\n')
        code, receipt = self.gate(self.FIXED_LOGIN, test, app_name='appé.py')
        self.assert_unobserved_replay(code, receipt)

    def test_a_deletion_only_fix_replays_without_proof(self):
        base = 'def login(expired):\n    if expired:\n        return False\n    return True\n'
        code, receipt = self.gate('def login(expired):\n    return True\n', self.LOGIN_TEST, base_app=base)
        self.assert_unobserved_replay(code, receipt)

    def test_an_installed_copy_maps_back_to_the_changed_source(self):
        import contextlib
        import io
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as checkout, tempfile.TemporaryDirectory() as venv:
            package = Path(venv) / 'lib/site-packages/pkg'
            package.mkdir(parents=True)
            (package / '__init__.py').write_text('')
            (package / 'mod.py').write_text('def answer():\n    return 42\n')
            test = Path(checkout) / 'test_mod.py'
            test.write_text(f'import sys\nsys.path.insert(0, {str(package.parent)!r})\nimport unittest\nfrom pkg.mod import answer\n'
                            'class T(unittest.TestCase):\n def test_answer(self):\n  self.assertEqual(answer(), 42)\n')
            source = Path(checkout) / 'src/pkg/mod.py'
            source.parent.mkdir(parents=True)
            cwd = os.getcwd()
            cases = ((('def answer():\n    return 42\n'), {'src/pkg/mod.py': [2]}, True),
                     (('def answer():\n    return 42\n'), {'src/pkg/mod.py': [3]}, False),
                     (('def answer():\n    return 42\n'), {'other.py': [2]}, False),
                     (('def answer():\n    return 41\n'), {'src/pkg/mod.py': [2]}, False))
            for index, (text, changed, expected) in enumerate(cases):
                source.write_text(text)
                changes = Path(checkout) / f'changes-{index}.json'
                hits = Path(checkout) / f'hits-{index}.json'
                changes.write_text(json.dumps(changed))
                os.chdir(checkout)
                try:
                    with contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(repro.run_traced_case(str(test), 'T.test_answer', str(changes), str(hits)), 0)
                finally:
                    os.chdir(cwd)
                    os.environ.pop('COLD_REPRO_TRACE', None)
                    sys.modules.pop('pkg.mod', None)
                    sys.modules.pop('pkg', None)
                self.assertEqual(hits.is_file(), expected)


class NewFeatureMissingOnBase(unittest.TestCase):
    gate = ProofMustExerciseTheChange.gate
    BASE_APP = ProofMustExerciseTheChange.BASE_APP
    FEATURE = BASE_APP + 'def multiply(a, b):\n    return a * b\n'
    IMPORT_TEST = 'import unittest\nfrom app import multiply\nclass M(unittest.TestCase):\n def test_multiply(self):\n  self.assertEqual(multiply(2, 3), 6)\n'

    def outcome(self, app, test, **options):
        code, receipt = self.gate(app, test, **options)
        return code, receipt['result'], [(t['base_exit'], t['head_exit'], t['kind']) for t in receipt['tests']]

    def test_repro_b_an_import_of_the_new_function_replays_without_proof(self):
        self.assertEqual(self.outcome(self.FEATURE, self.IMPORT_TEST), (2, 'INCOMPLETE', [(3, 0, 'unproven')]))

    def test_an_attribute_the_head_adds_is_missing_on_base(self):
        test = 'import unittest\nimport app\nclass M(unittest.TestCase):\n def test_multiply(self):\n  self.assertEqual(app.multiply(2, 3), 6)\n'
        self.assertEqual(self.outcome(self.FEATURE, test), (2, 'INCOMPLETE', [(3, 0, 'unproven')]))

    def test_a_pytest_function_importing_the_new_function_is_missing_on_base(self):
        test = 'from app import multiply\ndef test_multiply():\n    assert multiply(2, 3) == 6\n'
        self.assertEqual(self.outcome(self.FEATURE, test), (2, 'INCOMPLETE', [(3, 0, 'unproven')]))

    def test_a_new_module_is_missing_on_base(self):
        test = 'import unittest\nfrom maths import multiply\nclass M(unittest.TestCase):\n def test_multiply(self):\n  self.assertEqual(multiply(2, 3), 6)\n'
        maths = {'maths.py': 'def multiply(a, b):\n    return a * b\n'}
        self.assertEqual(self.outcome(self.BASE_APP, test, head_extra=maths), (2, 'INCOMPLETE', [(3, 0, 'unproven')]))

    def test_a_missing_name_the_head_does_not_define_is_only_an_error(self):
        app = self.BASE_APP + 'globals()["multiply"] = lambda a, b: a * b\ndef touched():\n    multiply = 1\n    return multiply\n'
        self.assertEqual(self.outcome(app, self.IMPORT_TEST), (1, 'FAIL', [(2, 0, 'unproven')]))

    def test_a_name_added_to_another_module_does_not_explain_the_error(self):
        test = 'import unittest\nimport other\nclass M(unittest.TestCase):\n def test_ready(self):\n  self.assertTrue(other.ready())\n'
        app = self.BASE_APP + 'def ready():\n    return True\n'
        other = {'other.py': 'import app\nglobals()["ready"] = app.ready\n'}
        self.assertEqual(self.outcome(app, test, extra={'other.py': 'X = 1\n'}, head_extra=other), (1, 'FAIL', [(2, 0, 'unproven')]))

    def test_a_name_added_to_a_same_named_module_in_another_package_does_not_count(self):
        test = 'import unittest\nimport app\nclass M(unittest.TestCase):\n def test_feature(self):\n  self.assertTrue(app.feature())\n'
        app = self.BASE_APP + 'import other.app\nglobals()["feature"] = other.app.feature\n'
        nested = {'other/__init__.py': '', 'other/app.py': 'def feature():\n    return True\n'}
        self.assertEqual(self.outcome(app, test, head_extra=nested, extra={'other/__init__.py': ''}), (1, 'FAIL', [(2, 0, 'unproven')]))

    def test_a_function_added_inside_a_package_is_missing_on_base(self):
        test = 'import unittest\nfrom pkg.maths import multiply\nclass M(unittest.TestCase):\n def test_multiply(self):\n  self.assertEqual(multiply(2, 3), 6)\n'
        base = {'src/pkg/__init__.py': '', 'src/pkg/maths.py': 'def add(a, b):\n    return a + b\n'}
        head = {'src/pkg/maths.py': 'def add(a, b):\n    return a + b\ndef multiply(a, b):\n    return a * b\n'}
        test = test.replace('import unittest\n', 'import os, sys, unittest\nsys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))\n')
        self.assertEqual(self.outcome(self.BASE_APP, test, extra=base, head_extra=head), (2, 'INCOMPLETE', [(3, 0, 'unproven')]))

    def test_a_method_added_to_a_parent_class_counts_through_a_child(self):
        base = 'class Parent:\n    pass\nclass Child(Parent):\n    pass\n'
        app = 'class Parent:\n    def feature(self):\n        return 2\nclass Child(Parent):\n    pass\n'
        test = 'import unittest\nimport app\nclass M(unittest.TestCase):\n def test_feature(self):\n  self.assertEqual(app.Child().feature(), 2)\n'
        self.assertEqual(self.outcome(app, test, base_app=base), (2, 'INCOMPLETE', [(3, 0, 'unproven')]))

    def test_a_renamed_module_is_missing_on_base_under_its_new_name(self):
        test = 'import unittest\nfrom renamed import multiply\nclass M(unittest.TestCase):\n def test_multiply(self):\n  self.assertEqual(multiply(2, 3), 6)\n'
        old = 'def multiply(a, b):\n    return a + b\n' + 'x = 1\n' * 20
        self.assertEqual(self.outcome(old.replace('a + b', 'a * b'), test, base_app=old, app_name='renamed.py', rename='orig.py'),
                         (2, 'INCOMPLETE', [(3, 0, 'unproven')]))

    def test_a_method_added_to_one_class_counts_when_another_class_has_it(self):
        base = 'class A:\n    def run(self):\n        return 1\nclass B:\n    pass\n'
        app = 'class A:\n    def run(self):\n        return 1\nclass B:\n    def run(self):\n        return 2\n'
        test = 'import unittest\nimport app\nclass M(unittest.TestCase):\n def test_run(self):\n  self.assertEqual(app.B().run(), 2)\n'
        self.assertEqual(self.outcome(app, test, base_app=base), (2, 'INCOMPLETE', [(3, 0, 'unproven')]))

    def test_a_new_constant_read_only_at_import_is_not_proof(self):
        test = 'import unittest\nfrom app import FACTOR\nclass M(unittest.TestCase):\n def test_factor(self):\n  self.assertEqual(FACTOR, 3)\n'
        code, result, tests = self.outcome(self.BASE_APP + 'FACTOR = 3\n', test)
        self.assertEqual((code, result, tests), (2, 'INCOMPLETE', [(3, 0, 'unproven')]))

    def test_a_missing_name_that_fails_on_head_too_is_not_proof(self):
        test = self.IMPORT_TEST.replace('6)', '7)')
        self.assertEqual(self.outcome(self.FEATURE, test), (1, 'FAIL', [(3, 1, 'regression')]))

    def test_compat_runs_keep_their_existing_verdict(self):
        self.assertEqual(self.outcome(self.FEATURE, self.IMPORT_TEST, exercised=False), (1, 'FAIL', [(2, 0, 'unproven')]))
