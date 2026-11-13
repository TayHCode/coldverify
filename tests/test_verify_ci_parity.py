import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_fleet_verify_workspace import SSH_SHIM, git

import repo_checkout
import synthetic_policy

ROOT = repo_checkout.root()
LOGGING_SSH_SHIM = SSH_SHIM.replace('command=$2\n', 'command=$2\nprintf \'%s\\n\' "$command" >> "$FAKE_LOG"\n')

GIT_DEPENDENT_TEST = """import subprocess
import unittest


class OwnCheckout(unittest.TestCase):
    def test_the_suite_sees_its_own_commit_and_tracked_files(self):
        subprocess.run(['git', 'rev-parse', '--verify', 'HEAD'], check=True, capture_output=True)
        subprocess.run(['git', 'ls-files', '--error-unmatch', 'test_own_checkout.py'], check=True, capture_output=True)
"""


def write_fixture(source):
    source.mkdir()
    git(source, 'init', '-q')
    (source / '.gitignore').write_text('ignored.log\n.env\n')
    (source / '.fleet').mkdir()
    (source / '.fleet/verify.ps1').write_text('exit 0\n')
    (source / '.fleet/verify.sh').write_text('#!/usr/bin/env bash\nset -euo pipefail\npython3 -m unittest discover -s tests\n')
    (source / 'tests').mkdir()
    (source / 'tests/test_pass.py').write_text('import unittest\n\n\nclass Pass(unittest.TestCase):\n    def test_pass(self):\n        pass\n')
    (source / 'README.md').write_text('fixture\n')
    (source / 'docs').mkdir()
    (source / 'docs/.gitignore').write_text('*.tmp\n')
    (source / 'docs/guide.md').write_text('guide\n')
    git(source, 'add', '.')
    assert git(source, 'commit', '-qm', 'fixture').returncode == 0
    for ignored in ('ignored.log', '.env', 'docs/scratch.tmp'):
        (source / ignored).write_text('skip\n')


def archive(source, destination):
    destination.mkdir()
    packed = subprocess.run(['git', '-C', str(source), 'archive', 'HEAD'], capture_output=True, check=True)
    subprocess.run(['tar', '-x', '-C', str(destination)], input=packed.stdout, check=True)


def plain_copy(source, destination):
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns('.git'))


def worktree(source, destination):
    assert git(source, 'worktree', 'add', '-q', '-b', 'agent', str(destination)).returncode == 0


@unittest.skipUnless(os.name == 'posix', 'drives native snapshot export and the actual repository recipe')
class VerifyCiParity(unittest.TestCase):
    def verify(self, base, workspace, identity=None):
        destination = base / f'snapshot-{workspace.name}'
        env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        env['GIT_CEILING_DIRECTORIES'] = str(base)
        exported = subprocess.run(['python3', str(ROOT / 'ci/linux-verify.py'), '--export', str(destination), str(workspace)],
                                  env=env, text=True, capture_output=True, check=False)
        if exported.returncode:
            return {'code': exported.returncode}, exported.stderr
        files = {str(path.relative_to(destination)): path.read_text()
                 for path in sorted(destination.rglob('*'))
                 if '.git' not in path.relative_to(destination).parts and path.is_file()}
        index = git(destination, 'ls-files').stdout.splitlines()
        result = subprocess.run(['bash', '.fleet/verify.sh'], cwd=destination, env=env, text=True, capture_output=True, check=False)
        return {'code': result.returncode, 'files': files, 'index': sorted(index), 'stdout': result.stdout}, result.stderr

    def test_copied_workspaces_resolve_the_same_steps_and_results_as_the_repository(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            write_fixture(base / 'repo')
            archive(base / 'repo', base / 'archive')
            worktree(base / 'repo', base / 'agent')
            plain_copy(base / 'repo', base / 'plain')
            expected, stderr = self.verify(base, base / 'repo')
            self.assertEqual(expected['code'], 0, stderr)
            self.assertIn('.fleet/verify.sh', expected['files'])
            self.assertEqual(expected['index'], sorted(expected['files']))
            for name in ('archive', 'plain', 'agent'):
                with self.subTest(workspace=name):
                    actual, stderr = self.verify(base, base / name, None if name == 'agent' else 'example-org/not-enrolled')
                    self.assertEqual(actual['code'], 0, stderr)
                    self.assertEqual(actual['files'], expected['files'])
                    self.assertEqual(actual['index'], expected['index'])
                    self.assertTrue(actual['index'])
                    self.assertFalse({'.env', 'ignored.log', 'docs/scratch.tmp'} & set(actual['files']))
                    self.assertEqual(actual['stdout'], expected['stdout'])

    def test_copy_without_repository_identity_never_picks_a_platform(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            write_fixture(base / 'repo')
            plain_copy(base / 'repo', base / 'plain')
            env = {key: value for key, value in os.environ.items() if key != 'GITHUB_REPOSITORY'}
            env.update(GIT_CEILING_DIRECTORIES=str(base), PYTHONPATH=synthetic_policy.shim_folder(self.addCleanup))
            missing = subprocess.run(['python3', str(ROOT / 'ci/linux-verify.py'), '--platform', str(base / 'plain')],
                                     env=env, text=True, capture_output=True, check=False)
            self.assertEqual(missing.returncode, 2)
            self.assertIn('set GITHUB_REPOSITORY', missing.stdout + missing.stderr)
            for identity, platform in (('example-org/native-tool', 'linux'), ('example-org/native-cli', 'linux'), ('example-org/not-enrolled', 'linux-generic')):
                env['GITHUB_REPOSITORY'] = identity
                result = subprocess.run(['python3', str(ROOT / 'ci/linux-verify.py'), '--platform', str(base / 'plain')],
                                        env=env, text=True, capture_output=True, check=False)
                self.assertEqual(result.stdout.strip(), platform, result.stderr)

    def test_git_dependent_suite_passes_in_an_exported_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            archive(ROOT, base / 'archive')
            (base / 'archive/tests/test_own_checkout.py').write_text(GIT_DEPENDENT_TEST)
            subprocess.run(['python3', str(ROOT / 'ci/linux-verify.py'), '--export', str(base / 'export'), str(base / 'archive')],
                           env={**os.environ, 'GIT_CEILING_DIRECTORIES': str(base)}, check=True)
            self.assertEqual(git(base / 'export', 'rev-parse', '--verify', 'HEAD').returncode, 0)
            env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
            result = subprocess.run(['python3', '-m', 'unittest', '-v',
                                     'test_own_checkout.OwnCheckout.test_the_suite_sees_its_own_commit_and_tracked_files'],
                                    cwd=base / 'export/tests', env=env, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Ran 1 test', result.stderr)

    def test_export_into_a_subfolder_of_the_source_skips_the_destination(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            write_fixture(base / 'repo')
            result = subprocess.run(['python3', str(ROOT / 'ci/linux-verify.py'), '--export', str(base / 'repo/export'), str(base / 'repo')],
                                    env={**os.environ, 'GIT_CEILING_DIRECTORIES': str(base)}, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            index = git(base / 'repo/export', 'ls-files').stdout.splitlines()
            self.assertIn('.fleet/verify.sh', index)
            self.assertFalse([path for path in index if path.startswith('export')])

    def test_non_git_copy_is_snapshotted_before_the_macos_verifier(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            write_fixture(base / 'repo')
            plain_copy(base / 'repo', base / 'plain')
            shutil.copytree(ROOT / 'ci', base / 'ci')
            (base / 'ci/mac-verify.py').write_text(
                'import subprocess, sys\n'
                'for args in (["rev-parse", "--verify", "HEAD"], ["status", "--porcelain"], ["ls-files"]):\n'
                '    print(*args, subprocess.run(["git", "-C", sys.argv[1], *args], capture_output=True, text=True, check=True).stdout.split())\n')
            env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
            env.update(GITHUB_REPOSITORY='example-org/mac-app', GIT_CEILING_DIRECTORIES=str(base),
                       PYTHONPATH=synthetic_policy.shim_folder(self.addCleanup))
            result = subprocess.run(['bash', str(base / 'ci/fleet-verify.sh'), str(base / 'plain')],
                                    env=env, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('status --porcelain []', result.stdout)
            self.assertIn('.fleet/verify.sh', result.stdout)
            self.assertNotIn('.env', result.stdout)


if __name__ == '__main__':
    unittest.main()
