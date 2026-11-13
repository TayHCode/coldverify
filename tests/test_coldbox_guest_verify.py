import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import criteria_shim

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ci'))
import synthetic_policy
import verify_criteria

GIT_LOCATION = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR',
                'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES')
COLDBOX_HOSTS = ('runner-a', 'ops-a', 'builder-b', 'builder-a')
REFUSE = '#!/usr/bin/env bash\necho "$0 $*" >> "$SHIM_CALLS"\nexit 255\n'
NO_REMOTE_GH = '#!/usr/bin/env bash\necho "$0 $*" >> "$SHIM_CALLS"\necho "no git remotes found" >&2\nexit 1\n'
GUEST = f'''import os, platform, sys
from pathlib import Path
platform.node = lambda: "cbx-guest"
sys.path.insert(0, {str(ROOT / 'ci')!r})
import verify_context
verify_context.COLDBOX_HOST_FILE = Path(os.environ["TEST_COLDBOX_HOST_FILE"])
''' + synthetic_policy.SHIM


@unittest.skipUnless(sys.platform.startswith('linux'), 'Coldbox guests are Linux')
class ColdboxGuestVerify(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        base = Path(folder.name)
        self.repo, self.calls, self.marker = base / 'coldfix', base / 'shim-calls', base / 'host'
        shims, home = base / 'shims', base / 'home'
        for directory in (self.repo, shims, home):
            directory.mkdir()
        self.marker.write_text('runner-a\n')
        (shims / 'sitecustomize.py').write_text(GUEST)
        for name, script in (('ssh', REFUSE), ('coldbox', REFUSE), ('gh', NO_REMOTE_GH)):
            (shims / name).write_text(script)
            (shims / name).chmod(0o755)
        self.env = {key: value for key, value in os.environ.items()
                    if key not in GIT_LOCATION and not key.startswith(('GITHUB_', 'COLD_VERIFY_', 'COLDBOX_'))
                    and key != 'PYTHONPATH'}
        self.env.update(criteria_shim.install(shims, base / 'criteria-calls'))
        self.env.update(PATH=f'{shims}:{self.env["PATH"]}', PYTHONPATH=str(shims), HOME=str(home), SHIM_CALLS=str(self.calls),
                        TEST_COLDBOX_HOST_FILE=str(self.marker), GITHUB_REPOSITORY='example-org/native-tool',
                        GIT_AUTHOR_NAME='Guest', GIT_AUTHOR_EMAIL='guest@localhost',
                        GIT_COMMITTER_NAME='Guest', GIT_COMMITTER_EMAIL='guest@localhost')
        self.write('calc.py', 'def add(a, b):\n    return a - b\n')
        self.write('tests/test_other.py', 'import unittest\nclass Other(unittest.TestCase):\n'
                   '    def test_truth(self):\n        self.assertTrue(True)\n')
        self.write('.fleet/verify.sh', 'set -euo pipefail\npython3 "$COLD_VERIFY_UNITTEST" discover -s tests -v\n')
        self.git('init', '-q', '-b', 'main')
        self.git('add', '-A')
        self.git('commit', '-qm', 'base')
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.git('switch', '-qc', 'fix/add')
        self.write('calc.py', 'def add(a, b):\n    return a + b\n')
        self.write('tests/test_calc.py', 'import sys, unittest\nsys.path.insert(0, ".")\nimport calc\n'
                   'class Add(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(calc.add(2, 3), 5)\n')
        self.git('add', '-A')
        self.git('commit', '-qm', 'Fix add')

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def git(self, *args):
        subprocess.run(['git', '-C', str(self.repo), '-c', 'commit.gpgsign=false', *args], check=True, env=self.env)

    def coldverify(self, *args):
        result = subprocess.run([sys.executable, str(ROOT / 'coldverify'), *args], cwd=self.repo, env=self.env,
                                text=True, capture_output=True, timeout=300)
        calls = self.calls.read_text() if self.calls.exists() else ''
        return result.returncode, result.stdout + result.stderr, calls

    def as_guest_of(self, host):
        self.marker.write_text(host + '\n')
        self.calls.unlink(missing_ok=True)

    def test_cold_verify_in_a_guest_of_every_listed_host_runs_without_ssh(self):
        for host in COLDBOX_HOSTS:
            with self.subTest(host=host):
                self.as_guest_of(host)
                code, output, calls = self.coldverify('--compat', '--ci')
                self.assertNotIn('ssh', calls)
                self.assertEqual(code, 0, output)
                self.assertIn('Repro gate: PASS', output)
                self.assertIn(f'PASS: real Linux verification on {host}', output)

    def test_strict_coldverify_in_a_guest_of_every_listed_host_passes_all_but_criteria_without_ssh(self):
        for host in COLDBOX_HOSTS:
            with self.subTest(host=host):
                self.as_guest_of(host)
                code, output, calls = self.coldverify('--ci', '--builder-model', criteria_shim.BUILDER, '--claim', 'add adds')
                self.assertNotIn('ssh', calls)
                self.assertEqual(code, 0, output)
                receipt = json.loads((self.repo / '.cold/verify-receipt.json').read_text())
                checks = receipt['checks']
                self.assertEqual(checks.pop('criteria'), ['INCOMPLETE', verify_criteria.GUEST])
                self.assertEqual({name: result for name, (result, _) in checks.items()},
                                 {name: 'PASS' for name in checks}, output)
                self.assertEqual(receipt['result'], 'PASS')
                self.assertIn('criteria not evaluated in a CI guest', receipt['reason'])
                self.assertRegex(output, r'COLDVERIFY PASS: .*criteria not evaluated in a CI guest')
                self.assertIn(f'| criteria | INCOMPLETE | {verify_criteria.GUEST} |', output)
                evidence = json.loads((self.repo / '.cold/verify-evidence.json').read_text())
                self.assertEqual(evidence['outcome'], 'incomplete')
                self.assertIn('criteria not evaluated in a CI guest', evidence['failure']['detail'])

    def test_strict_coldverify_in_a_guest_without_a_repro_names_the_guest_the_suite_ran_in(self):
        self.write('notes.txt', 'uncommitted, so the repro stops before it picks a backend\n')
        code, output, calls = self.coldverify('--ci', '--builder-model', criteria_shim.BUILDER, '--claim', 'add adds')
        self.assertNotIn('ssh', calls)
        receipt = json.loads((self.repo / '.cold/verify-receipt.json').read_text())
        self.assertIsNone(receipt['repro'].get('backend'), receipt['repro'])
        self.assertEqual((receipt['environment']['backend'], receipt['environment']['isolation']), ('local', 'coldbox-guest'))
        self.assertEqual(receipt['run']['backend'], 'local', output)

    def test_strict_coldverify_without_the_guest_marker_still_needs_criteria(self):
        self.marker.unlink()
        self.env.update(COLD_VERIFY_BACKEND='local', COLDBOX_GUEST='1')
        code, output, _ = self.coldverify('--ci', '--claim', 'add adds')
        self.assertEqual(code, 2, output)
        receipt = json.loads((self.repo / '.cold/verify-receipt.json').read_text())
        self.assertEqual(receipt['result'], 'INCOMPLETE')
        self.assertEqual(receipt['checks']['criteria'][0], 'INCOMPLETE')
        self.assertNotIn('criteria not evaluated in a CI guest', output)

    def test_without_the_guest_marker_the_same_run_asks_for_a_coldbox_vm_not_runner_01(self):
        self.marker.unlink()
        code, output, calls = self.coldverify('--compat', '--ci')
        self.assertIn('coldbox vm up', calls)
        self.assertNotIn('runner-a', calls)
        self.assertNotEqual(code, 0, output)


if __name__ == '__main__':
    unittest.main()
