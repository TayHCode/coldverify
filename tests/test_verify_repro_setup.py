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


class ProjectSetup(unittest.TestCase):
    PYPROJECT = '[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n[project]\nname = "fixture-app"\nversion = "0.1"\ndependencies = ["fixture-unavailable-dependency==9.9.9"]\n[tool.setuptools]\npy-modules = ["value"]\n'

    def gate_fixture(self, table, dependency_dir, requirements=None):
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
            (root / 'pyproject.toml').write_text(self.PYPROJECT)
            (root / 'value.py').write_text('VALUE = 0\n')
            if requirements:
                (root / 'requirements.txt').write_text(requirements)
            git('add', '.')
            git('commit', '-qm', 'base')
            base = git('rev-parse', 'HEAD')
            git('switch', '-qc', 'fix/fixture')
            (root / 'pyproject.toml').write_text(self.PYPROJECT + table)
            (root / 'value.py').write_text('VALUE = 1\n')
            (root / 'test_value.py').write_text('import unittest\nfrom sibling import SIBLING\nfrom value import VALUE\nclass Check(unittest.TestCase):\n def test_value(self):\n  self.assertEqual(VALUE, SIBLING)\n')
            git('add', '.')
            git('commit', '-qm', 'fix')
            head = git('rev-parse', 'HEAD')
            with patch.object(repro, 'metadata', return_value=('fix fixture', '', base, head)):
                code = repro.gate(root)
            return code, json.loads((root / '.cold/verify-repro.json').read_text())

    def test_declared_setup_lets_a_project_with_an_unavailable_dependency_pass(self):
        import tempfile
        with tempfile.TemporaryDirectory() as dependency:
            (Path(dependency) / 'sibling.py').write_text('SIBLING = 1\n')
            table = f'[tool.cold.repro]\nno-deps = true\npythonpath = [{json.dumps(dependency)}]\n'
            code, receipt = self.gate_fixture(table, dependency)
        self.assertEqual(receipt.get('error'), None)
        self.assertEqual(code, 0)
        self.assertEqual(receipt['result'], 'PASS')
        self.assertEqual(receipt['tests'][0]['base_exit'], 1)
        self.assertEqual(receipt['tests'][0]['head_exit'], 0)

    def test_declared_setup_still_requires_red_on_base(self):
        import tempfile
        with tempfile.TemporaryDirectory() as dependency:
            (Path(dependency) / 'sibling.py').write_text('SIBLING = 0\n')
            table = f'[tool.cold.repro]\nno-deps = true\npythonpath = [{json.dumps(dependency)}]\n'
            code, receipt = self.gate_fixture(table, dependency)
        self.assertEqual(code, 1)
        self.assertEqual(receipt['result'], 'FAIL')
        self.assertEqual(receipt['tests'][0]['base_exit'], 0)
        self.assertEqual(receipt['tests'][0]['head_exit'], 1)

    def test_no_deps_skips_unavailable_requirements(self):
        import tempfile
        with tempfile.TemporaryDirectory() as dependency:
            (Path(dependency) / 'sibling.py').write_text('SIBLING = 1\n')
            table = f'[tool.cold.repro]\nno-deps = true\npythonpath = [{json.dumps(dependency)}]\n'
            code, receipt = self.gate_fixture(table, dependency, 'fixture-unavailable-dependency==9.9.9\n')
        self.assertEqual(receipt.get('error'), None)
        self.assertEqual(code, 0)
        self.assertEqual(receipt['result'], 'PASS')

    def test_gate_without_the_table_does_not_need_tomllib(self):
        import subprocess
        import sys
        import tempfile
        script = Path(__file__).resolve().parents[1] / 'ci/verify_repro.py'
        probe = 'import importlib.util, sys\nsys.modules["tomllib"] = None\nspec = importlib.util.spec_from_file_location("verify_repro", sys.argv[1])\nmodule = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(module)\nprint(module.repro_setup(sys.argv[2]))\n'
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'pyproject.toml').write_text(self.PYPROJECT)
            result = subprocess.run([sys.executable, '-c', probe, str(script), tmp], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '(False, ())')

    def test_project_without_the_table_cannot_install_the_unavailable_dependency(self):
        code, receipt = self.gate_fixture('', None)
        self.assertEqual(code, 1)
        self.assertIn('could not install Python project', receipt['error'])

    def test_relative_pythonpath_is_rejected(self):
        code, receipt = self.gate_fixture('[tool.cold.repro]\nno-deps = true\npythonpath = ["sibling"]\n', None)
        self.assertEqual(code, 1)
        self.assertIn('must be absolute paths: sibling', receipt['error'])

    def test_malformed_table_is_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            for table in ('no-deps = "yes"\n', 'pythonpath = "/abs"\n', 'pythonpath = [1]\n'):
                (Path(tmp) / 'pyproject.toml').write_text('[tool.cold.repro]\n' + table)
                with self.assertRaises(RuntimeError):
                    repro.repro_setup(tmp)

    def test_inline_table_spelling_is_read(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'pyproject.toml').write_text('[tool.cold]\nrepro = { no-deps = true, pythonpath = ["/abs"] }\n')
            self.assertEqual(repro.repro_setup(tmp), (True, ('/abs',)))

    def test_without_the_table_nothing_changes(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp) / 'checkout'
            checkout.mkdir()
            (checkout / 'pyproject.toml').write_text(self.PYPROJECT)
            self.assertEqual(repro.repro_setup(checkout), (False, ()))
            self.assertEqual(repro.repro_setup(Path(tmp)), (False, ()))
            scratch = Path(tmp) / 'scratch'
            scratch.mkdir()
            commands = []
            with patch.object(repro, 'run_test', side_effect=lambda command, directory, env: commands.append((command, env)) or 0):
                repro.project_python(checkout, scratch, 'head', repro.repro_setup(checkout))
            python = str(scratch / 'head-venv/bin/python') if repro.os.name != 'nt' else str(scratch / 'head-venv/Scripts/python.exe')
            self.assertEqual([command for command, _ in commands], [
                [repro.sys.executable, '-m', 'venv', str(scratch / 'head-venv')],
                [python, '-m', 'pip', 'install', '-q', '.'],
            ])
            for _, env in commands:
                self.assertNotIn('PYTHONPATH', env)
            self.assertNotIn('PYTHONPATH', repro.case_environment(tmp))

    def test_pytest_is_installed_only_when_the_project_python_lacks_it(self):
        import tempfile
        from unittest.mock import patch
        for importable, expected in ((True, False), (False, True)):
            with self.subTest(importable=importable), tempfile.TemporaryDirectory() as tmp:
                commands = []
                def run(command, directory, env):
                    commands.append(command)
                    return int(not importable) if command[1:] == ['-c', 'import pytest'] else 0
                with patch.object(repro, 'run_test', side_effect=run):
                    python = repro.project_python(Path(tmp), Path(tmp), 'head', (False, ()), pytest=True)
                self.assertEqual(python != repro.sys.executable, expected)
                self.assertEqual(any(command[-1] == 'pytest' and 'install' in command for command in commands), expected)


class MissingBase(unittest.TestCase):
    def test_a_checkout_with_no_pr_and_no_origin_main_is_incomplete_not_fail(self):
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            for args in (['init', '-q'], ['config', 'user.name', 'Fixture'], ['config', 'user.email', 'fixture@localhost']):
                subprocess.run(['git', '-C', tmp, *args], check=True)
            (Path(tmp) / '.gitignore').write_text('.cold/\n')
            subprocess.run(['git', '-C', tmp, 'add', '.'], check=True)
            subprocess.run(['git', '-C', tmp, '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fix'], check=True)
            code = repro.gate(tmp, force=True, details=('', '', '', ''))
            receipt = json.loads((Path(tmp) / '.cold/verify-repro.json').read_text())
        self.assertEqual((code, receipt['result']), (2, 'INCOMPLETE'), receipt)
        self.assertIn('origin/main', receipt['error'])
