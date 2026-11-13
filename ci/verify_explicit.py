"""Run a caller's test command on an explicit base and head commit and record it as the repro."""
import json
import platform
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import os
from pathlib import Path

import verify_backend
import verify_flows
import verify_receipt
import verify_repro
import verify_trace
from verify_context import coldbox_guest, host_name

NAME = 'test command'
CODES = {'proof': 0, 'unproven': 2}
OFFLINE = re.compile(r'Temporary failure in name resolution|Name or service not known|Network is unreachable|'
                     r'ENETUNREACH|EAI_AGAIN|ENOTFOUND|Could not resolve host|nodename nor servname')


def placement():
    try:
        spec = verify_backend.configured()
    except verify_backend.Unavailable as error:
        raise verify_repro.NoRepro(str(error)) from error
    if spec == 'local' or spec is None and coldbox_guest():
        return
    where = f'COLD_VERIFY_BACKEND={spec} cannot run it yet' if spec else 'this is not a Coldbox guest'
    raise verify_repro.NoRepro('the test command is untrusted, so it runs only in a Coldbox guest or with '
                               f'COLD_VERIFY_BACKEND=local inside a disposable VM; {where}')


def changed_tests(root, base, head):
    listing = os.fsdecode(verify_repro.git(root, 'diff', '--name-status', '--no-renames', '-z', base, head)).split('\0')
    changed = [(status, path) for status, path in zip(*[iter(listing)] * 2) if path and verify_receipt.is_test(path)]
    kept = [path for status, path in changed if status != 'D']
    if not kept:
        raise verify_repro.NoRepro('no test file is added or changed between --base and --head, '
                                   'so a failure on base would not show what the change fixed')
    return kept, [path for status, path in changed if status == 'D']


def check_inputs(root, base, head):
    for name, commit in (('--base', base), ('--head', head)):
        if not verify_flows.present(root, commit):
            raise verify_repro.NoRepro(f'{name} {commit} is not a commit in this checkout')
    if verify_repro.git(root, 'status', '--porcelain', '--untracked-files=all').strip():
        raise verify_repro.NoRepro('commit all changes before running coldverify --test-command')
    if subprocess.run(['git', '-C', str(root), 'merge-base', '--is-ancestor', base, head],
                      env=verify_repro.clean_environment()).returncode:
        raise verify_repro.NoRepro(f'--base {base} is not an ancestor of --head {head}')


def checked_out(root, commit, checkout):
    verify_repro.setup_git(root, '--no-replace-objects', 'worktree', 'add', '--detach', str(checkout), commit)
    return checkout


def sandbox(root, writable, network):
    bwrap = shutil.which('bwrap')
    if bwrap is None:
        raise verify_repro.NoRepro('bubblewrap (bwrap) is not installed, and the test command never runs unsandboxed')
    root = Path(root).resolve()
    common = Path(verify_repro.git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir').decode().strip())
    hidden = ['/run', '/tmp', '/var', '/home', '/root', '/srv', '/mnt', '/media', str(root)]
    command = [bwrap, '--die-with-parent', '--new-session', '--unshare-all', *(['--share-net'] if network else []),
               '--ro-bind', '/', '/', '--dev', '/dev', '--proc', '/proc']
    for path in hidden:
        command += ['--tmpfs', path]
    resolver = os.path.realpath('/etc/resolv.conf')
    command += ['--ro-bind-try', resolver, resolver, '--ro-bind', str(common), str(common)]
    for path in writable:
        command += ['--bind', str(path), str(path)]
    for path in reversed(hidden):
        command += ['--remount-ro', path]
    return command + ['--']


def sandboxed(root, writable, network, command, checkout, env, output):
    marker = Path(env['HOME']) / '.cold-sandbox-started'
    marker.unlink(missing_ok=True)
    prefix = sandbox(root, writable, network)
    started = time.monotonic()
    try:
        code = verify_repro.run_test([*prefix, 'sh', '-c', ': > "$0" && exec sh -c "$1"', str(marker), command],
                                     checkout, env, capture=output)
    except OSError as error:
        raise verify_repro.NoRepro('the sandbox could not start: ' + verify_repro.excerpt(error)) from error
    if not marker.is_file():
        raise verify_repro.NoRepro(f'the sandbox did not start (exit {code}): {output[0]}')
    timed_out = code == 2 and time.monotonic() - started >= verify_repro.CASE_TIMEOUT
    return code, timed_out


def install(root, side, command, checkout, home, env):
    output = []
    code, timed_out = sandboxed(root, (checkout, home), True, command, checkout, env, output)
    print(f'Install command on {side}: exit {code}', flush=True)
    if timed_out:
        raise verify_repro.NoRepro(f'install on {side} timed out after {verify_repro.CASE_TIMEOUT} s')
    if code:
        raise verify_repro.NoRepro(f'install on {side} failed with exit {code}: {output[0]}')


def run_side(root, side, commit, command, folder, setup=None, tests=(), deleted=(), head=None):
    checkout = checked_out(root, commit, folder / side)
    home = folder / f'{side}-home'
    try:
        if tests:
            verify_repro.git(checkout, '--literal-pathspecs', '--no-replace-objects', 'checkout', head, '--', *tests)
        for path in deleted:
            (checkout / path).unlink(missing_ok=True)
        home.mkdir()
        env = verify_repro.case_environment(home)
        if setup:
            install(root, side, setup, checkout, home, env)
        output = []
        code, _ = sandboxed(root, (checkout, home), False, command, checkout, env, output)
    finally:
        verify_repro.git(root, 'worktree', 'remove', '--force', str(checkout))
        shutil.rmtree(home, ignore_errors=True)
    print(f'Test command on {side}: exit {code}', flush=True)
    steps = f'{setup} && {command}' if setup else command
    return code, output[0], f'cd {shlex.quote(str(checkout))} && {steps}'


def compare(root, base, head, command, folder, setup=None):
    tests, deleted = changed_tests(root, base, head)
    base_exit, base_output, base_command = run_side(root, 'base', base, command, folder, setup, tests, deleted, head)
    head_exit, head_output, head_command = run_side(root, 'head', head, command, folder, setup)
    kind = verify_flows.kind(base_exit, head_exit)
    offline = head_exit != 0 and base_exit != 0 and OFFLINE.search(base_output) and OFFLINE.search(head_output)
    return {'name': NAME, 'base_exit': base_exit, 'base_output': base_output, 'head_exit': head_exit,
            'head_output': head_output, 'kind': 'unproven' if kind == 'unrunnable' else kind,
            'commands': [base_command, head_command], 'offline': bool(offline)}


def run(root, base, head, command, setup=None):
    root = Path(root)
    report = {'result': 'INCOMPLETE', 'tests': [], 'base': None, 'head': None, 'host': host_name(),
              'platform': platform.system().lower(), 'backend': 'local', 'opt_out_reason': None}
    try:
        check_inputs(root, base, head)
        report.update(base=base, head=head)
        placement()
        with tempfile.TemporaryDirectory(prefix='cold-explicit-', dir=os.environ.get('RUNNER_TEMP') or None) as folder:
            test = compare(root, base, head, command, Path(folder), setup)
        offline = test.pop('offline')
        report['tests'] = [test]
        report['result'] = {'proof': 'PASS', 'unproven': 'INCOMPLETE'}.get(test['kind'], 'FAIL')
        if offline:
            report['error'] = ('both sides failed with a network error and the test command gets no network, '
                               'so the test may need network rather than the fix being wrong')
        elif test['kind'] == 'unproven':
            report['error'] = (f"the test command could not run: base exit {test['base_exit']}, head exit {test['head_exit']} "
                               '(exit 1 is a failing test, 0 a passing one)')
    except (verify_repro.NoRepro, OSError, RuntimeError, subprocess.CalledProcessError) as error:
        detail = error.stderr.decode(errors='replace') if isinstance(error, subprocess.CalledProcessError) and error.stderr else error
        report['error'] = verify_repro.excerpt(detail)
        print('INCOMPLETE: ' + report['error'], flush=True)
    for forged in (verify_trace.RESULT_PATH, verify_flows.EVIDENCE):
        (root / forged).unlink(missing_ok=True)
    shutil.rmtree(root / verify_flows.ARTIFACTS, ignore_errors=True)
    (root / verify_receipt.REPRO_PATH).write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return CODES.get(report['tests'][0]['kind'], 1) if report['tests'] else 2
