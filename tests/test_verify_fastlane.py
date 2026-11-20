import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ci'))
import verify_fastlane

IDENTITY = ['-c', 'user.name=t', '-c', 'user.email=t@localhost']
WORKFLOW = '.github/workflows/verify.yml'
ENVIRONMENT = 'e' * 64
STUDIO_C26D49E_LOOP = f'''from studio_core import limits, mdio, metrics, paths, privacy, records, runs
from studio_learn import versions
{'<' * 7} HEAD
from studio_learn.selftest import cases, judge, combiner, combiner_v2, cohorts, preference_rules, engine, promotion
{'=' * 7}
from studio_learn.selftest import cases, judge, combiner, cohorts, preference_rules, engine, promotion, fences
{'>' * 7} 8864085 (Fence edit proposer tests per candidate)
'''
STUDIO_8ED5A66_LOOP = '''from studio_core import limits, mdio, metrics, paths, privacy, records, runs
from studio_learn import versions
from studio_learn.selftest import cases, judge, combiner, combiner_v2, cohorts, preference_rules, engine, promotion, fences
'''
STUDIO_FA470933_GITIGNORE = '''__pycache__/
*.pyc
.venv/
.pytest_cache/
.scratch/
.staging/
tests/verify/evidence/*
!tests/verify/evidence/.gitkeep
'''
STUDIO_FE3286C_GITIGNORE = STUDIO_FA470933_GITIGNORE.replace('.scratch/\n', '.scratch/\n/.worktrees/\n')
FAKE_RELEASE_CHECK = '''
import json, re
from pathlib import Path

def manifest_for(repo, path):
    if path is None and not (repo / 'release-check.json').exists():
        return {'approvals': []}
    return json.loads((path or repo / 'release-check.json').read_text())

class Scanner:
    def __init__(self, terms, private_repos=(), approvals=()):
        self.approvals, self.findings = approvals, []

    def file(self, raw, scope, path):
        for number, line in enumerate(raw.decode().splitlines(), 1):
            for match in re.finditer(r'TOKEN=(\\S+)', line):
                self.findings.append({'scope': scope, 'category': 'secrets', 'kind': 'assigned-secret', 'path': path,
                                      'line': number, 'reviewed': any(a['path'] == path for a in self.approvals)})
            if 'KEYBLOCK' in line:
                self.findings.append({'scope': scope, 'category': 'secrets', 'kind': 'private-key', 'path': path,
                                      'line': number, 'reviewed': False})
            if 'ADDRESS=' in line:
                self.findings.append({'scope': scope, 'category': 'personal', 'kind': 'ipv4', 'path': path,
                                      'line': number, 'reviewed': False})
        if path.endswith('.pem'):
            self.findings.append({'scope': scope, 'category': 'secrets', 'kind': 'credential-file', 'path': path,
                                  'line': 0, 'reviewed': False})
'''


class GitHub(BaseHTTPRequestHandler):
    routes, requests = {}, []

    def do_GET(self):
        self.requests.append((self.path, self.headers.get('Authorization')))
        status, body = self.routes.get(self.path, (404, '{"message": "Not Found"}'))
        self.send_response(status)
        if status == 302:
            self.send_header('Location', body)
            body = ''
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass


class Repository:
    def __init__(self, folder):
        self.path = Path(folder, 'app')
        self.path.mkdir()
        self.git('init', '-q', '-b', 'main')
        (self.path / 'README.md').write_text('readme\n')
        (self.path / 'old.env').write_text('TOKEN=already-here-before\n')
        (self.path / '.gitignore').write_text('/.worktrees/\n')
        self.base = self.commit('base')
        self.git('checkout', '-q', '-b', 'feature')
        (self.path / 'feature.py').write_text('print("feature")\n')
        self.head = self.commit('feature')
        self.git('checkout', '-q', 'main')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.path), *IDENTITY, *args], text=True, capture_output=True,
                              check=True).stdout.strip()

    def commit(self, message):
        self.git('add', '-A')
        self.git('commit', '-q', '-m', message)
        return self.git('rev-parse', 'HEAD')

    def merge(self):
        self.git('checkout', '-q', '--detach', self.git('rev-parse', 'main'))
        self.git('merge', '-q', '--no-ff', '-m', 'merge', self.head)

    def change_feature(self, name, text):
        self.git('checkout', '-q', 'feature')
        Path(self.path, name).parent.mkdir(parents=True, exist_ok=True)
        Path(self.path, name).write_text(text)
        self.head = self.commit('more')
        self.git('checkout', '-q', 'main')


class Fixture(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = Repository(self.folder)
        self.release_check = self.folder / 'release_check.py'
        self.release_check.write_text(FAKE_RELEASE_CHECK)
        server = ThreadingHTTPServer(('localhost', 0), GitHub)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.api = f'http://localhost:{server.server_port}'
        GitHub.requests = []
        self.body = 'Adds a feature.\n\nVerified-Run: https://github.com/me/app/actions/runs/7\n'
        self.run_record = {'path': WORKFLOW, 'event': 'workflow_dispatch', 'status': 'completed',
                           'conclusion': 'success', 'run_attempt': 1, 'html_url': 'https://github.com/me/app/actions/runs/7'}
        self.jobs = [{'id': 9, 'name': 'verify', 'conclusion': 'success', 'labels': ['self-hosted', 'coldbox']}]
        self.log = f'2026-11-09T02:33:40.1234567Z coldverify environment: {ENVIRONMENT}\r\n'
        self.token, self.missing = 'test-token', ()

    def serve(self):
        GitHub.routes = {
            '/repos/me/app/actions/runs/7': (200, json.dumps({**self.run_record, 'head_sha': self.repo.head})),
            '/repos/me/app/actions/runs/7/attempts/1/jobs?per_page=100': (200, json.dumps({'jobs': self.jobs})),
            '/repos/me/app/actions/jobs/9/logs': (302, self.api + '/blob/9.txt'),
            '/blob/9.txt': (200, self.log),
        }
        for path in self.missing:
            GitHub.routes.pop(path)

    def lane(self, event='pull_request', environment=ENVIRONMENT, actions='true'):
        self.repo.merge()
        self.serve()
        event_path = self.folder / 'event.json'
        event_path.write_text(json.dumps({'pull_request': {
            'body': self.body, 'title': 'Add a feature', 'head': {'sha': self.repo.head, 'ref': 'feature'},
            'base': {'sha': self.repo.git('rev-parse', 'main'), 'ref': 'main'}}}))
        env = {'GITHUB_ACTIONS': actions, 'GITHUB_EVENT_NAME': event, 'GITHUB_EVENT_PATH': str(event_path),
               'GITHUB_REPOSITORY': 'me/app', 'GITHUB_WORKFLOW_REF': f'me/app/{WORKFLOW}@refs/pull/1/merge',
               'GITHUB_JOB': 'verify', 'GITHUB_TOKEN': self.token, 'GH_TOKEN': '', 'GITHUB_API_URL': self.api}
        output = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(verify_fastlane, 'RELEASE_CHECK', self.release_check), \
                mock.patch.object(verify_fastlane, 'environment', return_value=environment), \
                contextlib.redirect_stdout(output):
            code = verify_fastlane.run(self.repo.path)
        return code, output.getvalue()

    def warn_only_on_main(self, text):
        Path(self.repo.path, '.fleet').mkdir(exist_ok=True)
        Path(self.repo.path, verify_fastlane.WARN_ONLY_PATHS).write_text(text)
        self.repo.commit('warn-only folders')

    def assert_full_suite(self, reason):
        code, output = self.lane()
        self.assertIsNone(code, output)
        self.assertEqual(output.splitlines()[-1], f'Full suite: {reason}.')


class FastLane(Fixture):
    def test_a_tree_that_passed_on_the_same_toolchain_passes_after_the_secret_scan(self):
        code, output = self.lane()
        tree = self.repo.git('rev-parse', 'HEAD^{tree}')
        self.assertEqual(code, 0, output)
        self.assertEqual(output.splitlines(), [
            'coldverify environment: ' + ENVIRONMENT,
            'Secret scan: PASS (release-check secret findings on the added lines)',
            'Lesson checks: PASS (L13 conflict markers on the added lines, L14 .worktrees/ ignored)',
            f'PASS: fast lane: tree {tree} already passed the full suite on this toolchain in '
            'https://github.com/me/app/actions/runs/7'])
        self.assertEqual(GitHub.requests, [
            ('/repos/me/app/actions/runs/7', 'Bearer test-token'),
            ('/repos/me/app/actions/runs/7/attempts/1/jobs?per_page=100', 'Bearer test-token'),
            ('/repos/me/app/actions/jobs/9/logs', 'Bearer test-token'),
            ('/blob/9.txt', None)])

    def test_a_description_without_a_verified_run_runs_the_full_suite(self):
        self.body = 'Adds a feature.\n'
        self.assert_full_suite('the PR description has no Verified-Run line')

    def test_two_verified_runs_run_the_full_suite(self):
        self.body += 'Verified-Run: https://github.com/me/app/actions/runs/8\n'
        self.assert_full_suite('the PR description has more than one Verified-Run line')

    def test_a_run_in_another_repository_runs_the_full_suite(self):
        self.body = 'Verified-Run: https://github.com/other/app/actions/runs/7\n'
        self.assert_full_suite('the verified run is in other/app, not this repository')

    def test_a_run_that_was_not_dispatched_runs_the_full_suite(self):
        self.run_record['event'] = 'push'
        self.assert_full_suite(f'run 7 is not a manual run of {WORKFLOW}')

    def test_a_run_of_another_workflow_runs_the_full_suite(self):
        self.run_record['path'] = '.github/workflows/other.yml'
        self.assert_full_suite(f'run 7 is not a manual run of {WORKFLOW}')

    def test_a_run_that_did_not_pass_runs_the_full_suite(self):
        self.run_record['conclusion'] = 'failure'
        self.assert_full_suite('run 7 did not pass')

    def test_a_run_whose_job_did_not_run_on_coldbox_runs_the_full_suite(self):
        self.jobs[0]['labels'] = ['ubuntu-latest']
        self.assert_full_suite('run 7 has no passing Coldbox verify job')

    def test_a_run_on_another_toolchain_runs_the_full_suite(self):
        self.log = '2026-11-09T02:33:40Z coldverify environment: ' + 'f' * 64 + '\n'
        self.assert_full_suite('run 7 ran on a different toolchain')

    def test_a_log_that_prints_a_second_environment_runs_the_full_suite(self):
        self.log += '2026-11-09T02:33:41Z coldverify environment: ' + 'f' * 64 + '\n'
        self.assert_full_suite('run 7 ran on a different toolchain')

    def test_a_merge_with_a_moved_main_runs_the_full_suite(self):
        verified = self.repo.git('rev-parse', self.repo.head + '^{tree}')
        (self.repo.path / 'README.md').write_text('main moved\n')
        self.repo.commit('main moved')
        code, output = self.lane()
        tree = self.repo.git('rev-parse', 'HEAD^{tree}')
        self.assertIsNone(code, output)
        self.assertEqual(output.splitlines()[-1], f'Full suite: run 7 verified tree {verified}, but this run tests {tree}.')

    def test_a_dirty_checkout_runs_the_full_suite(self):
        self.repo.merge()
        (self.repo.path / 'stray.txt').write_text('untracked\n')
        self.repo.merge = lambda: None
        code, output = self.lane()
        self.assertIsNone(code, output)
        self.assertTrue(output.splitlines()[-1].endswith('but this run tests a dirty tree.'), output)

    def test_an_api_error_runs_the_full_suite(self):
        self.missing = ['/repos/me/app/actions/runs/7/attempts/1/jobs?per_page=100']
        self.assert_full_suite('GitHub API /repos/me/app/actions/runs/7/attempts/1/jobs?per_page=100: '
                               'HTTP Error 404: Not Found')

    def test_a_missing_token_runs_the_full_suite(self):
        self.token = ''
        self.assert_full_suite('no GITHUB_TOKEN to look up the verified run')
        self.assertEqual(GitHub.requests, [])

    def test_an_added_secret_fails_without_printing_it(self):
        self.repo.change_feature('config/app.env', 'NAME=app\nTOKEN=hunter2-value-xyz\n')
        code, output = self.lane()
        self.assertEqual(code, 1, output)
        self.assertNotIn('hunter2', output)
        self.assertEqual(output.splitlines()[1:3], [
            'FAIL: secret scan: assigned-secret in config/app.env:2',
            'Secret scan: FAIL (release-check secret findings on the added lines)'])

    def test_an_added_line_that_looks_like_a_file_header_is_still_scanned(self):
        self.repo.change_feature('notes.txt', 'one\n++ b/elsewhere TOKEN=hunter2-value-xyz\n')
        code, output = self.lane()
        self.assertEqual(code, 1, output)
        self.assertIn('FAIL: secret scan: assigned-secret in notes.txt:2', output.splitlines())

    def test_a_diff_line_at_the_byte_cap_is_scanned_as_before(self):
        line = 'TOKEN=hunter2-value-xyz ' + 'x' * 40
        self.repo.change_feature('long.env', line + '\n')
        uncapped = self.lane()
        with mock.patch.object(verify_fastlane, 'DIFF_LINE_LIMIT', len(f'+{line}\n'), create=True):
            code, output = self.lane()
        self.assertEqual((code, output), uncapped)
        self.assertEqual(code, 1, output)
        self.assertEqual(output.splitlines()[1:3], [
            'FAIL: secret scan: assigned-secret in long.env:1',
            'Secret scan: FAIL (release-check secret findings on the added lines)'])

    def test_a_diff_line_over_the_byte_cap_makes_both_scans_incomplete(self):
        line = 'TOKEN=hunter2-value-xyz ' + 'x' * 40
        self.repo.change_feature('long.env', line + '\n')
        with mock.patch.object(verify_fastlane, 'DIFF_LINE_LIMIT', len(f'+{line}\n') - 1, create=True):
            code, output = self.lane()
        self.assertEqual(code, 2, output)
        self.assertNotIn('hunter2', output)
        self.assertEqual(output.splitlines()[1:], ['INCOMPLETE: secret scan could not run: ValueError',
                                                   'INCOMPLETE: lesson checks could not run: ValueError'])

    def test_a_removed_line_over_the_byte_cap_is_skipped_and_added_lines_are_still_scanned(self):
        (self.repo.path / 'big.json').write_text('[' + '1,' * 400 + '1]\n')
        self.repo.commit('big')
        self.repo.git('checkout', '-q', 'feature')
        self.repo.git('merge', '-q', 'main')
        self.repo.git('rm', '-q', 'big.json')
        self.repo.commit('remove')
        self.repo.git('checkout', '-q', 'main')
        self.repo.change_feature('new.env', 'TOKEN=hunter2-value-xyz\n')
        with mock.patch.object(verify_fastlane, 'DIFF_LINE_LIMIT', 100, create=True):
            code, output = self.lane()
        self.assertEqual(code, 1, output)
        self.assertEqual(output.splitlines()[1:3], [
            'FAIL: secret scan: assigned-secret in new.env:1',
            'Secret scan: FAIL (release-check secret findings on the added lines)'])

    def test_a_changed_line_over_the_byte_cap_still_makes_both_scans_incomplete(self):
        line = 'TOKEN=hunter2-value-xyz ' + 'x' * 40
        self.repo.change_feature('old.env', line + '\n')
        with mock.patch.object(verify_fastlane, 'DIFF_LINE_LIMIT', len(f'+{line}\n') - 1, create=True):
            code, output = self.lane()
        self.assertEqual(code, 2, output)
        self.assertEqual(output.splitlines()[1:], ['INCOMPLETE: secret scan could not run: ValueError',
                                                   'INCOMPLETE: lesson checks could not run: ValueError'])

    def test_a_secret_in_a_path_with_a_space_is_found(self):
        self.repo.change_feature('my notes.env', 'TOKEN=hunter2-value-xyz\n')
        code, output = self.lane()
        self.assertEqual(code, 1, output)
        self.assertIn('FAIL: secret scan: assigned-secret in my notes.env:1', output.splitlines())

    def test_an_added_credential_file_fails(self):
        self.repo.change_feature('deploy.pem', 'not really a key\n')
        code, output = self.lane()
        self.assertEqual(code, 1, output)
        self.assertIn('FAIL: secret scan: credential-file in deploy.pem:0', output.splitlines())

    def test_a_secret_already_on_main_does_not_fail_a_change_next_to_it(self):
        self.repo.change_feature('old.env', 'TOKEN=already-here-before\nNAME=app\n')
        code, output = self.lane()
        self.assertEqual(code, 0, output)

    def test_a_reviewed_finding_and_private_data_in_a_private_repo_pass(self):
        self.repo.change_feature('release-check.json', json.dumps({'approvals': [{'path': 'fixture.env'}]}))
        self.repo.change_feature('fixture.env', 'TOKEN=example-fixture-value\nADDRESS=192.0.2.1\n')
        code, output = self.lane()
        self.assertEqual(code, 0, output)

    def test_a_heuristic_finding_in_a_warn_only_folder_warns_and_passes(self):
        self.warn_only_on_main('Conversations/\nAgent-Space/\n')
        self.repo.change_feature('Agent-Space/Notes/a.md', 'one\nTOKEN=hunter2-value-xyz\n')
        code, output = self.lane()
        self.assertIsNone(code, output)
        self.assertNotIn('hunter2', output)
        self.assertEqual(output.splitlines()[1:3], [
            'WARN: secret scan: assigned-secret in Agent-Space/Notes/a.md:2 '
            '(heuristic; does not block in .fleet/secret-scan-warn-only folders)',
            'Secret scan: PASS (release-check secret findings on the added lines)'])

    def test_a_high_confidence_secret_in_a_warn_only_folder_still_fails(self):
        self.warn_only_on_main('Conversations/\n')
        self.repo.change_feature('Conversations/a.md', 'KEYBLOCK\nTOKEN=hunter2-value-xyz\n')
        code, output = self.lane()
        self.assertEqual(code, 1, output)
        self.assertEqual(output.splitlines()[1:], [
            'WARN: secret scan: assigned-secret in Conversations/a.md:2 '
            '(heuristic; does not block in .fleet/secret-scan-warn-only folders)',
            'FAIL: secret scan: private-key in Conversations/a.md:1',
            'Secret scan: FAIL (release-check secret findings on the added lines)',
            'Lesson checks: PASS (L13 conflict markers on the added lines, L14 .worktrees/ ignored)'])

    def test_a_heuristic_finding_outside_the_warn_only_folders_still_fails(self):
        self.warn_only_on_main('Conversations/\n')
        self.repo.change_feature('Conversations.md', 'TOKEN=hunter2-value-xyz\n')
        code, output = self.lane()
        self.assertEqual(code, 1, output)
        self.assertEqual(output.splitlines()[1:], [
            'FAIL: secret scan: assigned-secret in Conversations.md:1',
            'Secret scan: FAIL (release-check secret findings on the added lines)',
            'Lesson checks: PASS (L13 conflict markers on the added lines, L14 .worktrees/ ignored)'])

    def test_a_warn_only_list_added_by_the_pull_request_does_not_apply(self):
        self.repo.change_feature(verify_fastlane.WARN_ONLY_PATHS, 'Conversations/\n')
        self.repo.change_feature('Conversations/a.md', 'TOKEN=hunter2-value-xyz\n')
        code, output = self.lane()
        self.assertEqual(code, 1, output)
        self.assertIn('FAIL: secret scan: assigned-secret in Conversations/a.md:1', output.splitlines())

    def test_an_invalid_warn_only_list_is_incomplete(self):
        self.repo.change_feature('Conversations/a.md', 'TOKEN=hunter2-value-xyz\n')
        for text in ('Conversations\n', '../Conversations/\n', '/Conversations/\n', 'a/../b/\n', './\n'):
            with self.subTest(text=text):
                self.repo.git('checkout', '-q', 'main')
                self.warn_only_on_main(text)
                code, output = self.lane()
                self.assertEqual(code, 2, output)
                self.assertEqual(output.splitlines()[1:], ['INCOMPLETE: secret scan could not run: ValueError',
                                                           'INCOMPLETE: lesson checks could not run: ValueError'])

    def test_a_scan_that_cannot_run_is_incomplete(self):
        self.release_check = self.folder / 'missing/release_check.py'
        code, output = self.lane()
        self.assertEqual(code, 2, output)
        self.assertEqual(output.splitlines()[1], 'INCOMPLETE: secret scan could not run: FileNotFoundError')

    def test_an_unreadable_event_is_incomplete(self):
        self.repo.merge()
        with mock.patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'pull_request',
                                          'GITHUB_EVENT_PATH': str(self.folder / 'none.json')}), \
                mock.patch.object(verify_fastlane, 'environment', return_value=ENVIRONMENT), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = verify_fastlane.run(self.repo.path)
        self.assertEqual((code, output.getvalue().splitlines()[1:]),
                         (2, ['INCOMPLETE: pull request event unreadable: FileNotFoundError']))

    def test_a_dispatch_run_prints_the_environment_and_runs_the_full_suite(self):
        code, output = self.lane(event='workflow_dispatch')
        self.assertEqual((code, output), (None, 'coldverify environment: ' + ENVIRONMENT + '\n'))
        self.assertEqual(GitHub.requests, [])

    def test_outside_github_actions_nothing_happens(self):
        self.assertEqual(self.lane(actions=''), (None, ''))


class LessonChecks(Fixture):
    def test_studio_c26d49e_left_conflict_markers_in_a_merge_and_fails(self):
        self.repo.change_feature('studio_learn/selftest/loop.py', STUDIO_C26D49E_LOOP)
        code, output = self.lane()
        fix = "finish the merge and delete the marker lines; a test fixture builds them at run time, for example '<' * 7"
        self.assertEqual(code, 1, output)
        self.assertEqual(output.splitlines()[2:], [
            f'FAIL: L13 conflict marker in studio_learn/selftest/loop.py:3: {fix}',
            f'FAIL: L13 conflict marker in studio_learn/selftest/loop.py:7: {fix}',
            'Lesson checks: FAIL (L13 conflict markers on the added lines, L14 .worktrees/ ignored)'])

    def test_studio_8ed5a66_resolved_merge_and_heading_underlines_pass(self):
        self.repo.change_feature('studio_learn/selftest/loop.py', STUDIO_8ED5A66_LOOP)
        self.repo.change_feature('CHANGELOG.md', 'Changes\n' + '=' * 7 + '\n\nQuote:\n> ' + '>' * 6 + 'x\n')
        code, output = self.lane()
        self.assertEqual(code, 0, output)

    def test_a_diff3_base_marker_fails(self):
        self.repo.change_feature('notes.txt', 'one\n' + '|' * 7 + ' base\n')
        code, output = self.lane()
        self.assertEqual(code, 1, output)
        self.assertIn('FAIL: L13 conflict marker in notes.txt:2', output)

    def test_markers_after_a_bare_carriage_return_and_in_crlf_files_fail(self):
        self.repo.change_feature('mac.txt', 'header\rcontinuation\n' + '<' * 7 + ' HEAD\n')
        self.repo.change_feature('windows.txt', 'one\r\n' + '>' * 7 + '\r\n')
        code, output = self.lane()
        self.assertEqual(code, 1, output)
        self.assertIn('FAIL: L13 conflict marker in mac.txt:2', output)
        self.assertIn('FAIL: L13 conflict marker in windows.txt:2', output)

    def test_a_marker_already_on_main_does_not_fail_a_change_next_to_it(self):
        (self.repo.path / 'legacy.txt').write_text('<' * 7 + ' HEAD\n')
        self.repo.commit('legacy marker')
        self.repo.git('checkout', '-q', 'feature')
        self.repo.git('merge', '-q', 'main')
        self.repo.git('checkout', '-q', 'main')
        self.repo.change_feature('legacy.txt', '<' * 7 + ' HEAD\nnew line\n')
        code, output = self.lane()
        self.assertEqual(code, 0, output)

    def test_a_quoted_marker_in_a_warn_only_folder_warns(self):
        self.warn_only_on_main('Conversations/\n')
        self.repo.change_feature('Conversations/a.md', STUDIO_C26D49E_LOOP)
        self.repo.change_feature('loop.py', STUDIO_C26D49E_LOOP)
        code, output = self.lane()
        self.assertEqual(code, 1, output)
        self.assertEqual(output.splitlines()[2:6], [
            'WARN: L13 conflict marker in Conversations/a.md:3 (quoted text; does not block in '
            '.fleet/secret-scan-warn-only folders)',
            'WARN: L13 conflict marker in Conversations/a.md:7 (quoted text; does not block in '
            '.fleet/secret-scan-warn-only folders)',
            'FAIL: L13 conflict marker in loop.py:3: finish the merge and delete the marker lines; '
            "a test fixture builds them at run time, for example '<' * 7",
            'FAIL: L13 conflict marker in loop.py:7: finish the merge and delete the marker lines; '
            "a test fixture builds them at run time, for example '<' * 7"])

    def test_studio_fa470933_gitignore_without_worktrees_fails(self):
        self.repo.change_feature('.gitignore', STUDIO_FA470933_GITIGNORE)
        code, output = self.lane()
        self.assertEqual(code, 1, output)
        self.assertEqual(output.splitlines()[2:], [
            "FAIL: L14 .gitignore does not ignore .worktrees/: add the line /.worktrees/ to the repository's .gitignore",
            'Lesson checks: FAIL (L13 conflict markers on the added lines, L14 .worktrees/ ignored)'])

    def test_studio_fe3286c_gitignore_with_worktrees_passes(self):
        self.repo.change_feature('.gitignore', STUDIO_FE3286C_GITIGNORE)
        code, output = self.lane()
        self.assertEqual(code, 0, output)

    def test_a_global_exclude_does_not_count_as_the_repository_ignoring_worktrees(self):
        self.repo.change_feature('.gitignore', STUDIO_FA470933_GITIGNORE)
        excludes = self.folder / 'global-excludes'
        excludes.write_text('.worktrees/\n')
        config = self.folder / 'global-config'
        config.write_text(f'[core]\n\texcludesFile = {excludes}\n')
        with mock.patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(config)}):
            code, output = self.lane()
        self.assertEqual(code, 1, output)
        self.assertIn('FAIL: L14 .gitignore does not ignore .worktrees/', output)

    def test_a_local_info_exclude_does_not_count_as_the_repository_ignoring_worktrees(self):
        self.repo.change_feature('.gitignore', STUDIO_FA470933_GITIGNORE)
        Path(self.repo.path, '.git', 'info').mkdir(exist_ok=True)
        Path(self.repo.path, '.git', 'info', 'exclude').write_text('.worktrees/\n')
        code, output = self.lane()
        self.assertEqual(code, 1, output)
        self.assertIn('FAIL: L14 .gitignore does not ignore .worktrees/', output)

    def test_checks_that_cannot_run_are_incomplete(self):
        with mock.patch.object(verify_fastlane, 'worktrees_ignored', side_effect=OSError):
            code, output = self.lane()
        self.assertEqual(code, 2, output)
        self.assertEqual(output.splitlines()[2:], ['INCOMPLETE: lesson checks could not run: OSError'])


class DocsOnly(Fixture):
    def branch(self, changes, on_main=None):
        self.repo.git('checkout', '-q', 'main')
        if on_main:
            for path, text in on_main.items():
                Path(self.repo.path, path).parent.mkdir(parents=True, exist_ok=True)
                Path(self.repo.path, path).write_text(text)
            self.repo.commit('main')
        self.repo.git('checkout', '-q', '-B', 'feature', 'main')
        for path, text in changes.items():
            if text is None:
                self.repo.git('rm', '-q', path)
            else:
                Path(self.repo.path, path).parent.mkdir(parents=True, exist_ok=True)
                Path(self.repo.path, path).write_text(text)
        self.repo.head = self.repo.commit('docs')
        self.repo.git('checkout', '-q', 'main')

    def docs(self, event='pull_request', actions='true'):
        self.repo.merge()
        event_path = self.folder / 'event.json'
        event_path.write_text(json.dumps({'pull_request': {
            'body': '', 'head': {'sha': self.repo.head}, 'base': {'sha': self.repo.git('rev-parse', 'main')}}}))
        env = {'GITHUB_ACTIONS': actions, 'GITHUB_EVENT_NAME': event, 'GITHUB_EVENT_PATH': str(event_path)}
        with mock.patch.dict(os.environ, env):
            return verify_fastlane.docs_only(self.repo.path)

    def test_markdown_that_nothing_names_is_docs_only(self):
        self.branch({'guide/setup.md': 'Setup.\n', 'NOTES.md': 'notes\n'}, on_main={'guide/old.md': 'old\n'})
        self.assertEqual(self.docs(), ['NOTES.md', 'guide/setup.md'])

    def test_a_deleted_markdown_file_counts_as_a_change(self):
        self.branch({'guide/setup.md': None}, on_main={'guide/setup.md': 'Setup.\n'})
        self.assertEqual(self.docs(), ['guide/setup.md'])

    def test_one_code_path_runs_the_full_suite(self):
        self.branch({'NOTES.md': 'notes\n', 'app.py': 'print(1)\n'})
        self.assertIsNone(self.docs())

    def test_code_renamed_into_markdown_runs_the_full_suite(self):
        self.branch({'tool.py': None, 'tool.md': 'print(1)\n'}, on_main={'tool.py': 'print(1)\n'})
        self.assertIsNone(self.docs())

    def test_a_readme_named_by_a_test_runs_the_full_suite(self):
        pin = 'from pathlib import Path\nassert "readme" in Path("README.md").read_text()\n'
        self.branch({'README.md': 'readme, reworded\n'}, on_main={'test_readme.py': pin})
        self.assertIsNone(self.docs())

    def test_a_name_added_on_main_after_the_fork_runs_the_full_suite(self):
        self.branch({'NOTES.md': 'more notes\n'}, on_main={'NOTES.md': 'notes\n'})
        self.repo.git('checkout', '-q', 'main')
        Path(self.repo.path, 'check.py').write_text('open("NOTES.md")\n')
        self.repo.commit('reads notes')
        self.assertIsNone(self.docs())

    def test_a_folder_named_by_code_runs_the_full_suite(self):
        for reader in ('glob("prompts/*.md")\n', "ROOT / 'prompts'\n", 'ROOT / "prompts"\n'):
            with self.subTest(reader=reader):
                self.branch({'prompts/ask.md': 'Ask.\n'}, on_main={'load.py': reader})
                self.assertIsNone(self.docs())

    def test_another_markdown_file_naming_it_is_still_docs_only(self):
        self.branch({'guide/setup.md': 'Setup.\n'}, on_main={'INDEX.md': 'See guide/setup.md\n'})
        self.assertEqual(self.docs(), ['guide/setup.md'])

    def test_markdown_in_test_and_ci_folders_runs_the_full_suite(self):
        for folder in sorted(verify_fastlane.NOT_DOCS_FOLDERS):
            with self.subTest(folder=folder):
                self.branch({f'pkg/{folder}/case.md': 'expected\n'})
                self.assertIsNone(self.docs())

    def test_a_submodule_update_beside_markdown_runs_the_full_suite(self):
        self.branch({'NOTES.md': 'notes\n'})
        self.repo.git('checkout', '-q', 'feature')
        self.repo.git('config', 'diff.ignoreSubmodules', 'all')
        self.repo.git('update-index', '--add', '--cacheinfo', f'160000,{self.repo.base},vendor/lib.md')
        self.repo.git('commit', '-q', '-m', 'bump')
        self.repo.head = self.repo.git('rev-parse', 'HEAD')
        self.repo.git('checkout', '-q', 'main')
        self.assertIsNone(self.docs())

    def test_an_uppercase_md_suffix_is_not_markdown(self):
        self.branch({'PROGRAM.MD': 'run\n'})
        self.assertIsNone(self.docs())

    def test_a_reader_with_an_uppercase_md_suffix_still_names_the_file(self):
        self.branch({'NOTES.md': 'more\n'}, on_main={'NOTES.md': 'notes\n', 'reader.MD': 'open("NOTES.md")\n'})
        self.assertIsNone(self.docs())

    def test_an_executable_or_symlinked_markdown_file_runs_the_full_suite(self):
        self.branch({'NOTES.md': 'notes\n'})
        self.repo.git('checkout', '-q', 'feature')
        os.chmod(self.repo.path / 'NOTES.md', 0o755)
        self.repo.head = self.repo.commit('mode')
        self.repo.git('checkout', '-q', 'main')
        self.assertIsNone(self.docs())
        self.branch({'NOTES.md': 'notes\n'})
        self.repo.git('checkout', '-q', 'feature')
        Path(self.repo.path, 'tool.md').symlink_to('feature.py')
        self.repo.head = self.repo.commit('link')
        self.repo.git('checkout', '-q', 'main')
        self.assertIsNone(self.docs())

    def test_paths_with_spaces_unicode_and_pathspec_magic_are_read_as_plain_names(self):
        changes = {'guide/set up.md': 'a\n', 'guide/café.md': 'b\n', ':(glob)x.md': 'c\n'}
        self.branch(changes)
        self.assertEqual(self.docs(), sorted(changes))

    def test_only_a_pull_request_in_github_actions_can_be_docs_only(self):
        self.branch({'NOTES.md': 'notes\n'})
        self.assertIsNone(self.docs(event='push'))
        self.assertIsNone(self.docs(actions='false'))

    def test_a_git_error_runs_the_full_suite(self):
        self.branch({'NOTES.md': 'notes\n'})
        with mock.patch.object(verify_fastlane, 'named_outside_markdown',
                               side_effect=subprocess.CalledProcessError(128, 'git grep')):
            self.assertIsNone(self.docs())

    def test_an_unreadable_event_runs_the_full_suite(self):
        self.branch({'NOTES.md': 'notes\n'})
        self.repo.merge()
        with mock.patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'pull_request',
                                          'GITHUB_EVENT_PATH': str(self.folder / 'missing.json')}):
            self.assertIsNone(verify_fastlane.docs_only(self.repo.path))


class Environment(unittest.TestCase):
    def test_the_environment_hash_is_stable_and_follows_coldstack(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {'COLDSTACK_ROOT': folder}):
            Path(folder, 'tool.py').write_text('one\n')
            first, again = verify_fastlane.environment(), verify_fastlane.environment()
            Path(folder, 'tool.py').write_text('two\n')
            changed = verify_fastlane.environment()
        self.assertRegex(first, '^[0-9a-f]{64}$')
        self.assertEqual(first, again)
        self.assertNotEqual(first, changed)


class Command(Fixture):
    def installed(self, recipe_change=None):
        install = self.folder / 'opt'
        shutil.copytree(ROOT, install / 'coldverify', ignore=shutil.ignore_patterns(
            '.git', '.worktrees', '.cold', '__pycache__', 'build', 'dist', '*.egg-info'))
        (install / 'release-check').mkdir()
        (install / 'release-check/release_check.py').write_text(FAKE_RELEASE_CHECK)
        self.repo.change_feature('.fleet/verify.sh', f'touch {self.folder}/recipe-ran\n')
        if recipe_change:
            self.repo.change_feature(*recipe_change)
        self.repo.merge()
        self.repo.merge = lambda: None
        self.serve()
        event_path = self.folder / 'event.json'
        event_path.write_text(json.dumps({'pull_request': {
            'number': 1, 'body': self.body, 'title': 'Add a feature', 'head': {'sha': self.repo.head, 'ref': 'feature'},
            'base': {'sha': self.repo.git('rev-parse', 'main'), 'ref': 'main'}}}))
        env = {key: value for key, value in os.environ.items() if not key.startswith(('GIT_', 'GITHUB_'))}
        env.update({'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'pull_request', 'GITHUB_EVENT_PATH': str(event_path),
                    'GITHUB_REPOSITORY': 'me/app', 'GITHUB_WORKFLOW_REF': f'me/app/{WORKFLOW}@refs/pull/1/merge',
                    'GITHUB_JOB': 'verify', 'GITHUB_TOKEN': 'test-token', 'GITHUB_API_URL': self.api,
                    'GITHUB_HEAD_REF': 'feature', 'COLDSTACK_ROOT': str(self.folder / 'no-coldstack')})
        return install, env

    def test_a_strict_ci_run_fails_on_an_added_secret_before_the_suite(self):
        install, env = self.installed(('config/app.env', 'NAME=app\nTOKEN=hunter2-value-xyz\n'))
        result = subprocess.run([sys.executable, str(install / 'coldverify/coldverify'), '--ci'], cwd=self.repo.path, env=env,
                                text=True, capture_output=True, timeout=300)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('FAIL: secret scan: assigned-secret in config/app.env:2', result.stdout)
        self.assertNotIn('hunter2', result.stdout + result.stderr)
        self.assertFalse((self.folder / 'recipe-ran').exists())

    def test_a_strict_ci_run_of_a_docs_only_change_passes_and_says_the_tests_did_not_run(self):
        install, env = self.installed()
        self.repo.git('checkout', '-q', '-B', 'feature', 'main')
        Path(self.repo.path, 'NOTES.md').write_text('notes\n')
        self.repo.head = self.repo.commit('docs')
        self.repo.git('checkout', '-q', '--detach', 'main')
        self.repo.git('merge', '-q', '--no-ff', '-m', 'merge', self.repo.head)
        Path(self.folder, 'event.json').write_text(json.dumps({'pull_request': {
            'number': 1, 'body': self.body, 'title': 'Notes', 'head': {'sha': self.repo.head, 'ref': 'feature'},
            'base': {'sha': self.repo.git('rev-parse', 'main'), 'ref': 'main'}}}))
        result = subprocess.run([sys.executable, str(install / 'coldverify/coldverify'), '--ci'], cwd=self.repo.path, env=env,
                                text=True, capture_output=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Secret scan: PASS', result.stdout)
        self.assertIn('Lesson checks: PASS', result.stdout)
        self.assertIn('COLDVERIFY CI PASS: docs only (1 .md file(s)', result.stdout)
        self.assertIn('THE TESTS DID NOT RUN', result.stdout)
        self.assertFalse((self.folder / 'recipe-ran').exists())
        self.assertFalse((self.repo.path / '.cold/verify-receipt.json').exists())

    def test_coldverify_compat_ci_takes_the_fast_lane_without_running_the_recipe(self):
        install, env = self.installed()
        command = [sys.executable, str(install / 'coldverify/coldverify'), '--compat', '--ci']
        probe = 'import sys; sys.path.insert(0, sys.argv[1]); import verify_fastlane; print(verify_fastlane.environment())'
        current = subprocess.run([sys.executable, '-c', probe, str(install / 'coldverify/ci')], env=env, text=True,
                                 capture_output=True, check=True, timeout=300).stdout.strip()
        self.log = f'2026-11-09T02:33:40Z coldverify environment: {current}\n'
        self.serve()
        result = subprocess.run(command, cwd=self.repo.path, env=env, text=True, capture_output=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('PASS: fast lane: tree ' + self.repo.git('rev-parse', 'HEAD^{tree}'), result.stdout)
        self.assertFalse((self.folder / 'recipe-ran').exists())


if __name__ == '__main__':
    unittest.main()
