"""Execution backend chosen by COLD_VERIFY_BACKEND: local, ssh:<host>, container:<image>, coldbox or coldbox-api."""
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

import verify_policy

SPEC = re.compile(r'local|coldbox(?:-api)?|ssh:[A-Za-z0-9][A-Za-z0-9._@-]*|container:[A-Za-z0-9][A-Za-z0-9._/:@-]*')
SSH = ['ssh', '-o', 'BatchMode=yes']
POLL_SECONDS = 15
POLL_LIMIT = 7200
STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
REMOTE_EXEC = ('import shlex, subprocess, sys; sys.exit(subprocess.call([*sys.argv[1:-2], shlex.join(['
               '"coldbox", "vm", "exec", sys.argv[-2], "--", "sh", "-c", sys.argv[-1]])]))')


class Unavailable(RuntimeError):
    pass


class NoApi(Unavailable):
    pass


ISOLATION = {'local': 'local', 'ssh': 'ssh', 'container': 'container', 'coldbox': 'coldbox-vm', 'coldbox-api': 'coldbox-vm'}


def configured():
    value = os.environ.get('COLD_VERIFY_BACKEND', '')
    if not value:
        return None
    if not SPEC.fullmatch(value):
        raise Unavailable(f'COLD_VERIFY_BACKEND must be local, ssh:<host>, container:<image> or coldbox[-api], not {value!r}')
    return value


def isolation(spec):
    return ISOLATION[spec.partition(':')[0]]


def answer(command, what):
    try:
        result = subprocess.run(command, text=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Unavailable(f'the {what} backend gave no answer: {error}') from error
    lines = result.stdout.split()
    if result.returncode or not lines:
        reason = ' '.join(result.stderr.split()) or f'exit {result.returncode}'
        reason = reason if len(reason) <= 300 else reason[:150] + ' … ' + reason[-150:]
        raise Unavailable(f'the {what} backend gave no answer: {reason}')
    return lines[-1]


def coldbox_host():
    host = None if shutil.which('coldbox') else os.environ.get('COLD_VERIFY_HOST') or verify_policy.verification_policy().get('coldbox_host')
    if host and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._@-]*', host):
        raise Unavailable(f'COLD_VERIFY_HOST must be a host name, not {host!r}')
    return host, [*SSH, host] if host else [], f'coldbox on {host}' if host else 'coldbox'


def api(run, what, deadline, *args):
    late = Unavailable(f'the {what} api did not finish within {POLL_LIMIT} s')
    left = deadline - time.monotonic()
    if left <= 0:
        raise late
    try:
        return subprocess.run([*run, 'coldbox', 'api', *args], text=True, errors='replace', capture_output=True,
                              stdin=subprocess.DEVNULL, timeout=left)
    except subprocess.TimeoutExpired as error:
        raise late from error
    except OSError as error:
        raise Unavailable(f'the {what} api gave no answer: {error}') from error


def shorten(text):
    return text if len(text) <= 300 else text[:150] + ' … ' + text[-150:]


def reason(result):
    return shorten(' '.join(result.stderr.split()) or f'exit {result.returncode}')


def unfinished(result):
    try:
        receipt = json.loads(result.stdout)
    except ValueError:
        receipt = None
    if not isinstance(receipt, dict) or not receipt.get('error'):
        return reason(result)
    return shorten(f"exit {result.returncode}, {printable(receipt.get('cause') or 'no cause')}: {printable(receipt['error'])}")


def printable(value):
    return ' '.join(''.join(char if char.isprintable() else ' ' for char in str(value)).split())


def reply(result, what):
    try:
        value = json.loads(result.stdout)
    except ValueError:
        value = None
    if result.returncode not in (0, 1) or not isinstance(value, dict):
        raise Unavailable(f'the {what} api gave no answer: {reason(result)}')
    return value


def submit(repo, sha):
    """Run `coldbox checks` on repo at sha through `coldbox api`; return 0 for pass and 1 for fail, raise NoApi when coldbox has no api."""
    _, run, what = coldbox_host()
    deadline, shown = time.monotonic() + POLL_LIMIT, None
    submitted = api(run, what, deadline, 'submit', '--repo', repo, '--sha', sha)
    if submitted.returncode == 2 and "invalid choice: 'api'" in submitted.stderr:
        raise NoApi(f'the {what} backend has no api subcommand')
    if submitted.returncode:
        raise Unavailable(f'the {what} api refused the submission: {reason(submitted)}')
    name = reply(submitted, what).get('id')
    if not isinstance(name, str) or not re.fullmatch(r'cbx-[A-Za-z0-9._-]+', name):
        raise Unavailable(f'the {what} api named an invalid submission {name!r}')
    while True:
        state = reply(api(run, what, deadline, 'status', name), what).get('state')
        if state in ('done', 'failed'):
            break
        if state not in ('queued', 'running'):
            raise Unavailable(f'the {what} api gave {name} an unknown state {state!r}')
        if state != shown:
            print(f'{name} on the {what} api is {state}', file=sys.stderr, flush=True)
            shown = state
        time.sleep(max(0, min(POLL_SECONDS, deadline - time.monotonic())))
    fetched = api(run, what, deadline, 'receipt', name)
    if fetched.returncode not in (0, 1):
        raise Unavailable(f'{name} on the {what} api has no pass or fail receipt: {unfinished(fetched)}')
    receipt = reply(fetched, what)
    found = tuple(receipt.get(key) for key in ('schema', 'id', 'verdict', 'repo', 'sha'))
    if found != ('coldbox.receipt/v1', name, 'fail' if fetched.returncode else 'pass', repo, sha):
        raise Unavailable(f'{name} on the {what} api returned a receipt for {found}, not for {name} of {repo} at {sha}')
    return fetched.returncode


@contextlib.contextmanager
def shell(spec):
    """Yield (argv prefix that runs one sh command string on the backend, the backend's node name)."""
    kind, _, target = spec.partition(':')
    if kind == 'ssh':
        yield ['ssh', '-o', 'BatchMode=yes', target], target.rpartition('@')[2]
        return
    if kind == 'container':
        engine = shutil.which('docker') or shutil.which('podman')
        if not engine:
            raise Unavailable('the container backend needs docker or podman on PATH')
        what = 'container'
        name = answer([engine, 'run', '-d', '--rm', '--hostname', 'cold-verify-' + uuid.uuid4().hex[:12],
                       target, 'sleep', 'infinity'], what)
        prefix, stop = [engine, 'exec', '-i', name, 'sh', '-c'], [engine, 'rm', '-f', name]
    elif kind in ('coldbox', 'coldbox-api'):
        host, run, what = coldbox_host()
        name = answer([*run, 'coldbox', 'vm', 'up'], what)
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', name):
            raise Unavailable(f'the {what} backend named an invalid VM {name!r}')
        prefix = [sys.executable, '-c', REMOTE_EXEC, *SSH, host, name] if host else ['coldbox', 'vm', 'exec', name, '--', 'sh', '-c']
        stop = [*run, 'coldbox', 'vm', 'down', name]
    else:
        raise RuntimeError(f'the {kind} backend runs in this process, not over a shell')
    previous = {}

    def ignore_stops():
        for number in previous:
            signal.signal(number, signal.SIG_IGN)

    def stopped_by(received, _):
        ignore_stops()
        sys.exit(128 + received)

    if threading.current_thread() is threading.main_thread():
        previous = {number: signal.signal(number, stopped_by) for number in STOP_SIGNALS}
    try:
        yield prefix, answer([*prefix, 'uname -n'], what)
    finally:
        ignore_stops()
        stopped = subprocess.run(stop, text=True, capture_output=True, stdin=subprocess.DEVNULL, check=False)
        for number, handler in previous.items():
            signal.signal(number, signal.SIG_DFL if handler is None else handler)
        if stopped.returncode:
            reason = ' '.join(stopped.stderr.split())[-300:] or f'exit {stopped.returncode}'
            print(f'warning: the {what} backend could not delete {name}: {reason}', file=sys.stderr, flush=True)
