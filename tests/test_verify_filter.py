import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
import unittest.mock

import criteria_shim
import repo_checkout

ROOT = repo_checkout.root()
sys.path.insert(0, str(ROOT / 'ci'))
import verify_filter
from verify_filter import Change, Decision, Filter

IDENTITY = ['-c', 'user.name=t', '-c', 'user.email=t@localhost']
TESTS = ('tests/test_a.py', 'tests/test_b.py', 'tests/test_c.py')
TRUSTED = Change(('src/app.py',), 'diff text', True)


def skip_model(test, change):
    return Decision(False, 'unrelated to the diff', tokens=10, cost=0.01)


def model(function):
    return (*verify_filter.FILTERS[:-1], Filter('luna', function, model=True))


def no_opinion(test, change):
    return None


OFFLINE = model(no_opinion)


def verdicts(change, function):
    plan, usage = verify_filter.select(TESTS, change, model(function))
    return {verdict.test: verdict for verdict in plan}, usage


class Boom(Exception):
    pass


FAKE_CODEX = '''#!/usr/bin/env python3
import json, os, re, sys
if sys.argv[1] == 'sandbox':
    with open(os.environ['FAKE_CODEX_LOG'], 'a') as log:
        log.write(json.dumps({'probe': sys.argv[2:], 'cwd': os.getcwd()}) + '\\n')
    if os.environ.get('FAKE_CODEX_LEAKS'):
        command = sys.argv[sys.argv.index('--') + 1:]
        os.execvp(command[0], command)
    sys.exit(0)
prompt = sys.stdin.read()
with open(os.environ['FAKE_CODEX_LOG'], 'a') as log:
    log.write(json.dumps({'argv': sys.argv[1:], 'prompt': prompt, 'github_token': 'GH_TOKEN' in os.environ}) + '\\n')
if os.environ.get('FAKE_CODEX_FAIL'):
    sys.exit(3)
test = re.search(r'^Test file: (\\S+)', prompt, re.M).group(1)
table = json.loads(os.environ.get('FAKE_CODEX_PROBABILITIES', '{}'))
reply = table.get(test, os.environ.get('FAKE_CODEX_REPLY', '{"probability": 0.9, "reason": "could reach it"}'))
if isinstance(reply, float):
    reply = json.dumps({'probability': reply, 'reason': 'canned'})
print(json.dumps({'type': 'thread.started', 'thread_id': 'x'}))
print(json.dumps({'type': 'item.completed', 'item': {'id': 'i', 'type': 'agent_message', 'text': reply}}))
print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1000, 'cached_input_tokens': 400, 'output_tokens': 20}}))
'''


class FakeCodex:
    def __init__(self, case):
        directory = tempfile.TemporaryDirectory()
        case.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.log = self.directory / 'calls.jsonl'
        binary = self.directory / 'codex'
        binary.write_text(FAKE_CODEX)
        binary.chmod(0o755)

    def environ(self, **extra):
        return {'PATH': f'{self.directory}{os.pathsep}{os.environ["PATH"]}', 'FAKE_CODEX_LOG': str(self.log), **extra}

    def logged(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def calls(self):
        return [entry for entry in self.logged() if 'argv' in entry]

    def probes(self):
        return [entry for entry in self.logged() if 'probe' in entry]


class Selection(unittest.TestCase):
    def test_a_model_with_no_opinion_runs_everything(self):
        plan, usage = verify_filter.select(TESTS, TRUSTED, OFFLINE)
        self.assertTrue(all(verdict.run for verdict in plan))
        self.assertEqual(usage.model_calls, 3)

    def test_the_default_model_filter_is_luna(self):
        self.assertEqual((verify_filter.FILTERS[-1].name, verify_filter.FILTERS[-1].filter, verify_filter.FILTERS[-1].model),
                         ('luna', verify_filter.luna, True))

    def test_model_skip_names_the_filter_and_gives_a_reason(self):
        plan, usage = verdicts(TRUSTED, skip_model)
        for verdict in plan.values():
            self.assertEqual((verdict.run, verdict.filter, verdict.reason), (False, 'luna', 'unrelated to the diff'))
        self.assertEqual((usage.calls, usage.model_calls, usage.tokens), (12, 3, 30))
        self.assertAlmostEqual(usage.cost, 0.03)

    def test_changed_test_file_runs_even_when_the_model_would_skip(self):
        change = Change(('tests/test_b.py',), 'diff', True)
        plan, usage = verdicts(change, skip_model)
        self.assertEqual((plan['tests/test_b.py'].run, plan['tests/test_b.py'].filter), (True, 'changed-test-file'))
        self.assertFalse(plan['tests/test_a.py'].run)
        self.assertEqual(usage.model_calls, 2)

    def test_config_and_lockfile_changes_run_everything(self):
        for name in ('package-lock.json', 'pyproject.toml', 'requirements-dev.txt', '.fleet/verify.sh',
                     '.github/workflows/ci.yml', 'sub/conftest.py', 'go.sum'):
            with self.subTest(name):
                plan, usage = verdicts(Change((name, 'src/app.py'), 'diff', True), skip_model)
                self.assertTrue(all(v.run and v.filter == 'config-change' for v in plan.values()))
                self.assertEqual(usage.model_calls, 0)

    def test_unknown_changes_run_everything(self):
        plan, usage = verdicts(Change(None, None, True), skip_model)
        self.assertTrue(all(v.run and v.filter == 'changes-unknown' for v in plan.values()))
        self.assertEqual(usage.model_calls, 0)

    def test_untrusted_diff_never_calls_the_model(self):
        calls = []
        plan, usage = verdicts(Change(('src/app.py',), 'diff', False), lambda test, change: calls.append(test))
        self.assertEqual(calls, [])
        self.assertTrue(all(v.run and v.filter == 'luna' and 'untrusted' in v.reason for v in plan.values()))
        self.assertEqual(usage.model_calls, 0)

    def test_oversized_or_missing_diff_never_calls_the_model(self):
        for change in (Change(('src/app.py',), None, True, oversized=True), Change(('src/app.py',), None, True)):
            with self.subTest(change):
                calls = []
                plan, usage = verdicts(change, lambda test, change: calls.append(test))
                self.assertEqual(calls, [])
                self.assertTrue(all(v.run and 'larger than' in v.reason for v in plan.values()))

    def test_model_stops_after_three_failures_and_the_rest_run(self):
        calls = []

        def failing(test, change):
            calls.append(test)
            raise Boom('timeout')

        tests = tuple(f'tests/test_{n}.py' for n in range(6))
        plan, usage = verify_filter.select(tests, TRUSTED, model(failing))
        self.assertEqual(len(calls), 3)
        self.assertEqual(usage.failures, 3)
        self.assertTrue(all(v.run for v in plan))
        self.assertIn('Boom: timeout', plan[0].reason)
        self.assertIn('stopped after 3 failures', plan[3].reason)

    def test_a_good_call_does_not_reset_the_failure_count_below_three(self):
        results = iter([Boom(), Decision(False, 'unrelated'), Boom(), Boom()])

        def flaky(test, change):
            result = next(results)
            if isinstance(result, Exception):
                raise result
            return result

        tests = tuple(f'tests/test_{n}.py' for n in range(5))
        plan, usage = verify_filter.select(tests, TRUSTED, model(flaky))
        self.assertEqual([v.run for v in plan], [True, False, True, True, True])
        self.assertEqual(usage.failures, 3)

    def test_bad_filter_output_fails_open(self):
        for name, function in (('wrong type', lambda test, change: 'skip'),
                               ('non-boolean run', lambda test, change: Decision(None, 'unrelated')),
                               ('falsy non-boolean run', lambda test, change: Decision(0, 'unrelated')),
                               ('non-string reason', lambda test, change: Decision(True, None)),
                               ('skip without reason', lambda test, change: Decision(False, '  '))):
            with self.subTest(name):
                plan, usage = verdicts(TRUSTED, function)
                self.assertTrue(all(v.run for v in plan.values()))
                self.assertEqual(usage.failures, 3)
                self.assertEqual(usage.tokens, 0)

    def test_a_raising_rule_filter_fails_open_without_counting_as_a_model_failure(self):
        def broken(test, change):
            raise Boom('x')

        plan, usage = verify_filter.select(TESTS, TRUSTED, (Filter('broken', broken), *model(skip_model)[3:]))
        self.assertTrue(all(v.run and v.filter == 'broken' for v in plan))
        self.assertEqual(usage.failures, 0)

    def test_filters_must_have_unique_names_and_models_last(self):
        with self.assertRaises(ValueError):
            verify_filter.check_order((Filter('m', skip_model, model=True), Filter('rule', skip_model)))
        with self.assertRaises(ValueError):
            verify_filter.check_order((Filter('a', skip_model), Filter('a', skip_model)))
        verify_filter.check_order(verify_filter.FILTERS)

    def test_first_decision_wins_and_later_filters_are_not_called(self):
        calls = []
        tracked = (Filter('first', lambda test, change: Decision(False, 'first says skip')),
                   Filter('second', lambda test, change: calls.append(test)))
        plan, _ = verify_filter.select(TESTS, TRUSTED, tracked)
        self.assertEqual((plan[0].run, plan[0].filter), (False, 'first'))
        self.assertEqual(calls, [])


class Trust(unittest.TestCase):
    def event(self, payload):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name, 'event.json')
        path.write_text(json.dumps(payload))
        return str(path)

    def test_local_runs_are_trusted_unless_marked(self):
        self.assertTrue(verify_filter.trusted({}))
        self.assertFalse(verify_filter.trusted({'COLD_VERIFY_UNTRUSTED': '1'}))

    def test_github_push_is_trusted(self):
        self.assertTrue(verify_filter.trusted({'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'push',
                                               'GITHUB_EVENT_PATH': self.event({})}))

    def test_github_pull_request_trust_depends_on_the_head_repo(self):
        def pull(head):
            return {'pull_request': {'head': {'repo': {'full_name': head}}, 'base': {'repo': {'full_name': 'org/repo'}}}}

        env = {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'pull_request'}
        self.assertTrue(verify_filter.trusted({**env, 'GITHUB_EVENT_PATH': self.event(pull('org/repo'))}))
        self.assertFalse(verify_filter.trusted({**env, 'GITHUB_EVENT_PATH': self.event(pull('fork/repo'))}))

    def test_github_pull_request_without_repo_names_is_untrusted(self):
        env = {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'pull_request'}
        for name in (None, ''):
            with self.subTest(name):
                pull = {'pull_request': {'head': {'repo': {'full_name': name}}, 'base': {'repo': {'full_name': name}}}}
                self.assertFalse(verify_filter.trusted({**env, 'GITHUB_EVENT_PATH': self.event(pull)}))

    def test_github_with_an_unreadable_or_odd_event_is_untrusted(self):
        env = {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'pull_request'}
        self.assertFalse(verify_filter.trusted(env))
        self.assertFalse(verify_filter.trusted({**env, 'GITHUB_EVENT_PATH': '/nonexistent/event.json'}))
        self.assertFalse(verify_filter.trusted({**env, 'GITHUB_EVENT_PATH': self.event({'pull_request': None})}))

    def test_untrusted_marker_wins_over_a_trusted_github_push(self):
        self.assertFalse(verify_filter.trusted({'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'push',
                                                'GITHUB_EVENT_PATH': self.event({}), 'COLD_VERIFY_UNTRUSTED': '1'}))


class Repo(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name, 'repo')
        (self.repo / 'tests').mkdir(parents=True)
        self.git('init', '-q', '-b', 'main')
        for name in 'abc':
            self.write(f'tests/test_{name}.py', 'import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n')
        self.write('src.py', 'x = 1\n')
        self.commit('first')
        self.git('checkout', '-q', '-b', 'feature')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *IDENTITY, *args], text=True, capture_output=True, check=True).stdout.strip()

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self, message):
        self.git('add', '-A')
        self.git('commit', '-q', '-m', message)

    def environ(self, change, **extra):
        path = Path(self.temp.name, 'changes.json')
        verify_filter.write_change(change, path)
        return {verify_filter.CHANGES_ENV: str(path), **extra}

    def run_filter(self, change, filters=OFFLINE, explain=False, suites=('tests/test_*.py',), environ=None):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = verify_filter.run(list(suites), [sys.executable, '-m', 'unittest', 'discover', '-s', '{dir}', '-p', '{name}'],
                                     self.repo, environ if environ is not None else self.environ(change), explain, filters)
        return code, output.getvalue()


class LunaDecision(Repo):
    def decide(self, reply, name='tests/test_a.py', change=TRUSTED):
        self.write(name, 'import unittest\n')
        with unittest.mock.patch.object(verify_filter, 'ask_model', return_value=(reply, 1000, 400, 20)), contextlib.chdir(self.repo):
            return verify_filter.luna(name, change)

    def test_only_a_probability_under_the_threshold_skips(self):
        for probability, run in ((0.0, False), (0.05, False), (0.19, False), (0.2, True), (0.5, True), (1.0, True)):
            decision = self.decide(json.dumps({'probability': probability, 'reason': 'because'}))
            self.assertEqual(decision.run, run, probability)

    def test_the_reason_and_probability_are_reported_with_tokens(self):
        decision = self.decide('{"probability": 0.05, "reason": " docs only "}')
        self.assertEqual(decision.reason, 'rated 0.05 likely affected: docs only')
        self.assertEqual(decision.tokens, 1020)

    def test_a_bad_reply_raises_so_the_runner_fails_open(self):
        for reply in ('not json', '{}', '{"probability": 0.1}', '{"probability": 1.5, "reason": "x"}', '{"probability": -0.1, "reason": "x"}',
                      '{"probability": "0.1", "reason": "x"}', '{"probability": true, "reason": "x"}', '{"probability": 0.1, "reason": " "}'):
            with self.assertRaises(Exception, msg=reply):
                self.decide(reply)

    def test_a_bad_reply_runs_the_test_through_select(self):
        self.write('tests/test_a.py', 'x = 1\n')
        fake = FakeCodex(self)
        with unittest.mock.patch.dict(os.environ, fake.environ(FAKE_CODEX_REPLY='{"probability": 9, "reason": "x"}')), contextlib.chdir(self.repo):
            plan, usage = verify_filter.select(('tests/test_a.py',), TRUSTED)
        self.assertEqual((plan[0].run, plan[0].filter, usage.failures), (True, 'luna', 1))
        self.assertIn('ValueError', plan[0].reason)

    def test_a_huge_test_file_runs_without_asking_the_model(self):
        self.write('tests/test_a.py', '#' * (verify_filter.MAX_TEST_BYTES + 1))
        with unittest.mock.patch.object(verify_filter, 'ask_model', side_effect=AssertionError('asked')), contextlib.chdir(self.repo):
            self.assertTrue(verify_filter.luna('tests/test_a.py', TRUSTED).run)


class LunaCall(Repo):
    def ask(self, **extra):
        fake = FakeCodex(self)
        with unittest.mock.patch.dict(os.environ, fake.environ(GH_TOKEN='secret', **extra)):
            self.write('tests/test_a.py', 'import unittest\n')
            with contextlib.chdir(self.repo):
                return fake, verify_filter.luna('tests/test_a.py', Change(('src/app.py',), 'THE-DIFF', True))

    def test_the_prompt_carries_the_diff_and_the_test_and_the_call_is_read_only_and_tokenless(self):
        fake, decision = self.ask()
        self.assertTrue(decision.run)
        (call,) = fake.calls()
        self.assertIn('THE-DIFF', call['prompt'])
        self.assertIn('Test file: tests/test_a.py', call['prompt'])
        self.assertIn('import unittest', call['prompt'])
        argv = call['argv']
        self.assertNotIn('--sandbox', argv)
        work = argv[argv.index('-C') + 1]
        schema = argv[argv.index('--output-schema') + 1]
        self.assertEqual(criteria_shim.filesystem(argv), {':minimal': 'read', os.path.realpath(fake.directory / 'codex'): 'read',
                                                          work: 'read', '/etc': 'none'})
        (probe,) = fake.probes()
        self.assertEqual(probe['probe'], [*argv[argv.index('--model') + 2:argv.index('-c')], '--', 'sh', '-c',
                                          'test -r "$1" && ! test -r "$2"', 'probe', work, schema])
        self.assertEqual(probe['cwd'], os.path.realpath(work))
        self.assertIn('--ephemeral', call['argv'])
        self.assertEqual(call['argv'][call['argv'].index('--model') + 1], json.loads(verify_filter.MODEL_ROUTE.read_text())['model'])
        self.assertFalse(call['github_token'])
        self.assertEqual(decision.tokens, 1020)

    def test_a_model_sandbox_that_lets_a_file_outside_its_folder_be_read_means_every_test_runs_and_none_is_asked(self):
        fake = FakeCodex(self)
        reply = json.dumps({'probability': 0.0, 'reason': 'unrelated'})
        with unittest.mock.patch.dict(os.environ, fake.environ(FAKE_CODEX_LEAKS='1', FAKE_CODEX_REPLY=reply)):
            for test in TESTS:
                self.write(test, 'import unittest\n')
            with contextlib.chdir(self.repo):
                plan, usage = verify_filter.select(TESTS, TRUSTED)
        self.assertEqual([verdict.run for verdict in plan], [True, True, True])
        for verdict in plan:
            self.assertIn('could not prove the model sandbox confines reads to its scratch folder: codex sandbox exited 1',
                          verdict.reason)
        self.assertEqual(usage.failures, 3)
        self.assertEqual(fake.calls(), [])

    def test_a_failing_codex_call_raises(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.ask(FAKE_CODEX_FAIL='1')

    def test_cost_is_priced_only_when_rates_are_given(self):
        self.assertEqual(verify_filter.model_cost(600, 400, 20, {}), 0.0)
        self.assertAlmostEqual(verify_filter.model_cost(600, 400, 20, {verify_filter.RATES_ENV: '1,0.5,4'}), (600 + 200 + 80) / 1_000_000)
        fake, decision = self.ask(**{verify_filter.RATES_ENV: '1,0.5,4'})
        self.assertAlmostEqual(decision.cost, 880 / 1_000_000)


class Collect(Repo):
    def test_reports_committed_modified_and_untracked_files_against_main(self):
        self.write('src.py', 'x = 2\n')
        self.commit('edit')
        self.write('tests/test_a.py', 'changed = 1\n')
        self.write('tests/test_new.py', 'new = 1\n')
        change = verify_filter.collect_change(self.repo, {})
        self.assertEqual(change.files, ('src.py', 'tests/test_a.py', 'tests/test_new.py'))
        self.assertIn('+x = 2', change.diff)
        self.assertTrue(change.trusted)
        self.assertFalse(change.oversized)

    def test_no_changes_is_an_empty_known_set(self):
        self.assertEqual(verify_filter.collect_change(self.repo, {}).files, ())

    def test_no_base_branch_means_unknown(self):
        self.git('branch', '-m', 'main', 'trunk')
        change = verify_filter.collect_change(self.repo, {})
        self.assertEqual((change.files, change.diff), (None, None))

    def test_oversized_diff_keeps_files_but_drops_the_diff(self):
        self.write('big.txt', 'x' * (verify_filter.MAX_DIFF_BYTES + 1))
        self.commit('big')
        change = verify_filter.collect_change(self.repo, {})
        self.assertEqual((change.files, change.diff, change.oversized), (('big.txt',), None, True))

    def test_huge_diff_is_oversized_without_reading_it_all(self):
        self.write('huge.txt', ('y' * 99 + '\n') * (50 * verify_filter.MAX_DIFF_BYTES // 100))
        self.commit('huge')
        tracemalloc.start()
        try:
            change = verify_filter.collect_change(self.repo, {})
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual((change.files, change.diff, change.oversized), (('huge.txt',), None, True))
        self.assertLess(peak, 4 * verify_filter.MAX_DIFF_BYTES)

    def test_diff_just_under_the_cap_is_kept(self):
        self.write('near.txt', 'x' * (verify_filter.MAX_DIFF_BYTES - 500) + '\n')
        self.commit('near')
        change = verify_filter.collect_change(self.repo, {})
        self.assertFalse(change.oversized)
        self.assertLessEqual(len(change.diff.encode()), verify_filter.MAX_DIFF_BYTES)
        self.assertIn('+xxx', change.diff)

    def test_crlf_diff_reaches_the_model_with_plain_newlines(self):
        Path(self.repo, 'dos.txt').write_bytes(b'one\r\ntwo\r\n')
        self.commit('dos')
        change = verify_filter.collect_change(self.repo, {})
        self.assertIn('+one\n+two', change.diff)
        self.assertNotIn('\r', change.diff)

    def test_untracked_file_content_is_in_the_diff(self):
        self.write('new_module.py', 'secret_marker = 42\n')
        change = verify_filter.collect_change(self.repo, {})
        self.assertIn('new_module.py', change.diff)
        self.assertIn('secret_marker = 42', change.diff)

    def test_oversized_untracked_file_drops_the_diff(self):
        self.write('big.txt', 'x' * (verify_filter.MAX_DIFF_BYTES + 1))
        change = verify_filter.collect_change(self.repo, {})
        self.assertEqual((change.files, change.diff, change.oversized), (('big.txt',), None, True))

    def test_untrusted_environment_is_recorded(self):
        self.assertFalse(verify_filter.collect_change(self.repo, {'COLD_VERIFY_UNTRUSTED': '1'}).trusted)

    def test_change_file_round_trips_and_a_missing_one_is_untrusted_and_unknown(self):
        change = verify_filter.collect_change(self.repo, {})
        environ = self.environ(change)
        self.assertEqual(verify_filter.load_change(environ), change)
        self.assertEqual(verify_filter.load_change({}), Change(None, None, False))
        Path(environ[verify_filter.CHANGES_ENV]).write_text('not json')
        self.assertEqual(verify_filter.load_change(environ), Change(None, None, False))


class Run(Repo):
    def test_skips_print_the_filter_name_and_reason_and_the_summary_counts(self):
        self.write('src.py', 'x = 2\n')
        self.commit('edit')
        change = verify_filter.collect_change(self.repo, {})
        code, output = self.run_filter(change, model(lambda test, change: Decision(test.endswith('a.py'), 'only a matters', 7, 0.5)))
        self.assertEqual(code, 0)
        self.assertIn('skip  tests/test_b.py  [luna] only a matters', output)
        self.assertIn('skip  tests/test_c.py  [luna] only a matters', output)
        self.assertNotIn('run   tests/test_a.py', output)
        self.assertIn('filters: calls=12 model_calls=3 model_failures=0 tokens=21 cost=$1.5000 run=1 skip=2', output)
        self.assertIn('::group::tests tests/test_*.py (1 of 3)', output)

    def test_only_selected_tests_execute(self):
        self.write('tests/test_b.py', 'raise SystemExit("b must not run")\n')
        self.commit('b is broken')
        code, output = self.run_filter(Change(('src.py',), 'diff', True), model(lambda test, change: Decision(test != 'tests/test_b.py', 'skip b')))
        self.assertEqual(code, 0, output)
        self.assertIn('skip  tests/test_b.py  [luna] skip b', output)

    def test_a_malformed_decision_fails_open_and_every_test_still_runs(self):
        self.write('tests/test_b.py', 'raise SystemExit("b ran")\n')
        self.commit('b is broken')
        failures = Path(self.temp.name, 'changes.json' + verify_filter.FAILURES_SUFFIX)
        for name, bad in (('non-boolean run', Decision(None, 'unrelated')), ('non-string reason', Decision(True, None))):
            with self.subTest(name):
                change = Change(('src.py',), 'diff', True)
                failures.unlink(missing_ok=True)
                code, output = self.run_filter(change, model(lambda test, change: bad), explain=True)
                self.assertEqual(code, 0, output)
                self.assertIn('run   tests/test_b.py  [luna] filter failed (TypeError', output)
                failures.unlink(missing_ok=True)
                code, output = self.run_filter(change, model(lambda test, change: bad))
                self.assertEqual(code, 1, output)
                self.assertIn('::group::tests tests/test_*.py (3 of 3)', output)
                self.assertIn('filters: calls=12 model_calls=3 model_failures=3', output)
                self.assertIn('1 of 3 selected test files failed', output)

    def test_a_failing_selected_test_exits_1(self):
        self.write('tests/test_a.py', 'raise SystemExit("a broke")\n')
        change = verify_filter.collect_change(self.repo, {})
        code, output = self.run_filter(change)
        self.assertEqual(code, 1)
        self.assertIn('fail  tests/test_a.py', output)
        self.assertIn('1 of 3 selected test files failed', output)
        self.assertIn('FAILED (1): tests tests/test_*.py (3 of 3)', output)

    def test_every_test_skipped_is_incomplete_not_a_pass(self):
        code, output = self.run_filter(TRUSTED, model(skip_model))
        self.assertEqual(code, 2)
        self.assertIn('INCOMPLETE: nothing left to run', output)

    def test_gate_and_no_cache_run_every_test_despite_skip_decisions(self):
        for flag in ('COLD_VERIFY_GATE', 'COLD_VERIFY_NO_CACHE'):
            with self.subTest(flag=flag):
                code, output = self.run_filter(TRUSTED, model(skip_model), environ=self.environ(TRUSTED, **{flag: '1'}))
                self.assertEqual(code, 0, output)
                self.assertIn('(3 of 3)', output)

    def test_no_matching_test_files_is_incomplete_not_a_pass(self):
        code, output = self.run_filter(TRUSTED, suites=('nothing/test_*.py',))
        self.assertEqual(code, 2)
        self.assertIn('INCOMPLETE: no test files match', output)

    def test_untrusted_environment_runs_everything_even_with_a_skipping_model(self):
        change = verify_filter.collect_change(self.repo, {'COLD_VERIFY_UNTRUSTED': '1'})
        code, output = self.run_filter(change, model(skip_model))
        self.assertEqual(code, 0)
        self.assertIn('::group::tests tests/test_*.py (3 of 3)', output)
        self.assertIn('model_calls=0', output)

    def test_a_selected_file_that_runs_no_tests_does_not_pass(self):
        self.write('tests/test_a.py', '')
        code, output = self.run_filter(TRUSTED)
        self.assertEqual(code, 1, output)
        self.assertIn('FAILED (1)', output)

    def test_model_failures_carry_across_suites(self):
        bad = Filter('model', lambda test, change: 1 / 0, model=True)
        environ = self.environ(TRUSTED)
        self.run_filter(TRUSTED, filters=(bad,), environ=environ)
        self.assertEqual(verify_filter.load_failures(environ), 3)
        calls = []
        counting = Filter('model', lambda test, change: calls.append(test), model=True)
        self.run_filter(TRUSTED, filters=(counting,), environ=environ)
        self.assertEqual(calls, [])

    def test_suites_running_at_once_do_not_lose_model_failures(self):
        def slow_failure(test, change):
            time.sleep(0.3)
            raise Boom
        bad = Filter('model', slow_failure, model=True)
        environ = self.environ(TRUSTED)
        with contextlib.redirect_stdout(io.StringIO()):
            suites = [threading.Thread(target=verify_filter.run, args=(
                [f'tests/test_{name}.py'], [sys.executable, '-c', ''], self.repo, environ, False, (bad,)))
                for name in 'ab']
            for suite in suites:
                suite.start()
            for suite in suites:
                suite.join()
        self.assertEqual(verify_filter.load_failures(environ), 2)

    def test_missing_change_file_runs_everything(self):
        code, output = self.run_filter(None, model(skip_model), explain=False, environ={})
        self.assertEqual(code, 0)
        self.assertIn('(3 of 3)', output)

    def test_explain_prints_every_verdict_and_runs_nothing(self):
        self.write('tests/test_a.py', 'raise SystemExit("must not run")\n')
        change = verify_filter.collect_change(self.repo, {})
        code, output = self.run_filter(change, explain=True)
        self.assertEqual(code, 0)
        self.assertIn('run   tests/test_a.py  [changed-test-file] test file changed in this diff', output)
        self.assertIn('run   tests/test_b.py  [default] no filter excluded it', output)
        self.assertNotIn('::group::', output)

    def test_preview_marker_in_the_environment_means_explain(self):
        code, output = self.run_filter(TRUSTED, environ=self.environ(TRUSTED, **{verify_filter.PREVIEW_ENV: '1'}))
        self.assertEqual(code, 0)
        self.assertNotIn('::group::', output)
        self.assertIn('run   tests/test_a.py', output)


class Preview(Repo):
    def preview(self, **extra):
        env = {key: value for key, value in os.environ.items() if key not in ('COLD_VERIFY_GATE', 'COLD_VERIFY_NO_CACHE')}
        result = subprocess.run([sys.executable, str(ROOT / 'ci/verify_filter.py'), 'preview', '--root', str(self.repo)],
                                text=True, capture_output=True, env={**env, **extra})
        return result.returncode, result.stdout

    def recipe(self, text):
        self.write('.fleet/verify.sh', text)

    def test_a_recipe_that_does_not_honor_preview_is_incomplete(self):
        self.recipe('#!/usr/bin/env bash\necho running everything\n')
        code, output = self.preview()
        self.assertEqual(code, 2)
        self.assertIn('does not support --preview', output)
        self.assertNotIn('running everything', output)

    def test_a_comment_mentioning_the_marker_is_not_support(self):
        self.recipe('#!/usr/bin/env bash\n# COLD_VERIFY_PREVIEW\ntouch ran.txt\n')
        code, output = self.preview()
        self.assertEqual(code, 2)
        self.assertFalse((self.repo / 'ran.txt').exists())

    def test_a_repo_without_a_recipe_is_incomplete_and_says_the_recipe_is_missing(self):
        code, output = self.preview()
        self.assertEqual(code, 2)
        self.assertIn(f'INCOMPLETE: no .fleet/verify.sh recipe in {self.repo.resolve()}; add one that branches on $COLD_VERIFY_PREVIEW', output)
        self.assertNotIn('does not support --preview', output)

    def test_a_python_recipe_is_incomplete_and_named(self):
        self.write('.fleet/verify.py', 'open("ran.txt", "w")\n')
        code, output = self.preview()
        self.assertEqual(code, 2)
        self.assertIn(f'--preview runs only a .fleet/verify.sh recipe; {self.repo.resolve()}/.fleet/verify.py has no preview mode', output)
        self.assertNotIn('no .fleet/verify.sh recipe in', output)
        self.assertFalse((self.repo / 'ran.txt').exists())

    def test_a_recipe_that_does_not_opt_in_keeps_its_message(self):
        self.recipe('#!/usr/bin/env bash\necho running everything\n')
        output = self.preview()[1]
        self.assertIn(f'INCOMPLETE: {self.repo.resolve()}/.fleet/verify.sh does not support --preview (it must branch on $COLD_VERIFY_PREVIEW)', output)
        self.assertNotIn('no .fleet/verify.sh recipe', output)

    def test_preview_runs_the_recipe_in_explain_mode_and_executes_no_test(self):
        self.write('tests/test_b.py', 'raise SystemExit("must not run")\n')
        self.write('tests/test_c.py', 'open("ran.txt", "w").write("x")\n')
        self.recipe('#!/usr/bin/env bash\nset -e\n[ -z "${COLD_VERIFY_PREVIEW:-}" ] && echo "recipe checks ran"\n'
                    f'python3 "$COLD_VERIFY_FILTER" run --suite "tests/test_*.py" -- {sys.executable} -m unittest discover -s "{{dir}}" -p "{{name}}"\n')
        self.commit('recipe and tests')
        self.git('update-ref', 'refs/heads/main', 'HEAD')
        self.write('src.py', 'x = 2\n')
        fake = FakeCodex(self)
        code, output = self.preview(**fake.environ(FAKE_CODEX_PROBABILITIES=json.dumps({'tests/test_b.py': 0.05})))
        self.assertEqual(code, 0, output)
        self.assertNotIn('recipe checks ran', output)
        for name in 'abc':
            self.assertIn(f'tests/test_{name}.py', output)
        self.assertIn('skip  tests/test_b.py  [luna] rated 0.05 likely affected: canned', output)
        self.assertIn('run   tests/test_c.py  [luna] rated 0.90 likely affected: could reach it', output)
        self.assertIn('filters: calls=', output)
        self.assertFalse((self.repo / 'ran.txt').exists())

    def test_preview_accepts_a_relative_root(self):
        self.recipe('#!/usr/bin/env bash\n: "${COLD_VERIFY_PREVIEW:-}"\npython3 "$COLD_VERIFY_FILTER" run --suite "tests/test_*.py" -- true\n')
        result = subprocess.run([sys.executable, str(ROOT / 'ci/verify_filter.py'), 'preview', '--root', self.repo.name],
                                text=True, capture_output=True, cwd=self.repo.parent)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('filters: calls=', result.stdout)

    def test_preview_inherits_no_stale_change_file(self):
        self.recipe('#!/usr/bin/env bash\n: "${COLD_VERIFY_PREVIEW:-}"\npython3 "$COLD_VERIFY_FILTER" run --suite "tests/test_*.py" -- true\n')
        self.write('tests/test_a.py', 'changed = 1\n')
        code, output = self.preview(COLD_VERIFY_CHANGES='/nonexistent/changes.json')
        self.assertEqual(code, 0, output)
        self.assertIn('run   tests/test_a.py  [changed-test-file]', output)


class EvalEnvironment(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('luna_eval', ROOT / 'docs/design/coldverify/luna-filter-eval/eval.py')
        self.eval = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.eval)

    def child_cost(self, parent):
        with unittest.mock.patch.dict(os.environ, parent, clear=True):
            env = self.eval.environment(Path('change.json'))
        done = subprocess.run([sys.executable, '-c', 'import verify_filter; print(verify_filter.model_cost(1000000, 0, 0))'],
                              cwd=ROOT / 'ci', env=env, text=True, capture_output=True, check=True)
        return env, float(done.stdout)

    def test_rates_reach_the_child_and_cost_is_nonzero(self):
        env, cost = self.child_cost({'COLD_VERIFY_MODEL_RATES': '2,1,3', 'COLD_VERIFY_PREVIEW': '1', 'PATH': os.environ['PATH']})
        self.assertEqual(env['COLD_VERIFY_MODEL_RATES'], '2,1,3')
        self.assertEqual(cost, 2.0)
        self.assertNotIn('COLD_VERIFY_PREVIEW', env)
        self.assertEqual(env['COLD_VERIFY_CHANGES'], 'change.json')

    def test_cost_is_unpriced_without_rates(self):
        env, cost = self.child_cost({'PATH': os.environ['PATH']})
        self.assertNotIn('COLD_VERIFY_MODEL_RATES', env)
        self.assertEqual(cost, 0.0)


class EvalIsolation(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('luna_eval', ROOT / 'docs/design/coldverify/luna-filter-eval/eval.py')
        self.eval = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.eval)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.scratch = Path(self.tmp.name) / 'scratch'
        (self.scratch / 'ci').mkdir(parents=True)
        (self.scratch / 'ci/verify_filter.py').write_text((ROOT / 'ci/verify_filter.py').read_text())
        (self.scratch / 'a.py').write_text('x = 1\n')
        for args in (('init', '-q', '-b', 'main'), ('add', '-A'), ('commit', '-q', '-m', 'baseline')):
            self.eval.git(self.scratch, *args)
        self.addCleanup(sys.modules.pop, 'verify_filter', None)

    def evaluate(self, number, failures_after):
        seen = []

        def fake_run_suites(scratch, env):
            path = Path(env['COLD_VERIFY_CHANGES'])
            seen.append((path, verify_filter.load_failures(env)))
            verify_filter.save_failures(env, failures_after)
            return [dict(suite='s', code=0, seconds=0, kept=0, total=0, skipped=[], failed=[], unit_tests=0,
                         calls=0, model_failures=failures_after, tokens=0, cost=0.0)]

        with unittest.mock.patch.object(self.eval, 'run_suites', fake_run_suites):
            self.eval.evaluate(self.scratch, (number, 'a.py', 'x = 1', 'x = 2', 'planted'))
        return seen

    def test_model_failures_from_one_bug_do_not_reach_the_next(self):
        first = self.evaluate(1, verify_filter.MODEL_FAILURE_LIMIT)
        second = self.evaluate(2, 0)
        self.assertEqual([failures for _, failures in first], [0, 0])
        self.assertEqual([failures for _, failures in second], [0, 0])
        self.assertNotEqual({path for path, _ in first}, {path for path, _ in second})

    def test_failure_sidecars_are_removed_after_each_evaluation(self):
        seen = self.evaluate(1, verify_filter.MODEL_FAILURE_LIMIT)
        for path, _ in seen:
            self.assertFalse(path.exists())
            self.assertFalse(Path(str(path) + verify_filter.FAILURES_SUFFIX).exists())


class EvalSuites(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('luna_eval', ROOT / 'docs/design/coldverify/luna-filter-eval/eval.py')
        self.eval = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.eval)

    def test_the_checked_in_recipe_yields_its_suite(self):
        found = self.eval.suites(ROOT)
        self.assertEqual([suite for suite, _ in found], ['tests/test*.py'])
        self.assertEqual(found[0][1], ['python3', 'ci/junit_unittest.py', 'discover', '-s', '{dir}', '-p', '{name}'])

    def test_a_recipe_without_suite_steps_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / '.fleet').mkdir()
            (Path(tmp) / '.fleet/verify.sh').write_text('#!/usr/bin/env bash\ntrue\n')
            with self.assertRaises(SystemExit):
                self.eval.suites(Path(tmp))
            with self.assertRaises(SystemExit):
                self.eval.run_suites(Path(tmp), {})


class Recipe(unittest.TestCase):
    def test_the_repo_recipe_covers_every_test_directory_and_honors_preview(self):
        recipe = (ROOT / '.fleet/verify.sh').read_text()
        self.assertTrue(verify_filter.honors_preview(recipe))
        suites = [line.split("--suite '")[1].split("'")[0] for line in recipe.splitlines() if "--suite '" in line]
        self.assertIn('tests/test*.py', suites)
        self.assertEqual([suite for suite in suites if not verify_filter.discover(ROOT, [suite])], [])
        listed = set(verify_filter.discover(ROOT, suites))
        expected = {name for directory in ('tests', 'install', 'routines/learning') for name in
                    subprocess.run(['git', '-C', str(ROOT), 'ls-files', '-co', '--exclude-standard', '--', f'{directory}/test*.py'],
                                   text=True, capture_output=True, check=True).stdout.split()}
        self.assertEqual(listed, expected)
        self.assertIn('tests/test_verify_filter.py', listed)

    def test_the_cache_and_trace_wrappers_strip_inherited_filter_variables(self):
        import verify_cache
        for name in ('COLD_VERIFY_FILTER', 'COLD_VERIFY_CHANGES', 'COLD_VERIFY_PREVIEW'):
            self.assertIn(name, verify_cache.ENV_NAMES)


if __name__ == '__main__':
    unittest.main()
