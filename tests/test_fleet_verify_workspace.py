import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SSH_SHIM = r'''#!/usr/bin/env bash
set -eu
command=$2
local_path() { printf '%s' "$FAKE_SCRATCH/$(printf '%s' "${1#C:\\actions-runners\\_scratch\\}" | tr '\\' '/')"; }
case $command in
  "mkdir "*) mkdir -p "$(local_path "${command#mkdir }")" ;;
  "tar -xzf - -C "*) tar -xzf - -C "$(local_path "${command#tar -xzf - -C }")" ;;
  "rmdir /s /q "*) : ;;
  powershell*) : ;;
  *) echo "unexpected remote command: $command" >&2; exit 64 ;;
esac
'''


def git(directory, *args):
    return subprocess.run(['git', '-C', str(directory), '-c', 'user.name=Fixture', '-c',
                           'user.email=fixture@localhost', '-c', 'commit.gpgsign=false', *args],
                          text=True, capture_output=True)


def run_coldverify(*args, **kwargs):
    env = {**(kwargs.pop('env', None) or os.environ), 'COLD_VERIFY_HOST': 'runner-a'}
    return subprocess.run([sys.executable, str(ROOT / 'coldverify'), *args], env=env, **kwargs)


@unittest.skipUnless(os.name == 'posix', 'drives the native snapshot export')
class VerifyWorkspace(unittest.TestCase):
    def test_staged_workspace_is_a_git_checkout_of_the_source_commit_with_local_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source, scratch, bin_dir = base / 'source', base / 'scratch', base / 'bin'
            for directory in (source, scratch, bin_dir):
                directory.mkdir()
            git(source, 'init', '-q')
            (source / '.gitignore').write_text('ignored.log\n')
            (source / 'tracked.txt').write_text('committed\n')
            (source / '.fleet').mkdir()
            (source / '.fleet/verify.ps1').write_text('exit 0\n')
            self.assertEqual(git(source, 'add', '.').returncode, 0)
            self.assertEqual(git(source, 'commit', '-qm', 'fixture').returncode, 0)
            (source / 'tracked.txt').write_text('edited\n')
            (source / 'untracked.txt').write_text('new\n')
            (source / 'ignored.log').write_text('skip\n')
            shim = bin_dir / 'ssh'
            shim.write_text(SSH_SHIM)
            shim.chmod(0o755)
            env = {key: value for key, value in os.environ.items() if key != 'GITHUB_REPOSITORY'}
            env.update(PATH=f'{bin_dir}:{env["PATH"]}', FAKE_SCRATCH=str(scratch))

            result = subprocess.run([sys.executable, str(ROOT / 'ci/linux-verify.py'), '--export', str(scratch / 'source-export'), str(source)],
                                    env=env, text=True, capture_output=True)

            self.assertEqual(result.returncode, 0, result.stderr)
            staged = [path for path in scratch.iterdir() if path.name.startswith('source-')]
            self.assertEqual(len(staged), 1)
            workspace = staged[0]
            head = git(source, 'rev-parse', 'HEAD').stdout
            self.assertEqual(git(workspace, 'rev-parse', 'HEAD').stdout, head)
            common = git(workspace, 'rev-parse', '--path-format=absolute', '--git-common-dir')
            self.assertEqual(common.returncode, 0, common.stderr)
            self.assertEqual(Path(common.stdout.strip()).resolve(), (workspace / '.git').resolve())
            self.assertEqual((workspace / 'tracked.txt').read_text(), 'edited\n')
            self.assertEqual((workspace / 'untracked.txt').read_text(), 'new\n')
            self.assertFalse((workspace / 'ignored.log').exists())


if __name__ == '__main__':
    unittest.main()
