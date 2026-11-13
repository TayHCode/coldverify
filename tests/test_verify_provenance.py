import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

FAKE_GH = '''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
if args[:2] == ['pr', 'view']:
    if args[2:3] == ['7'] and 'FAKE_GH_LIVE_RAW' in os.environ:
        print(os.environ['FAKE_GH_LIVE_RAW'])
        sys.exit(0)
    body = os.environ.get('FAKE_GH_LIVE_BODY', os.environ.get('FAKE_GH_BODY')) if args[2:3] == ['7'] else os.environ.get('FAKE_GH_BODY')
    if body is None:
        print('no pull requests found for branch "fixture"', file=sys.stderr)
        sys.exit(1)
    print(json.dumps({'number': 7, 'body': body}))
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
               builders=(), pull_request=None):
        return {'id': number, 'name': name, 'head_sha': commit, 'status': 'completed', 'conclusion': conclusion,
                'external_id': tree or self.tree(commit), 'app': {'slug': app},
                'output': {'text': json.dumps({'schema_version': 10, 'pull_request': pull_request, 'builders': list(builders)})},
                'html_url': f'https://github.com/o/r/runs/{number}'}

    def require(self, runs=(), attached=None, **env):
        api = {f'commits/{self.head}/check-runs': {'total_count': len(runs), 'check_runs': list(runs)}}
        api.update({f'check-runs/{run["id"]}': run for run in attached or ()})
        result = subprocess.run([sys.executable, str(ROOT / 'coldverify'), '--require-receipt'], cwd=self.repo,
                                env={**self.env, 'FAKE_GH_API': json.dumps(api), **env}, text=True, capture_output=True)
        return result.returncode, result.stdout.strip().splitlines()[-1] if result.stdout.strip() else result.stderr

    def test_no_receipt_for_the_head_tree_fails(self):
        code, line = self.require()
        self.assertEqual(code, 1, line)
        self.assertEqual(line, f'COLDVERIFY RECEIPT FAIL: no PASS receipt from GitHub Actions for tree {self.tree(self.head)}')

    def test_a_trusted_pass_receipt_bound_to_the_head_tree_passes(self):
        code, line = self.require([self.run_id(5, self.head)])
        self.assertEqual(code, 0, line)
        self.assertEqual(line, f'COLDVERIFY RECEIPT PASS: https://github.com/o/r/runs/5 is a PASS receipt from GitHub Actions '
                               f'for tree {self.tree(self.head)}')

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

    def test_an_attached_pre_pr_receipt_on_another_commit_with_the_same_tree_passes(self):
        before = self.head
        self.git('commit', '-q', '--amend', '-m', 'head, reworded')
        self.head = self.git('rev-parse', 'HEAD')
        self.assertNotEqual(before, self.head)
        code, line = self.require(attached=[self.run_id(42, before)],
                                  FAKE_GH_BODY='## Claim\nAdds two.\n\nColdverify-Receipt: https://github.com/o/r/runs/42\n')
        self.assertEqual(code, 0, line)
        self.assertIn('https://github.com/o/r/runs/42 is a PASS receipt', line)

    def test_an_attached_receipt_in_a_body_with_windows_line_endings_is_read(self):
        before = self.head
        self.git('commit', '-q', '--amend', '-m', 'head, reworded')
        self.head = self.git('rev-parse', 'HEAD')
        code, line = self.require(attached=[self.run_id(42, before)],
                                  FAKE_GH_BODY='## Claim\r\nAdds two.\r\n\r\nColdverify-Receipt: https://github.com/o/r/runs/42\r\n')
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
        return self.require([self.run_id(5, self.head, builders=recorded)], FAKE_GH_BODY=f'## Claim\nAdds two.\n\n{body}')

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
        event.write_text(json.dumps({'pull_request': {'number': 7, 'body': 'Builder-Model: claude-opus-5-5\n',
                                                      'head': {'sha': self.head}}}))
        code, line = self.require([self.run_id(5, self.head, builders=['claude-opus-5-5'], pull_request=7)],
                                  GITHUB_EVENT_NAME='pull_request', GITHUB_EVENT_PATH=str(event),
                                  FAKE_GH_LIVE_BODY='Builder-Model: gpt-6.1-terra\n')
        self.assertEqual(code, 1, line)
        self.assertIn('issued for builders claude-opus-5-5, but the "Builder-Model:" lines of PR #7 now name gpt-6.1-terra', line)

    def test_a_receipt_issued_for_a_pr_is_compared_with_that_pr_when_the_branch_has_none(self):
        receipt = self.run_id(5, self.head, builders=['claude-opus-5-5'], pull_request=7)
        code, line = self.require([receipt], FAKE_GH_LIVE_BODY='Builder-Model: gpt-6.1-terra\n')
        self.assertEqual(code, 1, line)
        self.assertIn('issued for builders claude-opus-5-5, but the "Builder-Model:" lines of PR #7 now name gpt-6.1-terra', line)
        code, line = self.require([receipt])
        self.assertEqual(code, 2, line)
        self.assertEqual(line, "COLDVERIFY RECEIPT INCOMPLETE: could not re-read the PR's Builder-Model lines for "
                               'https://github.com/o/r/runs/5: no pull requests found for branch "fixture"')

    def test_an_unreadable_live_pr_body_is_incomplete(self):
        for raw in ('{}', '{"body": null}', 'not json'):
            with self.subTest(raw=raw):
                code, line = self.require([self.run_id(5, self.head, pull_request=7)], FAKE_GH_LIVE_RAW=raw)
                self.assertEqual(code, 2, line)
                self.assertEqual(line, "COLDVERIFY RECEIPT INCOMPLETE: could not re-read the PR's Builder-Model lines for "
                                       'https://github.com/o/r/runs/5: gh pr view 7 returned no PR body')

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

    def test_a_pull_request_event_checks_the_pr_head_commit(self):
        event = Path(self.repo.parent) / 'event.json'
        merge = self.commit('merge checkout', 'three')
        event.write_text(json.dumps({'pull_request': {'number': 7, 'body': '', 'head': {'sha': self.head}}}))
        receipt = self.run_id(5, self.head, tree=self.tree(merge))
        code, line = self.require([receipt], GITHUB_EVENT_NAME='pull_request', GITHUB_EVENT_PATH=str(event), FAKE_GH_LIVE_BODY='')
        self.assertEqual(code, 0, line)


if __name__ == '__main__':
    unittest.main()
