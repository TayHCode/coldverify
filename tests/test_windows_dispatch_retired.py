import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class WindowsDispatchRetired(unittest.TestCase):
    def test_windows_recipe_stops_before_ssh_even_with_a_windows_override(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.run(['git', 'init', '-q', folder], check=True)
            (root / '.fleet').mkdir()
            (root / '.fleet/verify.ps1').write_text('exit 0\n')
            binary = root / 'bin'
            binary.mkdir()
            marker = root / 'ssh-called'
            (binary / 'ssh').write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 91\n')
            (binary / 'ssh').chmod(0o755)
            env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_') and k != 'GITHUB_REPOSITORY'}
            env.update(PATH=f'{binary}:{env["PATH"]}', FLEET_CI_HOST='windows-a')
            result = subprocess.run(['bash', str(ROOT / 'ci/fleet-verify.sh'), folder], env=env, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn('complete .fleet/verify.sh or .fleet/verify.py', result.stderr)
            self.assertIn('.fleet/verify.ps1', result.stderr)
            self.assertFalse(marker.exists(), result.stderr)

