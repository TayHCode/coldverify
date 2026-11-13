#!/usr/bin/env python3
"""Run every test in tests/ in its own process, across all CPUs.

    scripts/run_tests.py [--jobs N]

Tests marked with a true `run_alone` attribute (wall-clock checks) run one at
a time after the rest. Fails if any test fails, or if a discovered test neither ran nor was
skipped (SkipTest at import, in setUpModule or in setUpClass). Prints the output of
failing tests, the slowest tests, each skip reason with its count, and one
unittest-style total.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

TESTS = Path(__file__).resolve().parents[1] / 'tests'
COUNTS = ('failures', 'errors', 'skipped', 'expectedFailures', 'unexpectedSuccesses')
CHILD = f'''import json, sys, unittest
def text(reason):
    try:
        return str(reason).encode('ascii', 'backslashreplace').decode()
    except Exception:
        return '(a skip reason that cannot be shown)'
result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(unittest.defaultTestLoader.loadTestsFromName(sys.argv[1]))
fixture_skips = [test.id() for test, _ in result.skipped if type(test).__name__ == '_ErrorHolder']
counts = {{key: len(getattr(result, key)) for key in {COUNTS!r}}}
counts['skipped'] -= len(fixture_skips)
reasons = [text(reason) for test, reason in result.skipped if type(test).__name__ != '_ErrorHolder']
with open(sys.argv[2], 'w') as out:
    json.dump({{'run': result.testsRun, 'accounted': bool(result.testsRun or fixture_skips),
               'fixture_skips': fixture_skips, 'skip_reasons': reasons, **counts}}, out)
'''


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        elif type(item).__name__ == '_FailedTest':
            raise SystemExit(f'cannot load {item.id()}:\n{item._exception}')
        else:
            yield item


def run_test(test):
    with tempfile.TemporaryDirectory(prefix='cold-test-') as scratch:
        summary = Path(scratch) / 'summary.json'
        started = time.monotonic()
        done = subprocess.run([sys.executable, '-c', CHILD, test, str(summary)], cwd=TESTS,
                              env={**os.environ, 'TMPDIR': scratch}, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors='replace')
        counts = json.loads(summary.read_text()) if summary.is_file() else None
    return test, done.returncode, counts, done.stdout, time.monotonic() - started


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--jobs', type=int,
                        default=len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else os.cpu_count() or 1)
    jobs = parser.parse_args().jobs
    if jobs < 1:
        parser.error('--jobs must be at least 1')
    sys.path.insert(0, str(TESTS))
    found = list(flatten(unittest.defaultTestLoader.discover(str(TESTS), pattern='test*.py', top_level_dir=str(TESTS))))
    module_skips = [item for item in found if type(item).__name__ == 'ModuleSkipped']
    tests = [item for item in found if item not in module_skips]
    alone = [item.id() for item in tests if getattr(getattr(item, item._testMethodName), 'run_alone', False)]
    pooled = [item.id() for item in tests if item.id() not in alone]
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        results = list(pool.map(run_test, pooled))
    results += [run_test(test) for test in alone]
    total = dict.fromkeys(('run', *COUNTS), 0)
    total['run'] = total['skipped'] = len(module_skips)
    total['skipped'] += len({skip for _, _, counts, _, _ in results if counts for skip in counts['fixture_skips']})
    failed = 0
    missing = [test for test, _, counts, _, _ in results if not (counts and counts['accounted'])]
    for test, code, counts, output, _ in results:
        for key in total:
            total[key] += counts[key] if counts else 0
        if code or not counts or counts['failures'] or counts['errors'] or counts['unexpectedSuccesses']:
            failed += 1
            print(f'::group::FAILED {test} (exit {code})\n{output}::endgroup::', flush=True)
    for line in (line for _, _, _, output, _ in results for line in output.splitlines()):
        if line.startswith('CANARY '):
            print(line)
    print('Slowest tests:')
    for test, _, _, _, elapsed in sorted(results, key=lambda result: -result[4])[:10]:
        print(f'  {elapsed:6.1f}s  {test}')
    reasons = Counter(reason for _, _, counts, _, _ in results if counts for reason in counts['skip_reasons'])
    module_result = unittest.TestResult()
    for item in module_skips:
        item.run(module_result)
    reasons.update(str(reason).encode('ascii', 'backslashreplace').decode() for _, reason in module_result.skipped)
    for reason, count in sorted(reasons.items()):
        print(f'Skipped {count}: {reason}')
    print(f'Ran {total["run"]} tests in {time.monotonic() - started:.3f}s ({jobs} jobs)')
    if missing:
        failed += 1
        print(f'FAILED: {len(missing)} of {len(tests)} discovered tests did not run: {", ".join(missing[:10])}')
    notes = ', '.join(f'{key}={total[key]}' for key in COUNTS if total[key])
    print(('FAILED' if failed else 'OK') + (f' ({notes})' if notes else ''))
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
