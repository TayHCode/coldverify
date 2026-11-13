import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ci'))
import verify_repro as repro
import verify_repro_transport as transport


class StaleEventBase(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.folder = Path(scratch.name)
        self.root = self.folder / 'source'
        self.root.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@localhost')
        (self.root / '.gitignore').write_text('.cold/\n')
        (self.root / 'value.py').write_text('VALUE = 0\n')
        self.commit('base')
        self.base = self.git('rev-parse', 'HEAD')
        self.git('switch', '-qc', 'fix/value')
        (self.root / 'value.py').write_text('VALUE = 1\n')
        (self.root / 'test_value.py').write_text(
            'import unittest\nfrom value import VALUE\nclass Check(unittest.TestCase):\n'
            ' def test_value(self):\n  self.assertEqual(VALUE, 1)\n')
        self.commit('fix value')
        self.head = self.git('rev-parse', 'HEAD')
        self.git('switch', '-q', 'main')
        (self.root / 'unrelated').write_text('new base')
        self.commit('advance base')
        self.new_base = self.git('rev-parse', 'HEAD')
        self.git('remote', 'add', 'origin', str(self.root))
        self.git('update-ref', 'refs/remotes/origin/main', self.new_base)
        self.git('checkout', '-q', '--detach')
        self.git('merge', '--no-ff', '-qm', 'merge PR', self.head)
        self.checkout = self.git('rev-parse', 'HEAD')
        self.event = self.folder / 'event.json'
        self.write_event()
        env = patch.dict(os.environ, GITHUB_EVENT_NAME='pull_request',
                         GITHUB_WORKSPACE=str(self.root), GITHUB_EVENT_PATH=str(self.event),
                         GITHUB_HEAD_REF='fix/value', GITHUB_BASE_REF='main')
        env.start()
        self.addCleanup(env.stop)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args],
                                       text=True, stderr=subprocess.DEVNULL).strip()

    def commit(self, message):
        self.git('add', '.')
        self.git('commit', '-qm', message)

    def write_event(self, base=None, head=None):
        self.event.write_text(json.dumps({'pull_request': {'title': 'fix value',
            'base': {'sha': base or self.base, 'ref': 'main'},
            'head': {'sha': head or self.head}}}))

    def assert_rejected(self):
        with self.assertRaises((RuntimeError, subprocess.CalledProcessError)):
            transport.prepare(self.root, False, None)
        self.assertEqual(repro.gate(self.root), 1)
        report = json.loads((self.root / '.cold/verify-repro.json').read_text())
        self.assertEqual(report['result'], 'FAIL')
        self.assertEqual(report['tests'], [])

    def test_prepare_uses_authenticated_merge_first_parent(self):
        request = transport.prepare(self.root, False, None)
        self.assertEqual(request['details'][2:], [self.new_base, self.head])
        self.assertEqual(request['checkout'], self.checkout)

    def test_gate_proves_actual_head_against_merge_first_parent(self):
        self.assertEqual(repro.gate(self.root), 0)
        report = json.loads((self.root / '.cold/verify-repro.json').read_text())
        self.assertEqual(report['base'], self.new_base)
        self.assertEqual(report['head'], self.head)
        outputs = [{key: test.pop(key) for key in ('base_output', 'head_output', 'commands')} for test in report['tests']]
        self.assertEqual(report['tests'], [{'name': 'test_value.py::Check.test_value',
            'base_exit': 1, 'head_exit': 0, 'kind': 'proof'}])
        self.assertIn('AssertionError', outputs[0]['base_output'])
        self.assertIn('OK', outputs[0]['head_output'])
        self.assertEqual(len(outputs[0]['commands']), 2)

    def test_wrong_head_is_rejected_even_when_it_is_an_ancestor(self):
        self.write_event(head=self.base)
        self.assert_rejected()

    def test_event_base_revision_expression_is_rejected(self):
        self.write_event(base="HEAD^")
        self.assert_rejected()

    def test_event_base_off_first_parent_history_is_rejected(self):
        self.write_event(base=self.head)
        self.assert_rejected()

    def test_first_parent_off_named_base_branch_is_rejected(self):
        self.git('update-ref', 'refs/heads/main', self.base)
        self.git('update-ref', 'refs/remotes/origin/main', self.base)
        self.assert_rejected()

    def test_unavailable_base_branch_fails_closed(self):
        self.git('update-ref', '-d', 'refs/heads/main')
        self.git('update-ref', '-d', 'refs/remotes/origin/main')
        self.assert_rejected()

    def test_full_checkout_needs_no_network_credentials(self):
        self.git('remote', 'set-url', 'origin', str(self.folder / 'unavailable'))
        request = transport.prepare(self.root, False, None)
        self.assertEqual(request['details'][2:], [self.new_base, self.head])
        self.assertEqual(repro.gate(self.root, details=request['details']), 0)

    def test_base_branch_may_advance_again_after_merge_checkout(self):
        tip = self.git('commit-tree', self.git('rev-parse', 'HEAD^{tree}'),
                       '-p', self.new_base, '-m', 'later base')
        self.git('update-ref', 'refs/remotes/origin/main', tip)
        request = transport.prepare(self.root, False, None)
        self.assertEqual(request['details'][2:], [self.new_base, self.head])

    def test_three_parent_checkout_is_rejected(self):
        merge = self.git('commit-tree', self.git('rev-parse', 'HEAD^{tree}'),
                         '-p', self.new_base, '-p', self.head, '-p', self.base, '-m', 'extra parent')
        self.git('checkout', '-q', '--detach', merge)
        self.assert_rejected()

    def test_missing_base_branch_name_fails_closed(self):
        with patch.dict(os.environ, GITHUB_BASE_REF=''):
            self.assert_rejected()

    def test_non_pr_context_cannot_accept_a_merge_checkout(self):
        with patch.dict(os.environ, GITHUB_EVENT_NAME='push'), \
             patch.object(repro, 'metadata', return_value=('fix value', '', self.base, self.head)):
            self.assert_rejected()

    def test_shallow_merge_checkout_fetches_history_before_authentication(self):
        clone = self.folder / 'shallow'
        subprocess.run(['git', 'clone', '-q', '--depth=1', self.root.as_uri(), str(clone)], check=True)
        with patch.dict(os.environ, GITHUB_WORKSPACE=str(clone)):
            request = transport.prepare(clone, False, None)
        self.assertEqual(request['details'][2:], [self.new_base, self.head])
        self.assertEqual(repro.git(clone, 'rev-parse', '--is-shallow-repository').strip(), b'false')

    def test_prepared_details_work_without_event_payload_on_native_host(self):
        request = transport.prepare(self.root, False, None)
        with patch.dict(os.environ, GITHUB_EVENT_PATH=''):
            self.assertEqual(repro.gate(self.root, details=request['details']), 0)
        report = json.loads((self.root / '.cold/verify-repro.json').read_text())
        self.assertEqual(report['base'], self.new_base)
        self.assertEqual(report['head'], self.head)
