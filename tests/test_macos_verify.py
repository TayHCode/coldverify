import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ci'))
import synthetic_policy
spec = importlib.util.spec_from_file_location('mac_verify', ROOT / 'ci/mac-verify.py')
mac = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mac)


@unittest.skipUnless(sys.platform.startswith('linux'), 'isolated shell regression; native suite is proved on mac-a')
class LocalMacIsolation(unittest.TestCase):
    def setUp(self):
        synthetic_policy.use(self.addCleanup)

    def test_local_route_runs_the_same_isolated_helper_and_preserves_ignored_work(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source'
            subprocess.run(['git', 'init', '-q', str(source)], check=True)
            subprocess.run(['git', '-C', str(source), 'remote', 'add', 'origin',
                            'https://github.com/example-org/mac-app.git'], check=True)
            (source / '.gitignore').write_text('node_modules/\n')
            (source / '.fleet').mkdir()
            (source / '.fleet/verify.sh').write_text('set -eu\ntest -z "${GH_TOKEN:-}"\nmkdir -p node_modules\necho changed > node_modules/local-edit\necho retained > "$XDG_STATE_HOME/suite.log"\n')
            subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(source), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
                            '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture'], check=True)
            (source / 'node_modules').mkdir()
            (source / 'node_modules/local-edit').write_text('preserve')
            # This exercises isolation with a shell fixture; it is not native Swift evidence.
            helper = "import platform; platform.system=lambda: 'Darwin'; platform.node=lambda: 'mac-a'\n" + mac.REMOTE
            with patch.object(mac.platform, 'system', return_value='Darwin'), patch.object(mac.platform, 'node', return_value='mac-a'), \
                 patch.object(mac.Path, 'home', return_value=root / 'home'), patch.object(mac, 'REMOTE', helper), \
                 patch.dict(os.environ, GITHUB_REPOSITORY='example-org/mac-app', GH_TOKEN='synthetic-test-credential'):
                receipt = mac.verify(source)
            self.assertEqual((source / 'node_modules/local-edit').read_text(), 'preserve')
            self.assertEqual(receipt['exit_code'], 0)
            self.assertRegex(receipt['snapshot_tree'], r'^[0-9a-f]{64}$')
            self.assertTrue(receipt['clean'])
            exported = Path(receipt['evidence']) / 'checkout'
            self.assertFalse(exported.exists())
            self.assertFalse((exported.parent / 'source.bundle').exists())
            self.assertTrue((exported.parent / 'receipt.json').is_file())
            self.assertEqual((exported.parent / 'state/suite.log').read_text(), 'retained\n')
