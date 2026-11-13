import contextlib
import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('coldverify_cli', str(ROOT / 'coldverify'))
spec = importlib.util.spec_from_loader(loader.name, loader)
coldverify = importlib.util.module_from_spec(spec)
loader.exec_module(coldverify)

COLD_VERIFY_FLAGS = ['--bug-fix', '--no-repro-reason', 'no test can reach it', '--ci', '--no-cache', '--gate', '--preview']


class ColdVerifyCallers(unittest.TestCase):
    def test_every_cold_verify_flag_is_accepted(self):
        args = coldverify.parser().parse_args(COLD_VERIFY_FLAGS)
        self.assertTrue(args.bug_fix and args.no_cache and args.ci and args.gate and args.preview)
        self.assertEqual(args.no_repro_reason, 'no test can reach it')

    def test_explain_skips_is_the_same_as_preview(self):
        self.assertTrue(coldverify.parser().parse_args(['--explain-skips']).preview)

    def test_an_unknown_flag_is_still_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            coldverify.parser().parse_args(['--not-a-cold-verify-flag'])


class OutsideAGitCheckout(unittest.TestCase):
    def test_strict_run_is_incomplete_with_a_reason_and_runs_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            empty = Path(folder) / 'empty'
            empty.mkdir()
            env = {key: value for key, value in os.environ.items() if not key.startswith(('GIT_', 'GITHUB_'))}
            env['GIT_CEILING_DIRECTORIES'] = folder
            result = subprocess.run([sys.executable, str(ROOT / 'coldverify'), '--ci', '--claim', 'x'], cwd=empty,
                                    env=env, text=True, capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual(result.stdout.rstrip().splitlines()[-1],
                             f'COLDVERIFY INCOMPLETE: {empty.resolve()} is not a Git checkout; run coldverify inside a Git repository')
            self.assertEqual(list(empty.iterdir()), [])


def git(repo, *args):
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True, env=env).stdout.strip()


def commit_all(repo):
    git(repo, 'init', '--quiet')
    git(repo, 'add', '--all', '--force')
    git(repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost', '-c', 'core.hooksPath=/dev/null',
        '-c', 'commit.gpgsign=false', 'commit', '--quiet', '--no-verify', '-m', 'snapshot')
    return git(repo, 'rev-parse', '--short=12', 'HEAD')


def project():
    import tomllib
    return tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']


class Version(unittest.TestCase):
    def copy_script(self, folder):
        copy = Path(folder) / 'copy'
        copy.mkdir()
        shutil.copy(ROOT / 'coldverify', copy)
        shutil.copy(ROOT / 'pyproject.toml', copy)
        return copy

    def version_output(self, copy, folder):
        env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        env['GIT_CEILING_DIRECTORIES'] = folder
        result = subprocess.run([sys.executable, str(copy / 'coldverify'), '--version'], env=env, text=True,
                                capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_a_checkout_prints_the_version_and_its_commit(self):
        with tempfile.TemporaryDirectory() as folder:
            copy = self.copy_script(folder)
            commit = commit_all(copy)
            self.assertEqual(self.version_output(copy, folder), f'coldverify {project()["version"]} ({commit})\n')

    def test_an_installed_copy_prints_the_commit_stamped_at_build_time(self):
        with tempfile.TemporaryDirectory() as folder:
            copy = self.copy_script(folder)
            (copy / 'COMMIT').write_text('0123456789ab\n')
            self.assertEqual(self.version_output(copy, folder), f'coldverify {project()["version"]} (0123456789ab)\n')

    def test_a_git_archive_install_prints_the_archived_commit(self):
        with tempfile.TemporaryDirectory() as folder:
            copy = self.copy_script(folder)
            for name in ('COMMIT', '.gitattributes'):
                shutil.copy(ROOT / name, copy)
            commit_all(copy)
            full = git(copy, 'rev-parse', 'HEAD')
            archived = Path(folder) / 'archived'
            archived.mkdir()
            tarball = subprocess.run(['git', '-C', str(copy), 'archive', 'HEAD'], capture_output=True, check=True).stdout
            subprocess.run(['tar', '-x', '-C', str(archived)], input=tarball, check=True)
            self.assertEqual(self.version_output(archived, folder), f'coldverify {project()["version"]} ({full})\n')

    def test_a_checkout_ignores_its_unexpanded_archive_stamp(self):
        with tempfile.TemporaryDirectory() as folder:
            copy = self.copy_script(folder)
            shutil.copy(ROOT / 'COMMIT', copy)
            commit = commit_all(copy)
            self.assertEqual(self.version_output(copy, folder), f'coldverify {project()["version"]} ({commit})\n')

    def test_a_copy_without_git_or_a_stamp_says_the_commit_is_unknown(self):
        with tempfile.TemporaryDirectory() as folder:
            copy = self.copy_script(folder)
            self.assertEqual(self.version_output(copy, folder), f'coldverify {project()["version"]} (unknown commit)\n')


class PythonFloor(unittest.TestCase):
    def test_pyproject_declares_the_floor_the_script_enforces(self):
        self.assertEqual(project()['requires-python'], '>=' + '.'.join(map(str, coldverify.PYTHON_FLOOR)))

    def test_an_older_python_is_refused_with_a_reason_before_anything_runs(self):
        probe = ('import runpy, sys\nscript = sys.argv[1]\nsys.version_info = (3, 11, 2)\n'
                 'sys.argv = [script, "--version"]\nrunpy.run_path(script, run_name="__main__")\n')
        result = subprocess.run([sys.executable, '-c', probe, str(ROOT / 'coldverify')], text=True, capture_output=True,
                                timeout=60)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr, f'coldverify needs Python 3.12 or newer; {sys.executable} is Python 3.11.2\n')

    def test_an_older_python3_on_path_is_refused_because_the_verify_scripts_run_it(self):
        with tempfile.TemporaryDirectory() as folder:
            shims = Path(folder) / 'shims'
            shims.mkdir()
            old = shims / 'python3'
            old.write_text('#!/bin/sh\necho 3 11 2\n')
            old.chmod(0o755)
            work = Path(folder) / 'work'
            work.mkdir()
            env = dict(os.environ, PATH=f'{shims}{os.pathsep}{os.environ["PATH"]}')
            result = subprocess.run([sys.executable, str(ROOT / 'coldverify'), '--compat', '--ci'], cwd=work, env=env,
                                    text=True, capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual(result.stdout, '')
            self.assertEqual(result.stderr, 'coldverify needs Python 3.12 or newer as python3 on PATH, which runs the '
                                            f'verify scripts; {old} is Python 3.11.2\n')
            self.assertEqual(list(work.iterdir()), [])


class Install(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.source = self.folder / 'source'
        shutil.copytree(ROOT, self.source, ignore=shutil.ignore_patterns('.git', '.worktrees', '.parity', '.cold',
                                                                         '__pycache__', 'build', 'dist', '*.egg-info'))
        self.commit = commit_all(self.source)
        self.venv = self.folder / 'venv'
        subprocess.run([sys.executable, '-m', 'venv', str(self.venv)], check=True, capture_output=True, timeout=120)
        self.env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}

    def run_in_venv(self, *args):
        result = subprocess.run([str(self.venv / 'bin/python'), '-m', *args], cwd=self.folder, env=self.env, text=True,
                                capture_output=True, timeout=600)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def assert_installed_with_the_commit(self):
        env = dict(self.env, GIT_CEILING_DIRECTORIES=str(self.folder))
        result = subprocess.run([str(self.venv / 'bin/coldverify'), '--version'], cwd=self.folder, env=env, text=True,
                                capture_output=True, timeout=60)
        self.assertEqual((result.returncode, result.stdout),
                         (0, f'coldverify {project()["version"]} ({self.commit})\n'), result.stderr)
        where = subprocess.run([str(self.venv / 'bin/python'), '-c', 'import coldverify.ci.console as c; print(c.__file__)'],
                               cwd=self.folder, text=True, capture_output=True, check=True).stdout.strip()
        package = Path(where).parents[1]
        for shipped in ('coldverify', 'ci/fleet-verify.sh', 'ci/linux-verify.py', 'ci/filter-model.json',
                        'skills/create-verification-skill/SKILL.md'):
            self.assertTrue((package / shipped).is_file(), shipped)

    def test_pip_install_from_a_checkout_gives_a_coldverify_command_with_the_engine_and_the_commit(self):
        self.run_in_venv('pip', 'install', '-q', str(self.source))
        self.assert_installed_with_the_commit()

    def test_a_wheel_built_from_the_sdist_keeps_the_commit(self):
        self.run_in_venv('pip', 'install', '-q', 'build')
        self.run_in_venv('build', '--outdir', str(self.folder / 'dist'), str(self.source))
        self.run_in_venv('pip', 'install', '-q', str(next((self.folder / 'dist').glob('*.whl'))))
        self.assert_installed_with_the_commit()


if __name__ == '__main__':
    unittest.main()
