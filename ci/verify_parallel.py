#!/usr/bin/env python3
"""Run independent verify steps in parallel and print them as ordinary serial steps.

    verify_parallel.py [--jobs N] --step TITLE COMMAND [--step TITLE COMMAND]...

Each COMMAND runs with `bash -c` from the current directory. At most N run at
once (default: COLD_VERIFY_JOBS, else the CPU count of this process). The cap
is shared with the processes a step starts: each running step holds one token
from a pipe named by COLD_VERIFY_TOKENS, and verify_filter.py takes more before
running extra test files, so nested pools never exceed N in total (POSIX only;
on Windows each pool has its own cap of N). Every
step gets its own TMPDIR, removed when the step ends. Output is replayed in the
order the steps were declared, each as ::group::/::endgroup::, so the trace
rows do not depend on which step finished first. Every step runs even after a
failure; the exit code is the first failing step's. Docs: docs/verify-trace.md
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import os
import shutil
import subprocess
import sys
import tempfile
import time

JOBS_ENV = 'COLD_VERIFY_JOBS'
TOKENS_ENV = 'COLD_VERIFY_TOKENS'


def default_jobs(environ=None):
    value = (os.environ if environ is None else environ).get(JOBS_ENV)
    if value is None:
        return len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else os.cpu_count() or 1
    if not value.isdecimal() or int(value) < 1:
        raise ValueError(f'{JOBS_ENV} must be a positive integer, got {value!r}')
    return int(value)


def run_step(title, command, environ=None, tokens=None):
    environ = os.environ if environ is None else environ
    scratch = tempfile.mkdtemp(prefix='cold-step-', dir=environ.get('TMPDIR'))
    env = {**environ, 'TMPDIR': scratch, 'TMP': scratch, 'TEMP': scratch}
    env.pop(TOKENS_ENV, None)
    if tokens:
        env[TOKENS_ENV] = f'{tokens[0]},{tokens[1]}'
        os.read(tokens[0], 1)
    started = time.monotonic()
    try:
        done = subprocess.run(['bash', '-c', command], env=env, text=True, errors='replace', pass_fds=tokens or (),
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    finally:
        if tokens:
            os.write(tokens[1], b'+')
        shutil.rmtree(scratch, ignore_errors=True)
    status = 128 - done.returncode if done.returncode < 0 else done.returncode
    return title, status, done.stdout, round((time.monotonic() - started) * 1000)


def run(steps, jobs, environ=None):
    code = 0
    tokens = os.pipe() if os.name == 'posix' else None
    if tokens:
        os.write(tokens[1], b'+' * min(jobs, 4096))
    with ThreadPoolExecutor(max_workers=max(1, len(steps)) if tokens else jobs) as pool:
        futures = [pool.submit(run_step, title, command, environ, tokens) for title, command in steps]
        for future in futures:
            title, status, output, elapsed = future.result()
            print(f'::elapsed::{elapsed}')
            print(f'::group::{title}')
            if output:
                print(output, end='' if output.endswith('\n') else '\n')
            print('::endgroup::')
            if status:
                print(f'FAILED ({status}): {title}', flush=True)
                code = code or status
            sys.stdout.flush()
    if tokens:
        os.close(tokens[0])
        os.close(tokens[1])
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--jobs', type=int, help=f'maximum steps at once (default: ${JOBS_ENV}, else CPU count)')
    parser.add_argument('--step', nargs=2, action='append', metavar=('TITLE', 'COMMAND'), required=True)
    args = parser.parse_args()
    try:
        jobs = args.jobs if args.jobs is not None else default_jobs()
    except ValueError as error:
        parser.error(str(error))
    if jobs < 1:
        parser.error('--jobs must be at least 1')
    return run(args.step, jobs)


if __name__ == '__main__':
    raise SystemExit(main())
