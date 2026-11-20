import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_verify_receipt import ROOT, schema_errors


UNREACHABLE = '#!/usr/bin/env bash\necho "$SSH_ERROR" >&2\nexit 255\n'


@unittest.skipUnless(os.name == 'posix', 'drives the native snapshot export')
class ReproErrorsReachTheReceipt(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        base = Path(folder.name)
        self.repo, shims, home = base / 'sample', base / 'shims', base / 'home'
        for directory in (self.repo, shims, home):
            directory.mkdir()
        (shims / 'sitecustomize.py').write_text('import platform\nplatform.node = lambda: "outsider-box"\n')
        for name, script in (('ssh', UNREACHABLE), ('gh', '#!/usr/bin/env bash\nexit 1\n')):
            (shims / name).write_text(script)
            (shims / name).chmod(0o755)
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(('GITHUB_', 'COLD_', 'FAKE_', 'GIT_')) and key != 'PYTHONPATH'}
        no_coldbox = [entry for entry in self.env['PATH'].split(os.pathsep) if not (Path(entry) / 'coldbox').exists()]
        self.env.update(PATH=os.pathsep.join([str(shims), *no_coldbox]), PYTHONPATH=str(shims), HOME=str(home), COLDBOX_GUEST='0',
                        SSH_ERROR='ssh: Could not resolve hostname runner-a: Name or service not known',
                        GIT_AUTHOR_NAME='Fixture', GIT_AUTHOR_EMAIL='fixture@localhost',
                        GIT_COMMITTER_NAME='Fixture', GIT_COMMITTER_EMAIL='fixture@localhost')
        self.git('init', '-q', '-b', 'main')
        (self.repo / 'calc.py').write_text('def add(a, b):\n    return a - b\n')
        self.git('add', '-A')
        self.git('commit', '-qm', 'base')
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.git('switch', '-qc', 'fix/add')
        (self.repo / 'calc.py').write_text('def add(a, b):\n    return a + b\n')
        self.git('commit', '-qam', 'Fix add')

    def git(self, *args):
        subprocess.run(['git', '-C', str(self.repo), '-c', 'commit.gpgsign=false', *args], check=True, env=self.env)

    def coldverify(self, **env):
        result = subprocess.run([sys.executable, str(ROOT / 'coldverify'), '--ci', '--claim', 'add adds'], cwd=self.repo,
                                env={**self.env, **env}, text=True, capture_output=True, timeout=300)
        receipt = json.loads((self.repo / '.cold/verify-receipt.json').read_text())
        self.assertEqual(schema_errors(receipt), [])
        return result, receipt['checks']['repro'], (self.repo / '.cold/verify-receipt.md').read_text()

    def test_an_unreachable_coldbox_host_is_incomplete_and_named_in_the_receipt(self):
        result, repro, markdown = self.coldverify(COLD_VERIFY_HOST='runner-a')
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(repro, ['INCOMPLETE', 'repro: the coldbox on runner-a backend gave no answer: '
                                               'ssh: Could not resolve hostname runner-a: Name or service not known'])
        self.assertIn('| repro | INCOMPLETE | repro: the coldbox on runner-a backend gave no answer: ssh: Could not resolve hostname',
                      markdown)

    def test_an_unreachable_runner_over_an_explicit_ssh_backend_is_incomplete_and_named(self):
        result, repro, _ = self.coldverify(COLD_VERIFY_BACKEND='ssh:runner-a')
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(repro, ['INCOMPLETE', 'repro: could not reach runner-a: '
                                               'ssh: Could not resolve hostname runner-a: Name or service not known'])

    def test_an_uncommitted_file_is_named_in_the_receipt(self):
        (self.repo / 'notes.txt').write_text('scratch\n')
        result, repro, _ = self.coldverify()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(repro, ['INCOMPLETE', 'repro: commit all changes before running the repro gate'])

    def test_the_ssh_text_is_bounded_and_secrets_are_redacted(self):
        self.git('branch', '-m', 'change/add')
        token = 'ghp_' + 'A' * 36
        noise = 'debug1: ' + 'x' * 2000
        result, repro, _ = self.coldverify(GITHUB_ACTIONS='true', COLD_VERIFY_HOST='runner-a',
                                           SSH_ERROR=f'{noise}\nfetch https://bot:' f'{token}@github.com failed\nPermission denied (publickey).')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines()[-1], 'COLDVERIFY CI FAIL: tests')
        self.assertNotIn(token, repro[1])
        self.assertNotIn(token, (self.repo / '.cold/verify-repro.json').read_text())
        self.assertIn('[redacted]@github.com', repro[1])
        self.assertTrue(repro[1].startswith('repro: the coldbox on runner-a backend gave no answer: debug1:'), repro[1])
        self.assertTrue(repro[1].endswith('Permission denied (publickey).'), repro[1])
        self.assertLess(len(repro[1]), 600)
