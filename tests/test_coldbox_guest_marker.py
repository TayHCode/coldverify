import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ci'))
import synthetic_policy
import verify_context


class ColdboxGuestMarker(unittest.TestCase):
    def setUp(self):
        synthetic_policy.use(self.addCleanup)

    def test_environment_can_turn_the_guest_marker_off_but_never_on(self):
        with tempfile.TemporaryDirectory() as folder:
            forged = Path(folder) / 'host'
            forged.write_text('runner-a\n')
            absent = Path(folder) / 'absent'
            with patch.object(verify_context, 'COLDBOX_HOST_FILE', absent), \
                 patch.dict(os.environ, COLDBOX_HOST_FILE=str(forged), COLDBOX_GUEST='1'):
                self.assertFalse(verify_context.coldbox_guest())
            with patch.object(verify_context, 'COLDBOX_HOST_FILE', forged), patch.dict(os.environ, COLDBOX_GUEST='0'):
                self.assertFalse(verify_context.coldbox_guest())
            env = {key: value for key, value in os.environ.items() if key != 'COLDBOX_GUEST'}
            with patch.object(verify_context, 'COLDBOX_HOST_FILE', forged), patch.dict(os.environ, env, clear=True):
                self.assertTrue(verify_context.coldbox_guest())

    def test_marker_must_name_a_listed_linux_host(self):
        env = {key: value for key, value in os.environ.items() if key != 'COLDBOX_GUEST'}
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, env, clear=True), \
             patch.object(verify_context.platform, 'node', return_value='coldbox-job-7'):
            marker = Path(folder) / 'host'
            with patch.object(verify_context, 'COLDBOX_HOST_FILE', marker):
                for text, host in (('runner-a\n', 'runner-a'), ('ops-a\n', 'ops-a'), ('BUILDER-B\n', 'builder-b'),
                                   ('builder-a\n', 'builder-a'), ('control-a\n', None), ('\n', None)):
                    with self.subTest(marker=text):
                        marker.write_text(text)
                        self.assertEqual(verify_context.coldbox_guest(), host)
                        self.assertEqual(verify_context.host_name(), host or 'coldbox-job-7')
                marker.unlink()
                self.assertIsNone(verify_context.coldbox_guest())
                self.assertEqual(verify_context.host_name(), 'coldbox-job-7')


if __name__ == '__main__':
    unittest.main()
