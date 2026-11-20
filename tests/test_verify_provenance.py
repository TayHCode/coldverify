import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ci'))
import verify_receipt

FAKE_GH = '''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
if args[:2] == ['pr', 'view']:
    if '--repo' in args and args[2].startswith('--'):
        print('argument required when using the --repo flag', file=sys.stderr)
        sys.exit(1)
    expected_repo = os.environ.get('FAKE_EXPECT_REPO')
    if expected_repo and os.environ.get('GH_REPO') != expected_repo:
        print('wrong PR lookup repository', file=sys.stderr)
        sys.exit(1)
    if args[2:3] == ['7'] and 'FAKE_GH_LIVE_RAW' in os.environ:
        print(os.environ['FAKE_GH_LIVE_RAW'])
        sys.exit(0)
    body = os.environ.get('FAKE_GH_LIVE_BODY', os.environ.get('FAKE_GH_BODY')) if args[2:3] == ['7'] else os.environ.get('FAKE_GH_BODY')
    if body is None:
        print('no pull requests found for branch "fixture"', file=sys.stderr)
        sys.exit(1)
    print(json.dumps({'number': 7, 'body': body, 'baseRefOid': os.environ['FAKE_PR_BASE'],
                      'headRefOid': os.environ['FAKE_PR_HEAD']}))
    sys.exit(0)
if args[0] == 'api':
    if os.environ.get('FAKE_GH_API_ERROR'):
        print(os.environ['FAKE_GH_API_ERROR'], file=sys.stderr)
        sys.exit(1)
    path = next(arg for arg in args if arg.startswith('repos/o/r/'))[len('repos/o/r/'):]
    responses = json.loads(os.environ.get('FAKE_GH_API', '{}'))
    if path not in responses:
        print('gh: Not Found (HTTP 404)', file=sys.stderr)
        sys.exit(1)
    found = responses[path]
    if 'check_runs' in found and '--paginate' not in args:
        found = {**found, 'check_runs': found['check_runs'][:1]}
    if '--jq' in args:
        for run in found['check_runs']:
            print(json.dumps(run))
    else:
        print(json.dumps(found))
    sys.exit(0)
sys.exit(1)
'''


class RequireReceipt(unittest.TestCase):
    def setUp(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = folder / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('remote', 'add', 'fixture', 'https://example.invalid/o/r.git')
        self.parent = self.commit('parent', 'one')
        self.head = self.commit('head', 'two')
        self.git('update-ref', 'refs/remotes/origin/main', self.parent)
        bin_dir = folder / 'bin'
        bin_dir.mkdir()
        (bin_dir / 'gh').write_text(FAKE_GH)
        (bin_dir / 'gh').chmod(0o755)
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(('GITHUB_', 'COLD_VERIFY_', 'FAKE_', 'GIT_', 'GH_'))}
        self.env.update(PATH=f'{bin_dir}{os.pathsep}{os.environ["PATH"]}', GITHUB_REPOSITORY='o/r')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
                               *args], check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, message, text):
        (self.repo / 'value.txt').write_text(text)
        self.git('add', '-A')
        self.git('commit', '-q', '-m', message)
        return self.git('rev-parse', 'HEAD')

    def tree(self, commit):
        return self.git('rev-parse', f'{commit}^{{tree}}')

    def run_id(self, number, commit, tree=None, app='github-actions', conclusion='success', name='coldverify receipt',
               builders=(), pull_request=None, suite=None, base=None, claim='Adds two.'):
        suite = suite or commit
        evidence = {'schema_version': 10, 'pull_request': pull_request, 'builders': list(builders),
                    'result': 'PASS', 'commit': suite, 'tree': tree or self.tree(suite), 'claim': claim,
                    'repro': {'base': base or self.parent, 'head': commit},
                    'run': {'result': 'PASS', 'head': suite, 'tree': self.tree(suite)}}
        return {'id': number, 'name': name, 'head_sha': commit, 'status': 'completed', 'conclusion': conclusion,
                'external_id': evidence['tree'], 'app': {'slug': app},
                'output': {'text': json.dumps(evidence)},
                'html_url': f'https://github.com/o/r/runs/{number}'}

    def require(self, runs=(), attached=None, **env):
        api = {f'commits/{self.head}/check-runs': {'total_count': len(runs), 'check_runs': list(runs)}}
        api.update({f'check-runs/{run["id"]}': run for run in attached or ()})
        result = subprocess.run([sys.executable, str(ROOT / 'coldverify'), '--require-receipt', '--claim', 'Adds two.'], cwd=self.repo,
                                env={**self.env, 'FAKE_GH_API': json.dumps(api), 'FAKE_PR_BASE': self.parent,
                                     'FAKE_PR_HEAD': self.head, **env}, text=True, capture_output=True)
        return result.returncode, result.stdout.strip().splitlines()[-1] if result.stdout.strip() else result.stderr

    def test_the_suite_reports_its_own_committed_tree_only_when_the_snapshot_matches(self):
        (self.repo / '.fleet').mkdir()
        (self.repo / '.fleet/verify.sh').write_text('python3 "$COLD_VERIFY_UNITTEST" discover\n')
        (self.repo / 'test_value.py').write_text(
            'import unittest\nfrom pathlib import Path\n'
            'class Value(unittest.TestCase):\n'
            '    def test_value(self):\n'
            '        self.assertIn(Path("value.txt").read_text(), ("two", "dirty"))\n')
        head = self.commit('suite recipe', 'two')
        command = [sys.executable, str(ROOT / 'ci/verify_trace.py'), '--root', str(self.repo), '--',
                   'bash', str(ROOT / 'ci/fleet-verify.sh')]
        for value, tree in (('two', self.tree(head)), ('dirty', None)):
            with self.subTest(value=value):
                (self.repo / 'value.txt').write_text(value)
                result = subprocess.run(command, cwd=self.repo, env={**self.env, 'COLD_VERIFY_BACKEND': 'local'},
                                        text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                report = json.loads((self.repo / '.cold/verify-result.json').read_text())
                self.assertIn('tree', report)
                self.assertEqual((report['head'], report['tree'], report['tests_ran']), (head, tree, 1))

    def test_no_receipt_for_the_head_tree_fails(self):
        code, line = self.require()
        self.assertEqual(code, 1, line)
        self.assertEqual(line, f'COLDVERIFY RECEIPT FAIL: no PASS receipt from GitHub Actions for tree {self.tree(self.head)}')

    def test_a_trusted_pass_receipt_bound_to_the_head_tree_passes(self):
        code, line = self.require([self.run_id(5, self.head)])
        self.assertEqual(code, 0, line)
        self.assertEqual(line, f'COLDVERIFY RECEIPT PASS: https://github.com/o/r/runs/5 is a PASS receipt from GitHub Actions '
                               f'for tree {self.tree(self.head)}')

    def test_branch_lookup_uses_the_configured_repository_without_a_pr_number(self):
        code, line = self.require([self.run_id(5, self.head)], GH_REPO='other/repo', FAKE_EXPECT_REPO='o/r',
                                  FAKE_GH_BODY='## Claim\nAdds two.')
        self.assertEqual(code, 0, line)
        self.assertIn('https://github.com/o/r/runs/5 is a PASS receipt', line)

    def test_mismatched_receipt_inputs_fail_even_when_the_check_matches_the_tree(self):
        for field, value, reason in (
            ('claim', 'Adds three.', 'receipt claim differs from the current claim'),
            ('pull_request', 8, 'receipt names a different PR'),
            ('repro', {'base': self.head, 'head': self.head}, 'repro base or head differs'),
            ('repro', {'base': self.parent, 'head': self.parent}, 'repro base or head differs'),
            ('run', {'result': 'PASS', 'head': self.parent, 'tree': self.tree(self.head)}, 'suite commit or tree differs'),
            ('run', {'result': 'PASS', 'head': self.head, 'tree': self.tree(self.parent)}, 'suite commit or tree differs'),
            ('run', {'result': 'PASS', 'head': self.head}, 'suite commit or tree differs'),
        ):
            with self.subTest(field=field, value=value):
                check = self.run_id(5, self.head)
                evidence = json.loads(check['output']['text'])
                evidence[field] = value
                check['output']['text'] = json.dumps(evidence)
                code, line = self.require([check], FAKE_GH_BODY='## Claim\nAdds two.')
                self.assertEqual(code, 1, line)
                self.assertIn(reason, line)

    def test_a_live_pr_claim_edit_invalidates_the_same_published_check(self):
        check = self.run_id(5, self.head, pull_request=7)
        api = {f'commits/{self.head}/check-runs': {'check_runs': [check]}}
        env = {**self.env, 'FAKE_GH_API': json.dumps(api), 'FAKE_PR_BASE': self.parent, 'FAKE_PR_HEAD': self.head}
        with patch.dict(os.environ, {**env, 'FAKE_GH_BODY': '## Claim\nAdds two.'}, clear=True):
            self.assertEqual(verify_receipt.require(self.repo)[0], 'PASS')
        with patch.dict(os.environ, {**env, 'FAKE_GH_BODY': '## Claim\nAdds three.'}, clear=True):
            result, reason = verify_receipt.require(self.repo)
        self.assertEqual(result, 'FAIL', reason)
        self.assertIn('receipt claim differs from the current claim', reason)

    def test_a_base_ref_move_invalidates_evidence_on_the_same_pr_head(self):
        self.git('checkout', '-q', '--detach', self.parent)
        current_base = self.commit('new base', 'new base contents')
        self.git('checkout', '-q', '--detach', self.head)
        code, line = self.require([self.run_id(5, self.head)], FAKE_PR_BASE=current_base, FAKE_GH_BODY='## Claim\nAdds two.')
        self.assertEqual(code, 1, line)
        self.assertIn('repro base or head differs', line)

    def test_a_receipt_bound_to_another_tree_is_stale(self):
        code, line = self.require([self.run_id(5, self.head, tree=self.tree(self.parent))])
        self.assertEqual(code, 1, line)
        self.assertIn(f'https://github.com/o/r/runs/5: bound to tree {self.tree(self.parent)}, not {self.tree(self.head)}', line)

    def test_a_receipt_from_another_app_is_not_trusted(self):
        code, line = self.require([self.run_id(5, self.head, app='someone-else')])
        self.assertEqual(code, 1, line)
        self.assertIn('https://github.com/o/r/runs/5: published by someone-else, not GitHub Actions', line)

    def test_a_failed_receipt_fails(self):
        for conclusion in ('failure', 'neutral', 'skipped'):
            code, line = self.require([self.run_id(5, self.head, conclusion=conclusion)])
            self.assertEqual(code, 1, line)
            self.assertIn(f'https://github.com/o/r/runs/5: conclusion {conclusion}, not success', line)

    def test_a_receipt_on_a_later_page_is_found(self):
        code, line = self.require([self.run_id(4, self.head, app='someone-else'), self.run_id(5, self.head)])
        self.assertEqual(code, 0, line)
        self.assertIn('https://github.com/o/r/runs/5 is a PASS receipt', line)

    def test_a_receipt_still_in_progress_is_not_a_pass(self):
        code, line = self.require([{**self.run_id(5, self.head), 'status': 'in_progress', 'conclusion': None}])
        self.assertEqual(code, 1, line)
        self.assertIn('https://github.com/o/r/runs/5: conclusion in_progress, not success', line)

    def test_another_check_name_is_not_a_receipt(self):
        code, line = self.require([self.run_id(5, self.head, name='verify')])
        self.assertEqual(code, 1, line)
        self.assertIn('https://github.com/o/r/runs/5: named verify, not coldverify receipt', line)

    def test_an_attached_pre_pr_receipt_on_another_commit_with_the_same_tree_fails(self):
        before = self.head
        self.git('commit', '-q', '--amend', '-m', 'head, reworded')
        self.head = self.git('rev-parse', 'HEAD')
        self.assertNotEqual(before, self.head)
        code, line = self.require(attached=[self.run_id(42, before)],
                                  FAKE_GH_BODY='## Claim\nAdds two.\n\n## Evidence\nColdverify-Receipt: https://github.com/o/r/runs/42\n')
        self.assertEqual(code, 1, line)
        self.assertIn('check run does not name the current PR head', line)

    def test_an_attached_receipt_in_a_body_with_windows_line_endings_is_read(self):
        code, line = self.require(attached=[self.run_id(42, self.head)],
                                  FAKE_GH_BODY='## Claim\r\nAdds two.\r\n\r\n## Evidence\r\nColdverify-Receipt: https://github.com/o/r/runs/42\r\n')
        self.assertEqual(code, 0, line)

    def test_an_attached_receipt_for_an_ancestor_is_not_enough(self):
        code, line = self.require(attached=[self.run_id(42, self.parent)], FAKE_GH_BODY='Coldverify-Receipt: 42\n')
        self.assertEqual(code, 1, line)
        self.assertIn(f'https://github.com/o/r/runs/42: bound to tree {self.tree(self.parent)}, not {self.tree(self.head)}', line)

    def test_an_unreadable_receipt_list_is_incomplete_not_fail(self):
        code, line = self.require(FAKE_GH_API_ERROR='HTTP 401: Bad credentials')
        self.assertEqual(code, 2, line)
        self.assertEqual(line, 'COLDVERIFY RECEIPT INCOMPLETE: could not read the receipts: HTTP 401: Bad credentials')

    def builder_require(self, recorded, body):
        return self.require([self.run_id(5, self.head, builders=recorded)], FAKE_GH_BODY=f'## Claim\nAdds two.\n\n## Builder\n{body}')

    def test_a_builder_model_line_edited_after_the_pass_rejects_the_receipt(self):
        code, line = self.builder_require(['claude-opus-5-5'], 'Builder-Model: gpt-6.1-terra\n')
        self.assertEqual(code, 1, line)
        self.assertIn('https://github.com/o/r/runs/5: issued for builders claude-opus-5-5, but the "Builder-Model:" lines of '
                      'PR #7 now name gpt-6.1-terra; rerun coldverify on the current description', line)

    def test_a_builder_model_line_removed_after_the_pass_rejects_the_receipt(self):
        code, line = self.builder_require(['claude-opus-5-5'], '')
        self.assertEqual(code, 1, line)
        self.assertIn('issued for builders claude-opus-5-5, but the "Builder-Model:" lines of PR #7 now name none', line)

    def test_a_second_builder_model_line_added_after_the_pass_rejects_the_receipt(self):
        code, line = self.builder_require(['claude-opus-5-5'], 'Builder-Model: claude-opus-5-5\nBuilder-Model: gpt-6.1-terra\n')
        self.assertEqual(code, 1, line)
        self.assertIn('issued for builders claude-opus-5-5, but the "Builder-Model:" lines of PR #7 now name '
                      'claude-opus-5-5, gpt-6.1-terra', line)

    def test_unchanged_builder_model_lines_keep_the_receipt(self):
        code, line = self.builder_require(['claude-opus-5-5', 'gpt-6-luna'],
                                          'Builder-Model: GPT-6-Luna\n  builder-model: claude-opus-5-5\n')
        self.assertEqual(code, 0, line)
        self.assertIn('https://github.com/o/r/runs/5 is a PASS receipt', line)

    def test_a_pull_request_event_compares_the_live_builder_lines_not_the_event_copy(self):
        event = Path(self.repo.parent) / 'event.json'
        event.write_text(json.dumps({'pull_request': {'number': 7, 'body': '## Claim\nAdds two.\n\n## Builder\nBuilder-Model: claude-opus-5-5\n',
                                                      'head': {'sha': self.head}}}))
        code, line = self.require([self.run_id(5, self.head, builders=['claude-opus-5-5'], pull_request=7)],
                                  GITHUB_EVENT_NAME='pull_request', GITHUB_EVENT_PATH=str(event),
                                  FAKE_GH_LIVE_BODY='## Claim\nAdds two.\n\n## Builder\nBuilder-Model: gpt-6.1-terra\n')
        self.assertEqual(code, 1, line)
        self.assertIn('issued for builders claude-opus-5-5, but the "Builder-Model:" lines of PR #7 now name gpt-6.1-terra', line)

    def test_a_receipt_issued_for_a_pr_is_compared_with_that_pr_when_the_branch_has_none(self):
        receipt = self.run_id(5, self.head, builders=['claude-opus-5-5'], pull_request=7)
        code, line = self.require([receipt], FAKE_GH_LIVE_BODY='## Claim\nAdds two.\n\n## Builder\nBuilder-Model: gpt-6.1-terra\n')
        self.assertEqual(code, 1, line)
        self.assertIn('issued for builders claude-opus-5-5, but the "Builder-Model:" lines of PR #7 now name gpt-6.1-terra', line)
        code, line = self.require([receipt])
        self.assertEqual(code, 2, line)
        self.assertEqual(line, 'COLDVERIFY RECEIPT INCOMPLETE: could not read the current PR inputs for '
                               'https://github.com/o/r/runs/5: no pull requests found for branch "fixture"')

    def test_an_unreadable_live_pr_body_is_incomplete(self):
        for raw in ('{}', '{"body": null}', 'not json'):
            with self.subTest(raw=raw):
                code, line = self.require([self.run_id(5, self.head, pull_request=7)], FAKE_GH_LIVE_RAW=raw)
                self.assertEqual(code, 2, line)
                self.assertEqual(line, 'COLDVERIFY RECEIPT INCOMPLETE: could not read the current PR inputs for '
                                       'https://github.com/o/r/runs/5: gh pr view returned no PR body or commit identity')

    def test_an_unreadable_receipt_does_not_hide_a_later_valid_one(self):
        code, line = self.require([self.run_id(4, self.head, pull_request=7), self.run_id(5, self.head)])
        self.assertEqual(code, 0, line)
        self.assertIn('https://github.com/o/r/runs/5 is a PASS receipt', line)

    def test_a_receipt_that_records_no_builders_is_rejected(self):
        for output in ({}, {'text': json.dumps({'schema_version': 9})}, {'text': 'The receipt JSON is 70000 characters, over '
                                                                                   'the check run limit; the job summary keeps it.'}):
            with self.subTest(output=output):
                code, line = self.require([{**self.run_id(5, self.head), 'output': output}])
                self.assertEqual(code, 1, line)
                self.assertIn('https://github.com/o/r/runs/5: records no builders it was issued for (a receipt from before #123, '
                              'or one too large to keep its JSON); rerun coldverify', line)

    def test_a_pr_without_a_current_claim_cannot_reuse_a_receipt(self):
        code, line = self.require([self.run_id(5, self.head)], FAKE_GH_BODY='## Summary\nAdds two.')
        self.assertEqual(code, 2, line)
        self.assertIn('no current claim', line)

    def test_a_pull_request_event_checks_the_pr_head_commit(self):
        event = Path(self.repo.parent) / 'event.json'
        self.git('checkout', '-q', '--detach', self.parent)
        (self.repo / 'integration.txt').write_text('only on the base')
        self.git('add', 'integration.txt')
        self.git('commit', '-qm', 'base integration file')
        base = self.git('rev-parse', 'HEAD')
        self.git('merge', '--no-ff', '-qm', 'merge checkout', self.head)
        merge = self.git('rev-parse', 'HEAD')
        self.assertNotEqual(self.tree(merge), self.tree(self.head))
        event.write_text(json.dumps({'pull_request': {'number': 7, 'body': '', 'head': {'sha': self.head}}}))
        receipt = self.run_id(5, self.head, suite=merge, base=base, pull_request=7)
        code, line = self.require([receipt], GITHUB_EVENT_NAME='pull_request', GITHUB_EVENT_PATH=str(event), FAKE_GH_LIVE_BODY='## Claim\nAdds two.', FAKE_PR_BASE=base)
        self.assertEqual(code, 0, line)

        self.git('checkout', '-q', '--detach', base)
        moved_base = self.commit('move target branch', 'new base change')
        self.git('checkout', '-q', '--detach', merge)
        code, line = self.require([receipt], GITHUB_EVENT_NAME='pull_request', GITHUB_EVENT_PATH=str(event),
                                  FAKE_GH_LIVE_BODY='## Claim\nAdds two.', FAKE_PR_BASE=moved_base)
        self.assertEqual(code, 1, line)
        self.assertIn('checkout is neither the current PR head nor its merge with the current base', line)


if __name__ == '__main__':
    unittest.main()
