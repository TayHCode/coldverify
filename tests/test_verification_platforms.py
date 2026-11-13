import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ci'))
import synthetic_policy
import verify_policy


class NativeVerification(unittest.TestCase):
    def setUp(self):
        synthetic_policy.use(self.addCleanup)

    def test_portability_repository_uses_the_runner_without_a_linux_exception(self):
        repo = 'example-org/portability-checks'
        self.assertTrue(verify_policy.linux_runner_repository(repo))
        self.assertFalse(verify_policy.linux_repository(repo))

    def test_only_the_policy_owner_repositories_use_the_runner(self):
        self.assertTrue(verify_policy.linux_runner_repository('Example-Org/unlisted'))
        for repo in ('other-org/portability-checks', 'example-org-two/tool', 'example-org', 'example-org/a/b'):
            with self.subTest(repo=repo):
                self.assertFalse(verify_policy.linux_runner_repository(repo))

    def test_macos_repository_never_routes_to_linux(self):
        repo = 'example-org/mac-app'
        self.assertTrue(verify_policy.macos_repository(repo))
        self.assertFalse(verify_policy.linux_repository(repo))
        self.assertFalse(verify_policy.linux_runner_repository(repo))

    def test_listed_repositories_route_to_native_linux_and_unlisted_ones_run_generic_linux(self):
        env = {key: value for key, value in os.environ.items() if key != 'GITHUB_REPOSITORY' and not key.startswith('GIT_')}
        env['PYTHONPATH'] = synthetic_policy.shim_folder(self.addCleanup)
        for repo, platform in [('example-org/native-tool', 'linux'), ('example-org/native-cli', 'linux'),
                               ('example-org/not-enrolled', 'linux-generic')]:
            with self.subTest(repo=repo), tempfile.TemporaryDirectory() as folder:
                subprocess.run(['git', 'init', '-q', folder], check=True)
                subprocess.run(['git', '-C', folder, 'remote', 'add', 'origin', f'https://github.com/{repo}.git'], check=True)
                result = subprocess.run([sys.executable, str(ROOT / 'ci/linux-verify.py'), '--platform', folder],
                                        check=False, capture_output=True, text=True, env=env)
                self.assertEqual(result.stdout.strip(), platform, result.stderr)

    def test_missing_policy_grants_no_exception_and_no_native_host(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(verify_policy, 'PLATFORMS', Path(directory) / 'policy.json'):
            try:
                policy = verify_policy.verification_policy()
            except Exception as error:
                self.fail(f'an install without the policy file must still run: {error!r}')
            self.assertEqual(policy, {'linux_hosts': [], 'repositories': {}, 'macos_hosts': [],
                                      'macos_repositories': {}, 'auxiliary_linux_repositories': {}})
            for repo in ('example-org/factory', 'alice/tool'):
                self.assertFalse(verify_policy.linux_repository(repo))
                self.assertFalse(verify_policy.macos_repository(repo))
                self.assertFalse(verify_policy.linux_runner_repository(repo))

    def test_invalid_policy_cannot_open_a_gate(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(verify_policy, 'PLATFORMS', Path(directory) / 'policy.json'):
            verify_policy.PLATFORMS.write_text(json.dumps({'linux_hosts': [], 'repositories': {}}))
            with self.assertRaises(ValueError):
                verify_policy.verification_policy()
            policy = json.loads(synthetic_policy.POLICY.read_text())
            for host in ('-oProxyCommand=x', '', 7):
                with self.subTest(coldbox_host=host):
                    verify_policy.PLATFORMS.write_text(json.dumps({**policy, 'coldbox_host': host}))
                    with self.assertRaises(ValueError):
                        verify_policy.verification_policy()
            for owner in ('', '-org', 'org/name', 'org name', 7, None):
                with self.subTest(owner=owner):
                    verify_policy.PLATFORMS.write_text(json.dumps({**policy, 'owner': owner}))
                    with self.assertRaises(ValueError):
                        verify_policy.verification_policy()


if __name__ == '__main__':
    unittest.main()
