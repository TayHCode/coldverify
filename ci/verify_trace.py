#!/usr/bin/env python3
"""Run a verify command, echo its output (stderr merged into stdout so step order is exact), and write a per-step JSON Lines trace beside its output.

    verify_trace.py --root REPO -- COMMAND [ARGS...]

Schema and step markers: docs/verify-trace.md
"""
import argparse
import hashlib
import json
import os
import platform
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import hmac
import xml.etree.ElementTree as ET
from pathlib import Path

import verify_backend
import verify_cache
import verify_filter
from verify_cache import head_sha, inputs_hash, recipe_contents, tree_sha
from junit_unittest import JUNIT_KEY_ENV, SIGNATURE_SUFFIX, signature
from verify_context import SuitePhase, concurrent_run, host_name, lock, redact

SCHEMA_VERSION = 3
HISTORY_PATH = Path('.cold/verify-history.jsonl')
TRACE_PATH = Path('.cold/verify-trace.jsonl')
RESULT_PATH = Path('.cold/verify-result.json')
RESULT_SCHEMA_VERSION = 1
CACHE_DIR = Path('.cold/verify-cache')
JUNIT_ENV = 'COLD_VERIFY_JUNIT_DIR'
EXCERPT_LINES = 20
FALLBACK_STEP = 'verify'
def excerpt(lines):
    return redact('\n'.join(lines)).splitlines()[-EXCERPT_LINES:]


class Steps:
    """Groups output lines into steps using ::group::/::endgroup::/::skip:: markers."""

    def __init__(self, command):
        self.command = command
        self.steps = []
        self.open = None
        self.depth = 0
        self.pending_inputs = None
        self.pending_elapsed = None
        self.tail = self.new_step(FALLBACK_STEP, command)
        self.grouped = False

    @staticmethod
    def new_step(title, command):
        return {'step': title, 'command': command, 'status': 'pass', 'started': time.monotonic(), 'ended': None, 'lines': [], 'inputs': None, 'elapsed': None}

    def feed(self, line):
        line = line.rstrip('\r\n')
        if line.startswith('::inputs::'):
            self.pending_inputs = line[len('::inputs::'):].strip() or None
        elif self.open is None and re.fullmatch(r'::elapsed::\d+', line):
            self.pending_elapsed = int(line[len('::elapsed::'):])
        elif self.open is not None and line.startswith(('::group::', '::endgroup::')) and (
                line.startswith('::group::') or self.depth):
            self.depth += 1 if line.startswith('::group::') else -1
            self.pending_inputs = None
            self.open['lines'].append(line)
        elif line.startswith('::group::'):
            self.grouped = True
            self.close_open()
            self.tail = self.new_step(FALLBACK_STEP, self.command)
            self.open = self.new_step(line[len('::group::'):].strip(), line[len('::group::'):].strip())
            self.open['inputs'], self.pending_inputs = self.pending_inputs, None
            self.open['elapsed'], self.pending_elapsed = self.pending_elapsed, None
        elif line.startswith('::endgroup::'):
            self.close_open()
            self.tail = self.new_step(FALLBACK_STEP, self.command)
        elif line.startswith('::skip::'):
            title = line[len('::skip::'):].strip()
            step = self.new_step(title, title)
            step.update(status='skip', ended=step['started'], inputs=self.pending_inputs)
            self.pending_inputs = None
            self.steps.append(step)
        else:
            if not self.mark_failed(line):
                (self.open or self.tail)['lines'].append(line)

    def mark_failed(self, line):
        failed = re.match(r'FAILED(?: \(\d+\))?: (.+)$', line)
        if not failed or self.open is not None:
            return False
        title = failed[1].strip()
        target = self.open or next((s for s in reversed(self.steps) if s['step'] == title), None)
        if target is None:
            target = self.tail
            if target['step'] == FALLBACK_STEP:
                target.update(step=title, command=title)
        target['status'] = 'fail'
        target['lines'].append(line)
        return True

    def close_open(self):
        if self.open is not None:
            self.open['ended'] = time.monotonic()
            self.steps.append(self.open)
            self.open = None

    def finish(self, exit_code):
        self.close_open()
        if not self.grouped or any(line.strip() for line in self.tail['lines']):
            self.tail['ended'] = time.monotonic()
            self.steps.append(self.tail)
        if exit_code and not any(step['status'] == 'fail' for step in self.steps):
            self.steps[-1]['status'] = 'fail'
        return self.steps


def overlap_ms(step, window):
    start, end = window
    return round(max(0, min(step['ended'], end) - max(step['started'], start)) * 1000)


def records(steps, head, recipes, run_id, context=None, waiting=(0, 0), setup=(0, 0)):
    for step in steps:
        record = {'schema_version': SCHEMA_VERSION, 'run_id': run_id, 'head': head, 'step': step['step'],
                      'command': step['command'], 'status': step['status'],
                      'duration_ms': step['elapsed'] if step['elapsed'] is not None else round((step['ended'] - step['started']) * 1000),
                      'waiting_ms': overlap_ms(step, waiting), 'setup_ms': overlap_ms(step, setup)}
        record.update(context or {'snapshot_tree': None, 'host': host_name(), 'platform': platform.system().lower(), 'concurrent_runs': 1})
        if step['status'] == 'fail':
            record['failing_excerpt'] = '\n'.join(excerpt(step['lines']))
        record['inputs_hash'] = step['inputs'] or inputs_hash(recipes, step['step'])
        yield record


def read_records(path):
    """Read v1 to v3 traces without requiring the fields added in v2 and v3."""
    with Path(path).open(encoding='utf-8') as stream:
        for line in stream:
            record = json.loads(line)
            if record.get('schema_version') not in (1, 2, 3):
                raise ValueError('unsupported verify trace schema')
            if record.get('status') not in ('pass', 'fail', 'skip'):
                raise ValueError('unknown verify trace status')
            yield {'snapshot_tree': None, 'host': None, 'platform': None, 'concurrent_runs': None, 'waiting_ms': None, 'setup_ms': None, **record}


def complete(steps):
    titles = [step['step'] for step in steps]
    titles += [line[len('::group::'):].strip() for step in steps for line in step['lines'] if line.startswith('::group::')]
    partial = (re.search(r'\((\d+) of (\d+)\)$', title) for title in titles)
    return all(step['status'] == 'pass' for step in steps) and not any(m and m[1] != m[2] for m in partial)


TEST_COUNTS = [re.compile(pattern, re.MULTILINE) for pattern in (
    r'^Ran (\d+) tests? in ',
    r'^GENERIC_EXECUTED_TESTS=(\d+)$',
    r'^=+ (?:.*?, )?(\d+) passed\b.* in [\d.]+s',
    r'^test result: ok\. (\d+) passed;',
    r'^# pass (\d+)$',
    r'^Tests:\s+(?:.*?, )?(\d+) passed',
    r'^\s*Tests\s+(\d+) passed',
)]
UNITTEST_SKIPPED = re.compile(r'^(?:OK|FAILED) \((?:.*?, )?skipped=(\d+)', re.MULTILINE)


def tests_ran(steps):
    output = '\n'.join(line for step in steps for line in step['lines'])
    counted = sum(int(count) for pattern in TEST_COUNTS for count in pattern.findall(output))
    return max(0, counted - sum(int(count) for count in UNITTEST_SKIPPED.findall(output)))


def signed(path, key):
    stamp = path.with_name(path.name + SIGNATURE_SUFFIX)
    try:
        return hmac.compare_digest(stamp.read_text(encoding='ascii').strip(), signature(key, path.read_bytes()))
    except (OSError, UnicodeDecodeError):
        return False


def junit_totals(directory, key):
    """Totals over every testcase in directory/*.xml, or None when the run wrote no XML there."""
    files = sorted(Path(directory).glob('*.xml'))
    if not files:
        return None
    totals = {'tests': 0, 'failures': 0, 'errors': 0, 'skipped': 0, 'unreadable': [], 'unsigned': []}
    for path in files:
        if not signed(path, key):
            totals['unsigned'].append(path.name)
            continue
        try:
            root = ET.parse(path).getroot()
        except (ET.ParseError, OSError, LookupError, ValueError):
            root = None
        if root is None or root.tag not in ('testsuites', 'testsuite'):
            totals['unreadable'].append(path.name)
            continue
        for case in root.iter('testcase'):
            totals['tests'] += 1
            outcome = next((child.tag for child in case if child.tag in ('failure', 'error', 'skipped')), None)
            if outcome:
                totals['failures' if outcome == 'failure' else 'errors' if outcome == 'error' else 'skipped'] += 1
    return totals


def counted(steps, junit):
    if junit is not None:
        return junit['tests'] - junit['skipped'], 'junit'
    count = tests_ran(steps)
    return count, 'output' if count else None


def verdict(code, steps, junit=None, infrastructure_phase=None):
    if not infrastructure_phase and (code or any(step['status'] == 'fail' for step in steps)):
        return 'FAIL', f'exit {code}' if code else 'a step failed'
    if junit and (junit['failures'] or junit['errors']):
        return 'FAIL', f"the JUnit XML records {junit['failures']} failures and {junit['errors']} errors"
    if infrastructure_phase:
        return 'INCOMPLETE', f'infrastructure aborted during suite {infrastructure_phase}'
    skipped = [step['step'] for step in steps if step['status'] == 'skip']
    if skipped:
        return 'INCOMPLETE', 'not run fresh (skipped or cached): ' + ', '.join(skipped)
    if not complete(steps):
        return 'INCOMPLETE', 'only part of a suite was selected'
    count, source = counted(steps, junit)
    if source == 'output':
        return 'INCOMPLETE', (f'{count} tests counted only from output text, which does not prove they ran; '
                              f'write JUnit XML to ${JUNIT_ENV}')
    if junit is None:
        return 'INCOMPLETE', 'no test count found in the output, so nothing proves a test ran'
    if junit['unsigned']:
        return 'INCOMPLETE', ("JUnit XML not written by coldverify's test adapter, so it does not prove a test ran: "
                              + ', '.join(junit['unsigned']))
    if junit['unreadable']:
        return 'INCOMPLETE', 'unreadable JUnit XML: ' + ', '.join(junit['unreadable'])
    if not count:
        return 'INCOMPLETE', 'the JUnit XML records no test that ran'
    return 'PASS', f'{count} tests ran fresh and passed'


def write_summary(rows, environ):
    path = environ.get('GITHUB_STEP_SUMMARY')
    if not path:
        return
    lines = ['### verify', '', '| step | status | seconds |', '| --- | --- | ---: |']
    lines += [f"| {row['step'].replace('|', '/')} | {row['status']} | {row['duration_ms'] / 1000:.1f} |" for row in rows]
    for row in rows:
        if row.get('failing_excerpt'):
            lines += ['', f"<details><summary>{row['step']}</summary>", '', '~~~',
                      row['failing_excerpt'].replace('~~~', '~ ~ ~'), '~~~', '', '</details>']
    with open(path, 'a', encoding='utf-8') as summary:
        summary.write('\n'.join(lines) + '\n')


def cache_environment(env, root, head, gate, no_cache):
    env = {key: value for key, value in (os.environ if env is None else env).items()
           if key not in verify_cache.ENV_NAMES}
    env['COLD_VERIFY_CACHE'] = str(Path(verify_cache.__file__).resolve())
    backend = env.get('COLD_VERIFY_BACKEND')
    env['COLD_VERIFY_CACHE_DIR'] = str(root / CACHE_DIR / hashlib.sha256(backend.encode()).hexdigest()[:16] if backend else root / CACHE_DIR)
    env['COLD_VERIFY_PARALLEL'] = str(Path(__file__).resolve().with_name('verify_parallel.py'))
    for name, value in (('COLD_VERIFY_HEAD', head), ('COLD_VERIFY_TREE', tree_sha(root) if head else None),
                        ('COLD_VERIFY_GATE', '1' if gate else None), ('COLD_VERIFY_NO_CACHE', '1' if no_cache else None)):
        if value:
            env[name] = value
    return env


def run(command, root, env=None, gate=False, no_cache=False, ci=False, strict=True):
    """Run command, pass stdout and stderr through unchanged, write the trace, return 0 for PASS, 2 for INCOMPLETE, nonzero for FAIL; with strict=False return the command's exit code."""
    root = Path(root)
    steps = Steps(' '.join(command))
    head, recipes = head_sha(root), recipe_contents(root)
    (root / TRACE_PATH).parent.mkdir(parents=True, exist_ok=True)
    (root / TRACE_PATH).parent.joinpath('.gitignore').write_text('*\n')
    environ = os.environ if env is None else env
    begin = time.monotonic()
    with concurrent_run() as count, tempfile.TemporaryDirectory(prefix='cold-trace-') as temporary:
        locked = time.monotonic()
        receipt = Path(temporary) / 'snapshot.json'
        environment = cache_environment(env, root, head, gate, no_cache)
        environment['COLD_VERIFY_SNAPSHOT_RECEIPT'] = str(receipt)
        changes = Path(temporary) / 'changes.json'
        junit = Path(temporary) / 'junit'
        junit.mkdir()
        environment[JUNIT_ENV] = str(junit)
        key = secrets.token_hex(32)
        environment[JUNIT_KEY_ENV] = key
        environment['COLD_VERIFY_UNITTEST'] = str(Path(__file__).resolve().with_name('junit_unittest.py'))
        environment['COLD_VERIFY_PYTEST'] = str(Path(__file__).resolve().with_name('junit_pytest.py'))
        verify_filter.write_change(verify_filter.collect_change(root, environment), changes)
        environment[verify_filter.CHANGES_ENV] = str(changes)
        environment['COLD_VERIFY_FILTER'] = str(Path(verify_filter.__file__).resolve())
        context = {'snapshot_tree': None, 'host': host_name(), 'platform': platform.system().lower(), 'concurrent_runs': count}
        spawned_at = time.time()
        spawned = time.monotonic()
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   errors='replace', env=environment)
        received, previous = [], {}
        if threading.current_thread() is threading.main_thread():
            previous = {number: signal.signal(number, lambda number, _: (received.append(number), process.send_signal(number)))
                        for number in verify_backend.STOP_SIGNALS}
        try:
            for line in process.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                steps.feed(line)
            code = process.wait()
            process.stdout.close()
        finally:
            for number, handler in previous.items():
                signal.signal(number, signal.SIG_DFL if handler is None else handler)
        if received:
            sys.exit(128 + received[0])
        setup, backend, infrastructure_phase = (locked, locked), None, None
        if receipt.exists():
            exported = json.loads(receipt.read_text())
            ready_at = exported.pop('snapshot_ready_at', None)
            if ready_at is not None:
                setup = (locked, max(locked, spawned + ready_at - spawned_at))
            backend = exported.pop('backend', None)
            phase = exported.pop('infrastructure_phase', None)
            if code == 2 and phase in (SuitePhase.SETUP, SuitePhase.EXECUTION):
                infrastructure_phase = SuitePhase(phase)
            context.update(exported)
        finished = steps.finish(code)
        totals = junit_totals(junit, key)
        rows = list(records(finished, head, recipes, uuid.uuid4().hex, context, (begin, locked), setup))
        with (root / HISTORY_PATH).open('a', encoding='utf-8') as history:
            lock(history)
            with (root / TRACE_PATH).open('w', encoding='utf-8') as trace:
                for record in rows:
                    line = json.dumps(record) + '\n'
                    trace.write(line)
                    history.write(line)
    if ci:
        write_summary(rows, environ)
    result, reason = verdict(code, finished, totals, infrastructure_phase)
    count, source = counted(finished, totals)
    summary = {'schema_version': RESULT_SCHEMA_VERSION, 'result': result, 'reason': reason, 'head': head, 'command': command, 'exit_code': code,
               'tests_ran': count, 'tests_source': source,
               'steps': [{key: row[key] for key in ('step', 'command', 'status', 'duration_ms')} for row in rows]}
    if context.get('suite_head'):
        summary.update(head=context['suite_head'], tree=context.get('suite_tree'))
    if isinstance(backend, str) and verify_backend.SPEC.fullmatch(backend):
        summary['backend'] = backend
    if infrastructure_phase:
        summary['infrastructure_phase'] = infrastructure_phase
    (root / RESULT_PATH).write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    print(f'TESTS {result}: {reason}', flush=True)
    if not strict:
        return code
    return {'PASS': 0, 'INCOMPLETE': 2, 'FAIL': 1 if infrastructure_phase else code or 1}[result]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--gate', action='store_true', help='release or merge-gate run: reuse a cached step only for the exact same tree')
    parser.add_argument('--no-cache', action='store_true', help='run every step even when a cached pass exists')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('a command is required after --')
    return run(command, args.root, gate=args.gate, no_cache=args.no_cache)


if __name__ == '__main__':
    raise SystemExit(main())
