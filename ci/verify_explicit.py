"""Run a caller's test command on an explicit base and head commit and record it as the repro."""
import json
import platform
import re
import secrets
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
from verify_repro import sandbox

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


def sandboxed(root, writable, network, command, checkout, env, output, readable=(), log=None):
    prefix = sandbox(root, writable, network, readable)
    started = time.monotonic()
    code = verify_repro.run_sandboxed(prefix, ['/bin/sh', '-c', command], checkout, env, capture=output, output=log)
    timed_out = code == 2 and time.monotonic() - started >= verify_repro.CASE_TIMEOUT
    if log is not None:
        log.seek(0)
        output.append(verify_repro.excerpt(log.read(4096).decode(errors='replace')))
        log.seek(0)
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


def check_suite_tree(root, checkout, home, env, revision, untracked):
    command = (f'test "$(git --no-replace-objects rev-parse HEAD)" = {shlex.quote(revision)}'
               f' && changes=$(git -c core.fsmonitor=false status --porcelain --untracked-files={untracked})'
               ' && test -z "$changes"')
    code, _ = sandboxed(root, (checkout, home), False, command, checkout, env, [])
    if code:
        raise verify_repro.NoRepro(f'the suite worktree does not match the requested revision {revision}')


def run_suite(root, head, command, folder, setup):
    result = {'schema_version': verify_trace.RESULT_SCHEMA_VERSION, 'result': 'INCOMPLETE',
              'head': None, 'tree': None, 'backend': 'local', 'command': ['sh', '-c', command],
              'exit_code': 2, 'tests_ran': 0, 'tests_source': None, 'steps': []}
    try:
        checkout = checked_out(root, head, folder / 'suite')
        try:
            home, junit, adapters = (folder / name for name in ('suite-home', 'junit', 'adapters'))
            for path in (home, junit, adapters):
                path.mkdir()
            env = verify_repro.case_environment(home)
            revision, tree = verify_repro.git(checkout, '--no-replace-objects', 'rev-parse', 'HEAD', 'HEAD^{tree}').decode().splitlines()
            expected = verify_repro.git(root, '--no-replace-objects', 'rev-parse', f'{head}^{{tree}}').decode().strip()
            if (revision, tree) != (head, expected):
                raise verify_repro.NoRepro(f'the suite worktree does not match the requested revision {head}')
            if setup:
                install(root, 'suite', setup, checkout, home, env)
                check_suite_tree(root, checkout, home, env, head, 'all')
            result.update(head=revision, tree=tree)
            key = secrets.token_hex(32)
            env.update({verify_trace.JUNIT_ENV: str(junit), verify_trace.JUNIT_KEY_ENV: key})
            for name in ('unittest', 'pytest'):
                adapter = adapters / f'junit_{name}.py'
                shutil.copyfile(Path(__file__).with_name(adapter.name), adapter)
                env[f'COLD_VERIFY_{name.upper()}'] = str(adapter)
            output = []
            steps = verify_trace.Steps(command)
            with tempfile.TemporaryFile() as log:
                code, timed_out = sandboxed(root, (checkout, home, junit), False, command, checkout, env, output, (adapters,), log)
                for line in log:
                    steps.feed(line.decode(errors='replace'))
            finished = steps.finish(code)
            if any(path.is_symlink() or not path.is_file() for path in junit.iterdir()):
                raise verify_repro.NoRepro('the suite did not produce regular JUnit evidence files')
            totals = verify_trace.junit_totals(junit, key)
            count, source = verify_trace.counted(finished, totals)
            status, reason = ('INCOMPLETE', 'the suite timed out') if timed_out else verify_trace.verdict(code, finished, totals)
            if status == 'PASS':
                check_suite_tree(root, checkout, home, verify_repro.case_environment(home), head, 'no')
            rows = verify_trace.records(finished, revision, {}, None)
            result.update(result=status, reason=reason, exit_code=code, tests_ran=count, tests_source=source,
                          steps=[{name: row[name] for name in ('step', 'command', 'status', 'duration_ms')} for row in rows])
        finally:
            verify_repro.git(root, 'worktree', 'remove', '--force', str(checkout))
    except (verify_repro.NoRepro, OSError, RuntimeError, subprocess.CalledProcessError) as error:
        result.update(result='INCOMPLETE', reason=verify_repro.excerpt(error))
    return result


def run(root, base, head, command, setup=None, suite=None):
    root = Path(root)
    report = {'result': 'INCOMPLETE', 'tests': [], 'base': None, 'head': None, 'host': host_name(),
              'platform': platform.system().lower(), 'backend': 'local', 'opt_out_reason': None}
    suite_result = None
    try:
        if suite is not None and verify_receipt.revision(root, 'HEAD') != head:
            raise verify_repro.NoRepro(f'the checkout does not match the requested suite revision {head}')
        check_inputs(root, base, head)
        report.update(base=base, head=head)
        placement()
        with tempfile.TemporaryDirectory(prefix='cold-explicit-', dir=os.environ.get('RUNNER_TEMP') or None) as folder:
            test = compare(root, base, head, command, Path(folder), setup)
            if suite is not None:
                suite_result = run_suite(root, head, suite, Path(folder), setup)
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
    if suite_result is not None:
        (root / verify_trace.RESULT_PATH).write_text(json.dumps(suite_result, indent=2) + '\n', encoding='utf-8')
    shutil.rmtree(root / verify_flows.ARTIFACTS, ignore_errors=True)
    (root / verify_receipt.REPRO_PATH).write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return CODES.get(report['tests'][0]['kind'], 1) if report['tests'] else 2
