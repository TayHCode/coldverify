import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ci'))
import synthetic_policy
import verify_context

spec =importlib.util.spec_from_file_location('runner_dispatch_guard', ROOT / 'ci/linux-verify.py')
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


class RunnerDispatchGuard(unittest.TestCase):
    def setUp(self):
        synthetic_policy.use(self.addCleanup)
        for isolation in (patch.object(verify_context, 'COLDBOX_HOST_FILE', Path('/nonexistent/coldbox/host')),
                          patch.dict(os.environ, COLDBOX_GUEST='0')):
            isolation.start()
            self.addCleanup(isolation.stop)
        for name in ('COLD_VERIFY_BACKEND', 'COLD_VERIFY_HOST'):
            os.environ.pop(name, None)

    def test_cold_verify_host_only_moves_the_coldbox_vm_never_the_run_to_a_bare_host(self):
        policy = verify.verify_policy.verification_policy()
        policy['linux_hosts'] = ['control-a', 'builder-a', 'runner-a']
        for host in ('control-a', 'builder-a', 'builder-b', 'windows-a', 'runner-a'):
            with self.subTest(host=host), patch.object(sys, 'argv', ['linux-verify.py', str(ROOT)]), \
                 patch.dict(os.environ, GITHUB_REPOSITORY='example-org/native-cli', COLD_VERIFY_HOST=host), \
                 patch.object(verify.verify_policy, 'verification_policy', return_value=policy), \
                 patch.object(verify.platform, 'node', return_value=host), \
                 patch.object(verify, 'dispatch', return_value=0) as dispatch, patch.object(verify, 'verify') as local:
                self.assertEqual(verify.main(), 0)
            dispatch.assert_called_once_with(ROOT, 'coldbox', 'example-org/native-cli', False)
            local.assert_not_called()

    def test_the_default_destination_is_a_coldbox_vm_not_runner_01(self):
        with patch.object(sys, 'argv', ['linux-verify.py', str(ROOT)]), \
             patch.dict(os.environ, GITHUB_REPOSITORY='example-org/native-cli'), \
             patch.object(verify.platform, 'node', return_value='coordinator'), \
             patch.object(verify, 'dispatch', return_value=0) as dispatch, patch.object(verify, 'verify') as local:
            self.assertEqual(verify.main(), 0)
        dispatch.assert_called_once_with(ROOT, 'coldbox', 'example-org/native-cli', False)
        local.assert_not_called()
