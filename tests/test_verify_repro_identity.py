import json
import sys
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ci'))
import verify_repro
import verify_repro_transport


class IdentityDiagnostics(unittest.TestCase):
    def test_reports_mismatching_base_and_checkout_parents(self):
        with patch.object(verify_repro, 'pr_event_applies', return_value=True):
            message = verify_repro.identity_error(Path.cwd(), 'a' * 40, 'b' * 40, 'c' * 40, ['d' * 40, 'b' * 40])
        self.assertEqual(json.loads(message.split(': ', 1)[1]), {
            'event_applies': True, 'base': 'a' * 40, 'head': 'b' * 40,
            'checkout': 'c' * 40, 'parents': ['d' * 40, 'b' * 40]})

    def test_invalid_event_values_never_reach_output(self):
        with patch.object(verify_repro, 'pr_event_applies', return_value=False):
            message = verify_repro.identity_error(Path.cwd(), 'private-payload', {'secret': 'value'}, 'c' * 40, ['invalid-parent'])
        self.assertEqual(json.loads(message.split(': ', 1)[1]), {
            'event_applies': False, 'base': '<invalid>', 'head': '<invalid>',
            'checkout': 'c' * 40, 'parents': ['<invalid>']})

    def test_guard_rejects_stale_base_with_exact_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.check_output(['git', '-C', directory, *args], text=True).strip()
            git('init', '-q', '-b', 'main')
            git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost', 'commit', '--allow-empty', '-qm', 'base')
            base = git('rev-parse', 'HEAD')
            git('switch', '-qc', 'fix/example')
            git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost', 'commit', '--allow-empty', '-qm', 'head')
            head = git('rev-parse', 'HEAD')
            tree = git('rev-parse', 'HEAD^{tree}')
            env = dict(os.environ, GIT_AUTHOR_NAME='Fixture', GIT_AUTHOR_EMAIL='fixture@localhost', GIT_COMMITTER_NAME='Fixture', GIT_COMMITTER_EMAIL='fixture@localhost')
            merge = subprocess.check_output(['git', '-C', directory, 'commit-tree', tree, '-p', base, '-p', head, '-m', 'merge'], env=env, text=True).strip()
            git('checkout', '-q', '--detach', merge)
            event = root.parent / (root.name + '-event.json')
            try:
                event.write_text(json.dumps({'pull_request': {'title': 'fix fixture', 'base': {'sha': 'a' * 40}, 'head': {'sha': head}}}))
                with patch.dict(os.environ, GITHUB_EVENT_NAME='pull_request', GITHUB_WORKSPACE=directory, GITHUB_EVENT_PATH=str(event)):
                    with self.assertRaises(RuntimeError) as raised:
                        verify_repro_transport.prepare(root, False, None)
                self.assertEqual(json.loads(str(raised.exception).split(': ', 1)[1]), {
                    'event_applies': True, 'base': 'a' * 40, 'head': head,
                    'checkout': merge, 'parents': [base, head]})
            finally:
                event.unlink(missing_ok=True)
