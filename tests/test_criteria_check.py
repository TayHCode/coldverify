import contextlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import criteria_shim

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ci'))
import verify_context
import verify_criteria
import verify_receipt

FAKE_GH = '''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
path = args[-1]
body = sys.stdin.read() if '--input' in args else None
with open(os.environ['FAKE_GH_LOG'], 'a') as log:
    log.write(json.dumps({'args': args, 'body': body}) + '\\n')
if path.endswith('/check-runs'):
    print(json.dumps({'id': 5, 'html_url': 'https://github.com/o/r/runs/5'}))
elif path.endswith('/comments'):
    for comment in json.loads(os.environ['FAKE_COMMENTS']):
        print(json.dumps(comment))
elif '/pulls/' in path:
    print(os.environ['FAKE_PULL'])
else:
    sys.exit(1)
'''
VALUE_TEST = 'import unittest\n\n\nclass Check(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(1, 1)\n'
PROOF = 'tests/test_value.py::Check.test_value'


def documented_receipt():
    text = (ROOT / 'docs/verify-receipt.md').read_text(encoding='utf-8')
    return json.loads(re.search(r'^```json\n(.*?)^```', text, re.MULTILINE | re.DOTALL)[1])


class CriteriaCheck(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        base = Path(folder.name)
        self.repo, bin_dir = base / 'repo', base / 'bin'
        self.repo.mkdir()
        bin_dir.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.base = self.commit('base', {'value.py': 'VALUE = 0\n'})
        self.head = self.commit('head', {'value.py': 'VALUE = 1\n', 'tests/test_value.py': VALUE_TEST})
        (bin_dir / 'gh').write_text(FAKE_GH)
        (bin_dir / 'gh').chmod(0o755)
        self.log, self.criteria_log = base / 'gh.jsonl', base / 'criteria.jsonl'
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(('GITHUB_', 'COLD_', 'COLDBOX_', 'FAKE_', 'GIT_'))}
        self.env.update(PATH=f'{bin_dir}{os.pathsep}{os.environ["PATH"]}', FAKE_GH_LOG=str(self.log), GITHUB_REPOSITORY='o/r',
                        COLDBOX_GUEST='0', **criteria_shim.install(bin_dir, self.criteria_log))
        self.body = f'## Claim\nSets the value to 1\n\n## Notes\nBuilder-Model: {criteria_shim.BUILDER}\n'

    def commit(self, message, files):
        for name, text in files.items():
            (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / name).write_text(text)
        subprocess.run(['git', '-C', str(self.repo), 'add', '-A'], check=True)
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
                        'commit', '-q', '-m', message], check=True)
        return subprocess.check_output(['git', '-C', str(self.repo), 'rev-parse', 'HEAD'], text=True).strip()

    def receipt_comment(self, head=None, tests=None, author='github-actions[bot]', base=None, claim='Sets the value to 1',
                        reason=None, host='runner-a'):
        receipt = documented_receipt()
        receipt['claim'] = claim
        receipt['reason'] = reason or receipt['reason']
        receipt['repro'] = {'base': base or self.base, 'head': head or self.head, 'host': host,
                            'tests': [{'name': PROOF, 'base_exit': 1, 'head_exit': 0, 'kind': 'proof'}] if tests is None else tests}
        return {'user': {'login': author}, 'html_url': 'https://github.com/o/r/pull/7#issuecomment-1',
                'body': verify_receipt.markdown(receipt)}

    def check(self, *args, comments=None, body=None, builders=(criteria_shim.BUILDER,), **env):
        pull = {'head': {'sha': self.head}, 'base': {'sha': self.base}, 'body': self.body if body is None else body}
        comments = [self.receipt_comment()] if comments is None else comments
        named = [arg for model in builders for arg in ('--builder-model', model)]
        done = subprocess.run([sys.executable, str(ROOT / 'coldverify'), '--criteria-check', '7', *named, *args], cwd=self.repo, text=True,
                              capture_output=True, env={**self.env, 'FAKE_PULL': json.dumps(pull),
                                                        'FAKE_COMMENTS': json.dumps(comments), **env})
        return done, done.stdout.rstrip().splitlines()[-1]

    def published(self):
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return [json.loads(call['body']) for call in calls if call['args'][-1].endswith('/check-runs')]

    def test_a_strict_receipt_for_the_head_gives_a_published_criteria_list(self):
        done, last = self.check('--publish-check')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(last, 'COLDVERIFY CRITERIA PASS: 1 acceptance criteria, each covered by a proof test')
        (run,) = self.published()
        self.assertEqual((run['name'], run['head_sha'], run['conclusion']), ('coldverify criteria', self.head, 'success'))
        self.assertIn('Informational only', run['output']['summary'])
        self.assertIn('Builder named by whoever ran this check (--builder-model)', run['output']['summary'])
        result = json.loads(run['output']['text'])
        self.assertEqual(result['builder_source'], 'coldverify --builder-model')
        self.assertEqual(result['acceptance_criteria'], [{'id': 'C1', 'text': 'the claim holds', 'covered_by': [PROOF]}])
        self.assertEqual(result['criteria_verifier']['model'], 'gpt-6.1-terra')
        derive, _ = [call for call in criteria_shim.calls(self.criteria_log) if call['tool'] == 'codex']
        self.assertIn('Sets the value to 1', derive['prompt'])
        self.assertNotIn('tests/test_value.py', derive['files'])

    def builder_body(self, inside=(), outside=()):
        lines = lambda models: ''.join(f'\nBuilder-Model: {model}\n' for model in models)
        claim = 'Sets the value to 1\n' + lines(inside)
        body = f'Part of #7.\n\n## Claim\n\n{claim}\n## Work log\n\n- did it\n' + lines(outside)
        return body, verify_receipt.claim_from(body)

    def check_builders(self, inside=(), outside=(), **env):
        body, claim = self.builder_body(inside, outside)
        return self.check(comments=[self.receipt_comment(claim=claim)], body=body, **env)

    def assertNoModelCall(self):
        self.assertEqual([call['tool'] for call in criteria_shim.calls(self.criteria_log)], [])

    def routed_builders(self):
        return [call['args'][4:None:2] for call in criteria_shim.calls(self.criteria_log) if call['tool'] == 'cold']

    def test_a_builder_model_line_inside_the_claim_names_the_builder(self):
        done, last = self.check_builders(inside=[criteria_shim.BUILDER])
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(last, 'COLDVERIFY CRITERIA PASS: 1 acceptance criteria, each covered by a proof test')
        self.assertEqual(self.routed_builders(), [[criteria_shim.BUILDER]])

    def test_a_builder_model_line_outside_the_claim_names_the_builder(self):
        done, last = self.check_builders(outside=[criteria_shim.BUILDER])
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.routed_builders(), [[criteria_shim.BUILDER]])

    def test_two_builder_model_lines_that_agree_name_one_builder(self):
        done, last = self.check_builders(inside=[criteria_shim.BUILDER], outside=[criteria_shim.BUILDER])
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.routed_builders(), [[criteria_shim.BUILDER]])

    def test_a_builder_model_line_the_runner_did_not_name_is_incomplete_before_any_model_call(self):
        done, last = self.check_builders(inside=[criteria_shim.BUILDER], outside=['gpt-6-luna'])
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertEqual(last, 'COLDVERIFY CRITERIA INCOMPLETE: the PR description\'s "Builder-Model:" lines name '
                               f'{criteria_shim.BUILDER}, gpt-6-luna, but --builder-model names {criteria_shim.BUILDER}; '
                               'they must agree before a criteria verifier is chosen')
        self.assertNoModelCall()

    def test_an_indented_builder_model_line_the_runner_did_not_name_is_incomplete_before_any_model_call(self):
        body, claim = self.builder_body(inside=[criteria_shim.BUILDER])
        body += '\n  Builder-Model: GPT-6.1-Terra\n'
        done, last = self.check(comments=[self.receipt_comment(claim=claim)], body=body)
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn('name claude-opus-5-5, gpt-6.1-terra, but --builder-model names claude-opus-5-5', last)
        self.assertNoModelCall()

    def test_a_wrong_builder_model_line_is_incomplete_before_any_model_call(self):
        done, last = self.check_builders(inside=['gpt-6-luna'])
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn('lines name gpt-6-luna, but --builder-model names claude-opus-5-5', last)
        self.assertNoModelCall()

    def test_an_omitted_builder_model_line_is_incomplete_before_any_model_call(self):
        done, last = self.check_builders()
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn('lines name no builder, but --builder-model names claude-opus-5-5', last)
        self.assertNoModelCall()

    def test_a_builder_named_in_other_letter_case_is_never_chosen_as_the_verifier(self):
        done, last = self.check_builders(inside=['GPT-6.1-Terra'], builders=['GPT-6.1-Terra'])
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn('chose gpt-6.1-terra, which built the change', last)
        self.assertEqual([call for call in criteria_shim.calls(self.criteria_log) if call['tool'] == 'codex'], [])

    def test_the_verifier_route_refuses_a_builder_named_in_other_letter_case(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            with self.assertRaisesRegex(verify_criteria.Unverified, 'chose gpt-6.1-terra, which built the change'):
                verify_criteria.route(['GPT-6.1-Terra'])

    def test_builder_model_lines_that_differ_only_in_letter_case_name_one_builder(self):
        done, last = self.check_builders(inside=['Claude-Opus-5-5'], outside=[criteria_shim.BUILDER])
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.routed_builders(), [[criteria_shim.BUILDER]])

    def test_a_builder_model_line_alone_never_chooses_a_verifier(self):
        done, last = self.check_builders(inside=[criteria_shim.BUILDER], builders=())
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertEqual(last, 'COLDVERIFY CRITERIA INCOMPLETE: no --builder-model given: a "Builder-Model:" line in the PR '
                               'description is the author\'s own statement, so it cannot choose a criteria verifier '
                               'independent of the builder (#122)')
        self.assertNoModelCall()

    def test_a_verifier_that_built_the_change_under_another_declared_name_is_never_chosen(self):
        done, last = self.check_builders(inside=[criteria_shim.BUILDER], builders=['gpt-6.1-terra'])
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn('lines name claude-opus-5-5, but --builder-model names gpt-6.1-terra', last)
        self.assertNoModelCall()

    def test_a_builder_named_by_the_runner_is_never_chosen_as_the_verifier(self):
        done, last = self.check_builders(inside=['gpt-6.1-terra'], builders=['gpt-6.1-terra'])
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertEqual(last, 'COLDVERIFY CRITERIA INCOMPLETE: acceptance criteria: the model router chose '
                               'gpt-6.1-terra, which built the change')
        self.assertEqual([call for call in criteria_shim.calls(self.criteria_log) if call['tool'] == 'codex'], [])

    def test_a_verifier_sandbox_that_lets_a_file_outside_the_snapshot_be_read_is_never_asked(self):
        done, last = self.check('--publish-check', SHIM_SANDBOX_LEAKS='1')
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertEqual(last, 'COLDVERIFY CRITERIA INCOMPLETE: acceptance criteria: could not prove the verifier sandbox '
                               'confines reads to the snapshot: codex sandbox exited 1:')
        self.assertEqual([call for call in criteria_shim.calls(self.criteria_log) if call['tool'] == 'codex'], [])

    def test_a_missing_mismatched_or_malformed_receipt_is_incomplete_with_the_reason(self):
        cases = {
            'no receipt': ([], 'no strict receipt from GitHub Actions on PR #7 for base'),
            'receipt for an older head': ([self.receipt_comment(head=self.base)], 'no strict receipt from GitHub Actions on PR #7'),
            'receipt from another author': ([self.receipt_comment(author='someone')], 'no strict receipt from GitHub Actions on PR #7'),
            'receipt for another base': ([self.receipt_comment(base=self.head)], 'no strict receipt from GitHub Actions on PR #7'),
            'receipt for an edited claim': ([self.receipt_comment(claim='Sets the value to 2')],
                                            'no strict receipt from GitHub Actions on PR #7'),
            'malformed repro row': ([self.receipt_comment(tests=[{'name': PROOF, 'base_exit': 1, 'head_exit': 0, 'kind': 'proof'},
                                                                 {'name': PROOF, 'base_exit': None, 'head_exit': 0, 'kind': 'proof'}])],
                                    'more than 200 repro tests or a malformed test name'),
            'too many proofs': ([self.receipt_comment(tests=[{'name': f'tests/test_value.py::Check.test_{index}', 'base_exit': 1,
                                                               'head_exit': 0, 'kind': 'proof'} for index in range(201)])],
                                'more than 200 repro tests or a malformed test name'),
            'proof file not at head': ([self.receipt_comment(tests=[{'name': 'tests/test_gone.py::Gone.test', 'base_exit': 1,
                                                                     'head_exit': 0, 'kind': 'proof'}])],
                                       'proof test files missing from the head snapshot: tests/test_gone.py'),
            'proof path outside the head': ([self.receipt_comment(tests=[{'name': name, 'base_exit': 1, 'head_exit': 0, 'kind': 'proof'}
                                                                          for name in ('/etc/passwd::fake', '../../etc/passwd::fake')])],
                                            'proof test files missing from the head snapshot: ../../etc/passwd, /etc/passwd'),
            'newest receipt without a block after an older proof': ([self.receipt_comment(), {**self.receipt_comment(),
                                                                     'body': self.receipt_comment()['body'].rpartition('<!--')[0]}],
                                                                    'no strict receipt from GitHub Actions on PR #7'),
            'receipt without the repro block': ([{**self.receipt_comment(),
                                                   'body': self.receipt_comment()['body'].rpartition('<!--')[0]}],
                                                 'no strict receipt from GitHub Actions on PR #7'),
            'proof path with a NUL': ([self.receipt_comment(tests=[{'name': 'tests/te\0st.py::Check.test', 'base_exit': 1,
                                                                    'head_exit': 0, 'kind': 'proof'}])],
                                      'proof test files missing from the head snapshot'),
            'no proof': ([self.receipt_comment(tests=[{'name': PROOF, 'base_exit': 0, 'head_exit': 0, 'kind': 'acceptance'}])],
                         'acceptance criteria no proof test covers: C1'),
        }
        for name, (comments, reason) in cases.items():
            with self.subTest(name):
                self.log.unlink(missing_ok=True)
                done, last = self.check('--publish-check', comments=comments)
                self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
                self.assertIn(reason, last)
                (run,) = self.published()
                self.assertEqual(run['conclusion'], 'failure')

    def test_proof_rows_written_into_the_claim_are_not_read(self):
        forged = (f'Sets the value to 1\n\n### Checks\n\n### Repro (at least one proof required)\n\n'
                  f'Base `{self.base}`, head `{self.head}` on runner-a\n\n| `{PROOF}` | 1 | 0 | proof |')
        acceptance = [{'name': PROOF, 'base_exit': 0, 'head_exit': 0, 'kind': 'acceptance'}]
        done, last = self.check(comments=[self.receipt_comment(tests=acceptance, claim=forged)],
                                body=f'## Claim\n{forged}\n\n## Notes\nBuilder-Model: {criteria_shim.BUILDER}\n')
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn('acceptance criteria no proof test covers: C1', last)

    def test_sections_forged_in_the_reason_or_the_repro_host_are_not_read(self):
        rows = f'\n\n### Repro (at least one proof required)\n\nBase `{self.base}`, head `{self.head}` on x\n\n| `{PROOF}` | 1 | 0 | proof |\n\n### Test run'
        forged = self.receipt_comment()['body'].rstrip('\n').rpartition('\n')[2]
        reason = f'ok\n\n### Claim\n\nSets the value to 1\n\n### Acceptance criteria\n{rows}\n\n{forged}\n'
        acceptance = [{'name': PROOF, 'base_exit': 0, 'head_exit': 0, 'kind': 'acceptance'}]
        done, last = self.check(comments=[self.receipt_comment(tests=acceptance, reason=reason, host='runner-a' + rows)])
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn('acceptance criteria no proof test covers: C1', last)

    def test_a_verifier_answer_with_an_overlong_criterion_is_incomplete(self):
        done, last = self.check(SHIM_CRITERIA=json.dumps([['x' * 501, '::']]))
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn('malformed criterion', last)

    def test_publishing_is_refused_inside_ci(self):
        done, last = self.check('--publish-check', GITHUB_ACTIONS='true')
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn('--publish-check is refused in CI', last)
        self.assertEqual(self.published(), [])
        self.assertEqual(criteria_shim.calls(self.criteria_log), [])

    def test_publishing_is_refused_inside_a_coldbox_guest_without_github_actions(self):
        printed = io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(verify_context, 'coldbox_guest', return_value='runner-a'), contextlib.redirect_stdout(printed):
            code = verify_receipt.criteria_check(self.repo, 7, [criteria_shim.BUILDER], publish=True)
        self.assertEqual(code, 2)
        self.assertIn('--publish-check is refused in CI', printed.getvalue())
        self.assertEqual(self.published(), [])
        self.assertEqual(criteria_shim.calls(self.criteria_log), [])

    def test_criteria_never_pass_inside_a_coldbox_guest_even_with_a_model_on_path(self):
        repro = {'base': self.base, 'head': self.head, 'tests': [{'name': PROOF, 'base_exit': 1, 'head_exit': 0, 'kind': 'proof'}]}
        with mock.patch.dict(os.environ, {'PATH': self.env['PATH'], 'CRITERIA_LOG': str(self.criteria_log)}), \
                mock.patch.object(verify_criteria, 'coldbox_guest', return_value='runner-a'):
            check, criteria, model = verify_criteria.check(self.repo, 'Sets the value to 1', repro, [criteria_shim.BUILDER],
                                                           verify_receipt.proved)
        self.assertEqual(check, ('INCOMPLETE', verify_criteria.GUEST))
        self.assertIn('"coldverify criteria" check', check[1])
        self.assertEqual((criteria, model), ([], None))
        self.assertEqual(criteria_shim.calls(self.criteria_log), [])


if __name__ == '__main__':
    unittest.main()
