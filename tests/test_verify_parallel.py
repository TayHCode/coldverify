import contextlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ci'))
import verify_parallel
import verify_trace
from run_alone import run_alone

RUNNER = ROOT / 'ci/verify_parallel.py'
POSIX_ONLY = unittest.skipUnless(os.name == 'posix', 'steps run with bash and POSIX utilities')
TIMINGS = ('duration_ms', 'run_id')
STEPS = [
    ('slow pass', 'sleep 0.4; echo slow-out; echo slow-err >&2; echo "tmp=$(basename "$TMPDIR")" >/dev/null'),
    ('fast fail', 'echo fast-out; echo "assertion failed" >&2; exit 3'),
    ('quiet pass', 'true'),
    ('no newline', 'printf tail-without-newline'),
    ('second fail', 'sleep 0.2; echo second-out; exit 5'),
]


def step_arguments(steps):
    return [argument for title, command in steps for argument in ('--step', title, command)]


@POSIX_ONLY
class VerifyParallel(unittest.TestCase):
    def run_recipe(self, jobs, steps=STEPS, environ=None):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.run(['git', '-C', folder, 'init', '-q'], check=True)
            (root / '.fleet').mkdir()
            (root / '.fleet/verify.sh').write_text('echo recipe\n')
            env = {**os.environ, 'HOME': folder, **(environ or {})}
            env.pop('COLD_VERIFY_JOBS', None)
            env.update(environ or {})
            process = subprocess.run(
                [sys.executable, str(ROOT / 'ci/verify_trace.py'), '--root', folder, '--',
                 sys.executable, str(RUNNER), '--jobs', str(jobs), *step_arguments(steps)],
                check=False, text=True, capture_output=True, env=env)
            trace = (root / '.cold/verify-trace.jsonl').read_text().splitlines()
            return process, [json.loads(line) for line in trace]

    @staticmethod
    def without_timings(records):
        return [{key: value for key, value in record.items() if key not in TIMINGS} for record in records]

    @staticmethod
    def without_elapsed(output):
        return re.sub(r'(?m)^::elapsed::\d+\n', '', output)

    def test_parallel_results_equal_serial_results(self):
        serial, serial_trace = self.run_recipe(1)
        parallel, parallel_trace = self.run_recipe(len(STEPS))
        self.assertEqual(serial.returncode, 3)
        self.assertEqual(parallel.returncode, serial.returncode)
        self.assertEqual(self.without_elapsed(parallel.stdout), self.without_elapsed(serial.stdout))
        self.assertEqual(self.without_timings(parallel_trace), self.without_timings(serial_trace))
        self.assertEqual([(r['step'], r['status']) for r in parallel_trace],
                         [('slow pass', 'pass'), ('fast fail', 'fail'), ('quiet pass', 'pass'),
                          ('no newline', 'pass'), ('second fail', 'fail')])
        self.assertEqual({r['concurrent_runs'] for r in parallel_trace}, {1})
        self.assertEqual({r['host'] for r in parallel_trace}, {r['host'] for r in serial_trace})
        failed = {r['step']: r['failing_excerpt'] for r in parallel_trace if r['status'] == 'fail'}
        self.assertEqual(failed['fast fail'], 'fast-out\nassertion failed\nFAILED (3): fast fail')
        self.assertEqual(failed['second fail'], 'second-out\nFAILED (5): second fail')
        self.assertIn('slow-out\nslow-err', parallel.stdout)

    def test_a_step_killed_by_a_signal_fails_with_the_shell_exit_code(self):
        process, trace = self.run_recipe(2, [('killed', 'kill -TERM $$'), ('healthy', 'true')])
        self.assertEqual(process.returncode, 143)
        self.assertIn('FAILED (143): killed', process.stdout)
        self.assertEqual([(r['step'], r['status']) for r in trace], [('killed', 'fail'), ('healthy', 'pass')])

    def test_recorded_duration_is_the_step_time_not_the_replay_time(self):
        _, trace = self.run_recipe(len(STEPS))
        durations = {r['step']: r['duration_ms'] for r in trace}
        self.assertGreaterEqual(durations['slow pass'], 400)
        self.assertLess(durations['quiet pass'], durations['slow pass'])

    def run_marked(self, steps, jobs, extra=()):
        done = subprocess.run([sys.executable, str(RUNNER), '--jobs', str(jobs), *step_arguments(steps), *extra],
                              check=False, text=True, capture_output=True)
        return done

    def test_steps_really_overlap(self):
        with tempfile.TemporaryDirectory() as folder:
            def meet(me, other):
                return (f'touch {folder}/{me}; for _ in $(seq 100); do [ -e {folder}/{other} ] && exit 0; sleep 0.1; done; exit 9')
            done = self.run_marked([('a', meet('a', 'b')), ('b', meet('b', 'a'))], 2)
        self.assertEqual(done.returncode, 0, done.stdout)

    def test_jobs_caps_how_many_steps_run_at_once(self):
        for jobs in (1, 2):
            with self.subTest(jobs=jobs), tempfile.TemporaryDirectory() as folder:
                log = Path(folder) / 'log'
                body = f'echo start >> {log}; sleep 0.2; echo end >> {log}'
                done = self.run_marked([(f's{n}', body) for n in range(5)], jobs)
                self.assertEqual(done.returncode, 0, done.stdout)
                active = peak = 0
                for event in log.read_text().split():
                    active += 1 if event == 'start' else -1
                    peak = max(peak, active)
                self.assertEqual(peak, jobs)

    def test_jobs_caps_test_files_across_nested_filter_steps(self):
        with tempfile.TemporaryDirectory() as folder:
            repo, log = Path(folder, 'repo'), Path(folder, 'log')
            (repo / 'tests').mkdir(parents=True)
            subprocess.run(['git', '-C', str(repo), 'init', '-q'], check=True)
            for n in range(4):
                (repo / f'tests/test_{n}.py').write_text(
                    'import time, unittest\n'
                    'class T(unittest.TestCase):\n'
                    '    def test_slow(self):\n'
                    f'        open({str(log)!r}, "a").write("start\\n")\n'
                    '        deadline = time.monotonic() + 10\n'
                    f'        while open({str(log)!r}).read().split().count("start") < 2 and time.monotonic() < deadline:\n'
                    '            time.sleep(0.01)\n'
                    f'        open({str(log)!r}, "a").write("end\\n")\n')
            command = (f"{shlex.quote(sys.executable)} {shlex.quote(str(ROOT / 'ci/verify_filter.py'))} run --suite '%s' -- "
                       f"{shlex.quote(sys.executable)} -m unittest discover -s '{{dir}}' -p '{{name}}'")
            steps = [('low', command % 'tests/test_[01].py'), ('high', command % 'tests/test_[23].py')]
            env = {key: value for key, value in os.environ.items() if not key.startswith('COLD_VERIFY_')}
            done = subprocess.run([sys.executable, str(RUNNER), '--jobs', '2', *step_arguments(steps)],
                                  cwd=repo, env=env, text=True, capture_output=True, check=False)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            active = peak = 0
            for event in log.read_text().split():
                active += 1 if event == 'start' else -1
                peak = max(peak, active)
            self.assertEqual(log.read_text().split().count('start'), 4)
            self.assertEqual(peak, 2)

    def test_windows_runs_steps_without_passing_file_descriptors(self):
        calls = []
        def fake_run(argv, **kwargs):
            calls.append(kwargs)
            return subprocess.CompletedProcess(argv, 0, 'ok\n', '')
        with patch.object(verify_parallel.os, 'name', 'nt'), \
             patch.object(verify_parallel.subprocess, 'run', side_effect=fake_run), \
             contextlib.redirect_stdout(io.StringIO()):
            code = verify_parallel.run([('a', 'true'), ('b', 'true')], 2)
        self.assertEqual(code, 0)
        self.assertEqual([call['pass_fds'] for call in calls], [(), ()])
        self.assertTrue(all(verify_parallel.TOKENS_ENV not in call['env'] for call in calls))

    def test_each_step_gets_its_own_tmpdir_and_it_is_removed(self):
        with tempfile.TemporaryDirectory() as parent:
            body = 'echo "$TMPDIR"; test "$TMP" = "$TMPDIR"; test "$TEMP" = "$TMPDIR"; touch "$TMPDIR/scratch"; sleep 0.2'
            done = subprocess.run(
                [sys.executable, str(RUNNER), '--jobs', '3', *step_arguments([(f's{n}', body) for n in range(3)])],
                check=False, text=True, capture_output=True, env={**os.environ, 'TMPDIR': parent})
            self.assertEqual(done.returncode, 0, done.stdout)
            used = [line for line in done.stdout.splitlines() if line.startswith(parent)]
            self.assertEqual(len(set(used)), 3)
            self.assertEqual(os.listdir(parent), [])
            self.assertTrue(all(Path(line).parent == Path(parent) for line in used))

    def test_scratch_is_removed_when_a_step_fails(self):
        with tempfile.TemporaryDirectory() as parent:
            done = subprocess.run(
                [sys.executable, str(RUNNER), '--jobs', '2', *step_arguments([('boom', 'touch "$TMPDIR/x"; exit 7')])],
                check=False, text=True, capture_output=True, env={**os.environ, 'TMPDIR': parent})
            self.assertEqual(done.returncode, 7)
            self.assertEqual(os.listdir(parent), [])

    def test_default_jobs_is_cpu_count_and_configurable(self):
        cpus = len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else os.cpu_count()
        self.assertEqual(verify_parallel.default_jobs({}), cpus)
        self.assertEqual(verify_parallel.default_jobs({'COLD_VERIFY_JOBS': '3'}), 3)
        for bad in ('0', '-1', 'many', ''):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                verify_parallel.default_jobs({'COLD_VERIFY_JOBS': bad})

    def test_environment_variable_sets_the_cap_and_rejects_bad_values(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / 'log'
            body = f'echo start >> {log}; sleep 0.2; echo end >> {log}'
            done = subprocess.run([sys.executable, str(RUNNER), *step_arguments([(f's{n}', body) for n in range(4)])],
                                  check=False, text=True, capture_output=True, env={**os.environ, 'COLD_VERIFY_JOBS': '1'})
            self.assertEqual(done.returncode, 0)
            self.assertEqual(log.read_text().split(), ['start', 'end'] * 4)
        refused = subprocess.run([sys.executable, str(RUNNER), *step_arguments([('s', 'true')])],
                                 check=False, text=True, capture_output=True, env={**os.environ, 'COLD_VERIFY_JOBS': '0'})
        self.assertEqual(refused.returncode, 2)
        self.assertIn('COLD_VERIFY_JOBS', refused.stderr)

    def test_the_trace_wrapper_tells_recipes_where_the_runner_is(self):
        with tempfile.TemporaryDirectory() as folder:
            script = 'import os; print(os.environ["COLD_VERIFY_PARALLEL"])'
            done = subprocess.run([sys.executable, str(ROOT / 'ci/verify_trace.py'), '--root', folder, '--',
                                   sys.executable, '-c', script],
                                  check=False, text=True, capture_output=True, env={**os.environ, 'HOME': folder})
        self.assertEqual(done.stdout.splitlines()[0], str(RUNNER))


@POSIX_ONLY
class ParallelTiming(unittest.TestCase):
    def trace(self, steps, jobs, *, lock_wait=0.0, setup=0.0):
        @contextmanager
        def slow_lock():
            time.sleep(lock_wait)
            yield 1
        launcher = ('import json, os, subprocess, sys, time\n'
                    f'time.sleep({setup})\n'
                    'open(os.environ["COLD_VERIFY_SNAPSHOT_RECEIPT"], "w").write('
                    'json.dumps({"snapshot_tree": "ab", "snapshot_ready_at": time.time()}))\n'
                    f'sys.exit(subprocess.call([sys.executable, {str(RUNNER)!r}, "--jobs", "{jobs}", *{step_arguments(steps)!r}]))\n')
        with tempfile.TemporaryDirectory() as folder, patch.object(verify_trace, 'concurrent_run', slow_lock):
            root = Path(folder)
            subprocess.run(['git', '-C', folder, 'init', '-q'], check=True)
            with contextlib.redirect_stdout(io.StringIO()):
                verify_trace.run([sys.executable, '-u', '-c', launcher], root)
            return [json.loads(line) for line in (root / verify_trace.TRACE_PATH).read_text().splitlines()]

    @run_alone
    def test_each_parallel_step_reports_its_own_running_time_and_the_phases_stay_out_of_it(self):
        rows = self.trace([('slow a', 'sleep 1.5'), ('quick', 'true'), ('slow b', 'sleep 1.5')], 3,
                          lock_wait=1.0, setup=1.0)
        self.assertEqual([r['step'] for r in rows], ['slow a', 'quick', 'slow b'])
        for row in rows:
            self.assertEqual((row['waiting_ms'], row['setup_ms']), (0, 0), row)
        self.assertGreaterEqual(rows[0]['duration_ms'], 1500)
        self.assertGreaterEqual(rows[2]['duration_ms'], 1500)
        self.assertLess(rows[1]['duration_ms'], 1000)

    def test_serial_and_parallel_runs_report_the_same_split_up_to_timings(self):
        steps = [('slow a', 'sleep 0.3'), ('slow b', 'sleep 0.3')]
        serial, parallel = self.trace(steps, 1), self.trace(steps, 2)
        for rows in (serial, parallel):
            self.assertEqual([(r['step'], r['waiting_ms'], r['setup_ms']) for r in rows],
                             [('slow a', 0, 0), ('slow b', 0, 0)])
            self.assertTrue(all(r['duration_ms'] >= 300 for r in rows))


if __name__ == '__main__':
    unittest.main()
