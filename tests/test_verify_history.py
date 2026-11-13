import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ci'))


def git(root, *args):
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    return subprocess.run(['git', '-C', str(root), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost', *args],
                          check=True, capture_output=True, text=True, env=env).stdout.strip()


def proved(test):
    return test.get('kind') in ('proof', 'missing-on-base')


class Archive(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.repo = Path(folder.name) / 'repo'
        git(folder.name, 'init', '-q', str(self.repo))
        git(self.repo, 'commit', '-q', '--allow-empty', '-m', 'base')
        self.base = git(self.repo, 'rev-parse', 'HEAD')
        for name in ('app/login.py', 'tests/test_login.py', 'docs/login.md'):
            (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / name).write_text('x\n')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', 'head')
        self.head = git(self.repo, 'rev-parse', 'HEAD')

    def receipt(self, result, tests=(), flows=()):
        return {'commit': self.head, 'tree': 't' * 40, 'pull_request': 7, 'result': result,
                'repro': {'base': self.base, 'head': self.head, 'tests': list(tests)},
                'flows': {'flows': list(flows)} if flows else None}

    def test_a_strict_run_is_archived_in_the_clone_and_queryable_by_change_type_and_strategy(self):
        import verify_history
        proof = {'name': 'tests/test_login.py::Login.test_expired', 'kind': 'proof'}
        new = {'name': 'tests/test_login.py::Login.test_new', 'kind': 'missing-on-base'}
        other = {'name': 'tests/test_login.py::Login.test_valid', 'kind': 'acceptance'}
        worktree = self.repo.parent / 'worktree'
        git(self.repo, 'worktree', 'add', '-q', str(worktree), self.head)
        written = verify_history.record(worktree, self.receipt('PASS', [proof, other]), proved)
        verify_history.record(self.repo, self.receipt('FAIL', [proof]), proved)
        verify_history.record(self.repo, self.receipt('PASS', [new], [{'name': 'login', 'kind': 'proof'}]), proved)
        verify_history.record(self.repo, self.receipt('INCOMPLETE', [other]), proved)
        self.assertEqual(written, self.repo / '.git/coldverify/receipts.jsonl')
        entries, rejected = verify_history.read(written)
        self.assertEqual(rejected, 0)
        self.assertEqual(entries[0]['paths'], ['app/login.py', 'docs/login.md', 'tests/test_login.py'])
        self.assertEqual(entries[0]['proofs'], [{'strategy': 'repro', 'name': proof['name']}])
        self.assertEqual((entries[0]['commit'], entries[0]['pull_request']), (self.head, 7))
        self.assertEqual(verify_history.table(entries), [
            'change_type\tstrategy\tPASS\tFAIL\tINCOMPLETE',
            'docs+py+tests\tflow+missing-on-base\t1\t0\t0',
            'docs+py+tests\tnone\t0\t0\t1',
            'docs+py+tests\trepro\t1\t1\t0'])

    def test_unreadable_or_foreign_lines_are_counted_not_coerced(self):
        import verify_history
        path = verify_history.record(self.repo, self.receipt('PASS', [{'name': 'a', 'kind': 'proof'}]), proved)
        good = verify_history.read(path)[0][0]
        broken = [{**good, 'result': 'MAYBE'}, {**good, 'schema_version': 2}, {**good, 'schema_version': True},
                  {**good, 'proofs': [{'strategy': 'guess', 'name': 'a'}]}, {**good, 'proofs': [{'strategy': 'repro'}]},
                  {key: value for key, value in good.items() if key != 'commit'},
                  *({key: value for key, value in good.items() if key != name} for name in ('paths', 'tree', 'pull_request')),
                  {**good, 'pull_request': '7'}]
        with path.open('a') as stream:
            stream.write('not json\n' + ''.join(json.dumps(item) + '\n' for item in broken) + '{"schema_version": 1, "res')
        unbound = {**self.receipt('PASS', [{'name': 'b', 'kind': 'proof'}]), 'tree': None, 'pull_request': None}
        verify_history.record(self.repo, unbound, proved)
        entries, rejected = verify_history.read(path)
        self.assertEqual(([item['proofs'][0]['name'] for item in entries], rejected), (['a', 'b'], 12))
        self.assertEqual((entries[1]['tree'], entries[1]['pull_request']), (None, None))

    def test_the_newest_pass_on_the_same_paths_is_suggested_and_used_when_one_of_its_proofs_ran(self):
        import verify_history
        old = {'name': 'tests/test_login.py::Login.test_old', 'kind': 'proof'}
        new = {'name': 'tests/test_login.py::Login.test_new', 'kind': 'proof'}
        other = {'name': 'tests/test_login.py::Login.test_other', 'kind': 'proof'}
        verify_history.record(self.repo, self.receipt('PASS', [old]), proved)
        verify_history.record(self.repo, self.receipt('PASS', [new], [{'name': 'login', 'kind': 'proof'}]), proved)
        verify_history.record(self.repo, self.receipt('FAIL', [other]), proved)
        verify_history.record(self.repo, self.receipt('PASS'), proved)
        failed_on_head = {**new, 'kind': 'regression'}
        found = verify_history.suggest(self.repo, {**self.receipt('FAIL')['repro'], 'tests': [failed_on_head]})
        self.assertEqual({key: found[key] for key in ('commit', 'shared_paths', 'proofs', 'used')}, {
            'commit': self.head, 'shared_paths': ['app/login.py', 'docs/login.md', 'tests/test_login.py'],
            'proofs': [{'strategy': 'repro', 'name': new['name']}, {'strategy': 'flow', 'name': 'login'}], 'used': True})
        self.assertTrue(verify_history.suggest(self.repo, self.receipt('FAIL')['repro'], {'flows': [{'name': 'login', 'kind': 'regression'}]})['used'])
        self.assertFalse(verify_history.suggest(self.repo, {**self.receipt('FAIL')['repro'], 'tests': [other]})['used'])
        self.assertFalse(verify_history.suggest(self.repo, {**self.receipt('FAIL')['repro'], 'tests': [{'name': 'login'}]})['used'],
                         'a test is not the flow of the same name')

    def test_the_suggestion_is_printed_before_a_run_from_the_merge_base_with_origin_main(self):
        import contextlib
        import io
        import verify_history
        verify_history.record(self.repo, self.receipt('PASS', [{'name': 'a', 'kind': 'proof'}]), proved)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertIsNone(verify_history.announce(self.repo), 'no origin/main: nothing to compare against')
        git(self.repo, 'update-ref', 'refs/remotes/origin/main', self.base)
        with contextlib.redirect_stdout(out):
            found = verify_history.announce(self.repo)
        self.assertEqual(found['commit'], self.head)
        self.assertEqual(out.getvalue(), f'Suggestion: the last PASS for these paths, `{self.head}`, proved it with repro `a`. '
                                         'Run them too; a suggestion never changes the verdict.\n')

    def test_no_suggestion_without_a_shared_path(self):
        import verify_history
        verify_history.record(self.repo, self.receipt('PASS', [{'name': 'a', 'kind': 'proof'}]), proved)
        (self.repo / 'other.py').write_text('y\n')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', 'other')
        later = git(self.repo, 'rev-parse', 'HEAD')
        self.assertIsNone(verify_history.suggest(self.repo, {'base': self.head, 'head': later, 'tests': []}))

    def test_no_repro_base_and_head_means_unknown_change_type(self):
        import verify_history
        receipt = {**self.receipt('INCOMPLETE'), 'repro': None}
        self.assertEqual(verify_history.entry(self.repo, receipt, proved)['change_type'], 'unknown')

    def test_path_kinds(self):
        import verify_history
        kinds = {path: verify_history.path_kind(path) for path in (
            '.github/workflows/verify.yml', '.fleet/verify.sh', 'tests/test_x.py', 'src/login_test.go', 'web/a.spec.ts',
            'README.md', 'docs/arch.svg', 'ci/verify_receipt.py', 'Makefile')}
        self.assertEqual(kinds, {'.github/workflows/verify.yml': 'ci', '.fleet/verify.sh': 'ci', 'tests/test_x.py': 'tests',
                                 'src/login_test.go': 'tests', 'web/a.spec.ts': 'tests', 'README.md': 'docs',
                                 'docs/arch.svg': 'docs', 'ci/verify_receipt.py': 'py', 'Makefile': 'other'})


if __name__ == '__main__':
    unittest.main()
