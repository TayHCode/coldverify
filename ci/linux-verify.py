#!/usr/bin/env python3
"""Run a repository's real suite on an approved native Linux host."""
import argparse
import io
import json
import os
import platform
import random
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

import evidence_export
import npm_content_cache
import verify_backend
import verify_cache
import verify_policy
import windows_lane
from verify_context import SuitePhase, coldbox_guest, record_backend, record_run_context, record_snapshot

SSH_CONNECTION_FAILED = 255
CONNECT_ATTEMPTS = 8
REMOTE_PRODUCT_FAILED = 65
REMOTE_EXECUTION_ABORTED = 75


class RecipeFailed(subprocess.CalledProcessError):
    pass


def git_environment():
    return {key: value for key, value in os.environ.items() if key not in
            ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR',
             'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES')}


def repository(directory):
    if os.environ.get('GITHUB_REPOSITORY'):
        return os.environ['GITHUB_REPOSITORY']
    inside = subprocess.run(['git', '-C', str(directory), 'rev-parse', '--is-inside-work-tree'],
                            text=True, capture_output=True, check=False, env=git_environment())
    if inside.stdout.strip() != 'true':
        raise RuntimeError('repository identity is unknown without a Git checkout; set GITHUB_REPOSITORY=owner/name')
    result = subprocess.run(['git', '-C', str(directory), 'config', '--get-regexp', r'^remote\..*\.url$'],
                            text=True, capture_output=True, check=False, env=git_environment())
    fallback = ''
    for line in result.stdout.splitlines():
        fields = line.split(maxsplit=1)
        if len(fields) == 2:
            match = re.fullmatch(r'(?:https://github\.com/|git@github\.com:|ssh://git@github\.com(?:\:22)?/)(' + verify_policy.REPOSITORY + r'?)(?:\.git)?', fields[1])
            if match:
                repo = match[1]
                if verify_policy.linux_repository(repo) or verify_policy.macos_repository(repo):
                    return repo
                if not fallback and verify_policy.linux_runner_repository(repo):
                    fallback = repo
    return fallback


def runs_locally(repo):
    return not os.environ.get('COLD_VERIFY_HOST') and not verify_policy.linux_runner_repository(repo) and not verify_policy.macos_repository(repo)


def native_host():
    guest = coldbox_guest()
    if platform.system() != 'Linux' or 'microsoft' in platform.release().casefold() or not guest and platform.node().casefold() not in {
            host.casefold() for host in verify_policy.verification_policy()['linux_hosts']}:
        raise RuntimeError('INCOMPLETE: run on an approved native Linux host')
    return guest or platform.node()


def command(args, directory, env):
    try:
        with subprocess.Popen(args, cwd=directory, env=env, text=True, errors='replace', stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL) as process:
            for line in process.stdout:
                print(line, end='', flush=True)
            code = process.wait()
    except OSError as error:
        raise verify_backend.ExecutionAborted(str(error)) from error
    if code < 0:
        raise verify_backend.ExecutionAborted(f'the verification command was terminated by signal {-code}')
    if code:
        raise RecipeFailed(code, args)


def snapshot(source, destination):
    source_recipe = recipe(source)
    git_env = git_environment()
    subprocess.run(['git', 'init', '-q', str(destination)], check=True, capture_output=True, env=git_env)
    listing = subprocess.run(['git', '-C', str(source), 'ls-files', '-co', '--exclude-standard', '-z'],
                             capture_output=True, env=git_env, check=False)
    if listing.returncode:
        listing = subprocess.run(['git', f'--git-dir={destination / ".git"}', f'--work-tree={source}',
                                  'ls-files', '-o', '--exclude-standard', '-z'],
                                 check=True, capture_output=True, env=git_env)
    tracked = listing.stdout.split(b'\0')
    revision = subprocess.run(['git', '-C', str(source), 'rev-parse', '--verify', 'HEAD'],
                              text=True, capture_output=True, env=git_env, check=False)
    if revision.returncode == 0:
        subprocess.run(['git', '-C', str(destination), 'fetch', '-q', '--depth=1', str(source),
                        revision.stdout.strip()], check=True, capture_output=True, env=git_env)
        subprocess.run(['git', '-C', str(destination), 'reset', '--mixed', '-q', revision.stdout.strip()],
                       check=True, capture_output=True, env=git_env)
    base = source.resolve()
    inside = destination.resolve().relative_to(base) if destination.resolve().is_relative_to(base) else None
    for raw in set(tracked) - {b''}:
        relative = Path(os.fsdecode(raw))
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('FAIL: invalid source path')
        if inside and (relative == inside or inside in relative.parents):
            continue
        original, target = source / relative, destination / relative
        if not original.exists() and not original.is_symlink():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        resolved = original.resolve()
        if not resolved.is_relative_to(base):
            raise ValueError('FAIL: source symlink escapes the repository')
        if original.is_symlink():
            isolated = destination.resolve() / resolved.relative_to(base)
            target.symlink_to(os.path.relpath(isolated, target.parent.resolve()))
        elif original.is_file():
            shutil.copy2(original, target)
        else:
            raise ValueError('FAIL: source export requires ordinary files or symlinks')
    if revision.returncode:
        subprocess.run(['git', '-C', str(destination), 'add', '-A'], check=True, capture_output=True, env=git_env)
        subprocess.run(['git', '-C', str(destination), '-c', 'user.name=snapshot', '-c', 'user.email=snapshot@localhost',
                        '-c', 'commit.gpgsign=false', 'commit', '-q', '--allow-empty', '--no-verify', '-m', 'snapshot'],
                       check=True, capture_output=True, env=git_env)

    if source_recipe != recipe(destination):
        raise RuntimeError('a complete .fleet/verify.sh or .fleet/verify.py must be included in the verification snapshot')
    if os.environ.get('COLD_VERIFY_SNAPSHOT_RECEIPT'):
        record_snapshot(destination, suite_head=verify_cache.head_sha(destination), suite_tree=verify_cache.tree_sha(destination))


def fetch(remote, dest, folder, pattern, target, limit):
    archive = subprocess.run([*remote, f'tar -czf - -C {dest} {folder}'], capture_output=True, check=True)
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode='r:gz') as packed:
        for entry in packed:
            match = re.fullmatch(f'{folder}/({pattern})', entry.name)
            if not match or not entry.isfile() or entry.size > limit:
                continue
            with tempfile.NamedTemporaryFile(dir=target, delete=False) as output:
                temporary = Path(output.name)
                try:
                    shutil.copyfileobj(packed.extractfile(entry), output)
                    output.flush()
                    os.replace(temporary, target / match.group(1))
                finally:
                    temporary.unlink(missing_ok=True)


def dispatch(directory, backend, repo, generic=False):
    dest = f'/tmp/cold-verify-{uuid.uuid4().hex}'
    with tempfile.TemporaryDirectory(prefix='cold-verify-export-') as stage, verify_backend.shell(backend) as (remote, host):
        stage = Path(stage)
        snapshot(directory, stage / 'checkout')
        shutil.copytree(Path(__file__).resolve().parent, stage / 'ci', ignore=shutil.ignore_patterns('__pycache__', 'evidence', 'tests'))
        evidence_export.write_marker(stage, 'coldverify linux lane', evidence_export.REMOTE_DAYS)
        context = {'GITHUB_REPOSITORY': repo, 'COLD_VERIFY_HOST': host, 'COLD_VERIFY_SNAPSHOT_RECEIPT': f'{dest}/started.json'}
        for name in ('COLD_VERIFY_HEAD', 'COLD_VERIFY_TREE', 'COLD_VERIFY_GATE', 'COLD_VERIFY_NO_CACHE',
                     'COLD_VERIFY_PREVIEW', 'COLD_VERIFY_UNTRUSTED', 'COLD_VERIFY_JOBS', 'COLD_VERIFY_MODEL_RATES',
                     'COLDPASS_REPO', 'AGENT_SANDBOX_REPO', 'SANDBOX_REPO',
                     'READTRIAL_TEST_RUNTIME', 'READTRIAL_TEST_DEPS', 'READTRIAL_TEST_PYTHON'):
            if name in os.environ:
                context[name] = os.environ[name]
        for name, filename in (('COLD_VERIFY_FILTER', 'verify_filter.py'), ('COLD_VERIFY_PARALLEL', 'verify_parallel.py'),
                               ('COLD_VERIFY_CACHE', 'verify_cache.py'), ('COLD_VERIFY_UNITTEST', 'junit_unittest.py'),
                               ('COLD_VERIFY_PYTEST', 'junit_pytest.py')):
            if name in os.environ:
                context[name] = f'{dest}/ci/{filename}'
        if 'COLD_VERIFY_CACHE_DIR' in os.environ:
            context['COLD_VERIFY_CACHE_DIR'] = f'{dest}/cache'
            cache = Path(os.environ['COLD_VERIFY_CACHE_DIR'])
            (stage / 'cache').mkdir()
            if cache.is_dir():
                for entry in cache.iterdir():
                    if re.fullmatch(r'[0-9a-f]{64}\.json', entry.name) and entry.is_file() and not entry.is_symlink():
                        shutil.copyfile(entry, stage / 'cache' / entry.name)
        if 'COLD_VERIFY_JUNIT_DIR' in os.environ:
            context['COLD_VERIFY_JUNIT_DIR'] = f'{dest}/junit'
            (stage / 'junit').mkdir()
            if 'COLD_VERIFY_JUNIT_KEY' in os.environ:
                context['COLD_VERIFY_JUNIT_KEY'] = os.environ['COLD_VERIFY_JUNIT_KEY']
        if 'COLD_VERIFY_CHANGES' in os.environ:
            changes = Path(os.environ['COLD_VERIFY_CHANGES'])
            shutil.copyfile(changes, stage / 'changes.json')
            context['COLD_VERIFY_CHANGES'] = f'{dest}/changes.json'
        assignments = ' '.join(f'{name}={shlex.quote(value)}' for name, value in context.items())
        execution = f'''import platform, runpy, subprocess, sys
from pathlib import Path
sys.path.insert(0, 'ci')
import verify_backend
runner = runpy.run_path('ci/linux-verify.py')
try:
    if platform.system() != 'Linux' or 'microsoft' in platform.release().casefold():
        raise RuntimeError('native verification needs native Linux, not WSL or another OS')
    runner['verify'](Path('checkout').resolve(), generic={generic!r}, local=True)
except runner['RecipeFailed']:
    print('FAIL: real verification command failed', file=sys.stderr)
    sys.exit({REMOTE_PRODUCT_FAILED})
except ValueError as error:
    print(str(error), file=sys.stderr)
    sys.exit({REMOTE_PRODUCT_FAILED})
except verify_backend.ExecutionAborted as error:
    print(f'INCOMPLETE: {{error}}', file=sys.stderr)
    sys.exit({REMOTE_EXECUTION_ABORTED})
except (OSError, RuntimeError, subprocess.SubprocessError) as error:
    print(f'INCOMPLETE: {{error}}', file=sys.stderr)
    sys.exit(2)
'''
        command = (f'rm -rf {dest}; (mkdir -p {dest} && tar -xzf - -C {dest}) || exit 2; '
                   'export PATH="$HOME/.local/bin:$HOME/bin:$PATH"; '
                   f'cd {dest} && command -v python3 >/dev/null || exit 2; '
                   f'{assignments} python3 -c {shlex.quote(execution)}; '
                   f'status=$?; exit $status')
        for attempt in range(CONNECT_ATTEMPTS):
            tar = subprocess.Popen(['tar', '-czf', '-', '-C', str(stage), '.'], stdout=subprocess.PIPE)
            status = subprocess.run([*remote, command], stdin=tar.stdout, check=False).returncode
            tar.stdout.close()
            tar.wait()
            if status != SSH_CONNECTION_FAILED:
                try:
                    if status not in (0, 2, REMOTE_PRODUCT_FAILED, REMOTE_EXECUTION_ABORTED):
                        raise verify_backend.ExecutionAborted(f'the {backend} backend exited {status} without a verdict')
                    observed = subprocess.run([*remote, f'head -c 4096 {dest}/started.json'], text=True,
                                              capture_output=True, check=False)
                    started = json.loads(observed.stdout) if observed.returncode == 0 else {}
                    if not isinstance(started, dict):
                        raise ValueError('the remote suite returned invalid snapshot evidence')
                    record_run_context(snapshot_tree=started.get('snapshot_tree'), suite_head=started.get('suite_head'),
                                       suite_tree=started.get('suite_tree'), backend=backend if started.get('backend') else None)
                    if status == 0 and 'COLD_VERIFY_CACHE_DIR' in context:
                        fetch(remote, dest, 'cache', r'[0-9a-f]{64}\.json', cache, 1_000_000)
                    if status in (0, REMOTE_EXECUTION_ABORTED) and 'COLD_VERIFY_JUNIT_DIR' in context:
                        fetch(remote, dest, 'junit', r'[\w-][\w.-]*\.xml(?:\.sig)?', Path(os.environ['COLD_VERIFY_JUNIT_DIR']), 64_000_000)
                    if status == REMOTE_EXECUTION_ABORTED:
                        raise verify_backend.ExecutionAborted(f'the {backend} backend could not finish the suite')
                    if status == 2:
                        record_run_context(infrastructure_phase=SuitePhase.SETUP)
                    return 1 if status == REMOTE_PRODUCT_FAILED else status
                except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as error:
                    raise verify_backend.ExecutionAborted(f'could not collect evidence from the {backend} backend: {error}') from error
                finally:
                    subprocess.run([*remote, f'rm -rf {dest}'], check=False)
            if attempt < CONNECT_ATTEMPTS - 1:
                time.sleep(random.uniform(0.2, 2 ** min(attempt, 4)))
        subprocess.run([*remote, f'rm -rf {dest}'], check=False)
    raise RuntimeError(f'could not reach {host}')


def submit(directory, repo):
    env = git_environment()
    head = subprocess.run(['git', '-C', str(directory), 'rev-parse', '--verify', 'HEAD'], text=True, capture_output=True, env=env, check=False)
    dirty = subprocess.run(['git', '-C', str(directory), 'status', '--porcelain'], text=True, capture_output=True, env=env, check=False)
    if head.returncode or dirty.returncode or dirty.stdout or not re.fullmatch(r'[\w.-]+/[\w.-]+', repo):
        raise RuntimeError('the coldbox-api backend runs only a clean checkout of a committed OWNER/REPO revision')
    sha = head.stdout.strip()
    code = verify_backend.submit(repo, sha)
    record_backend('coldbox-api')
    print(f"{'FAIL' if code else 'PASS'}: coldbox checks on {repo} at {sha} through the coldbox api", flush=True)
    return code


def recipe(directory):
    for filename, interpreter in (('verify.sh', 'bash'), ('verify.py', sys.executable)):
        if (directory / '.fleet' / filename).is_file():
            return [interpreter, '.fleet/' + filename]
    return None


def verify(directory, generic=False, local=False):
    host = platform.node() if local else native_host()
    with tempfile.TemporaryDirectory(prefix='agentfleet-linux-verify-', dir=os.environ.get('RUNNER_TEMP')) as scratch:
        temporary = Path(scratch)
        source = directory
        directory = temporary / "checkout"
        snapshot(source, directory)
        env = git_environment()
        env.pop('COLD_VERIFY_SNAPSHOT_RECEIPT', None)
        env.pop('GITHUB_STEP_SUMMARY', None)
        for name in ['GH_TOKEN', 'GITHUB_TOKEN', 'FLEET_GITHUB_APP', 'ANTHROPIC_API_KEY',
                     'OPENAI_API_KEY', 'XAI_API_KEY', 'GROK_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN', 'CODEX_API_KEY']:
            env.pop(name, None)
        caller_home = env.get('HOME') if (coldbox_guest() and env.get('GITHUB_ACTIONS') == 'true'
                                          and env.get('GITHUB_JOB') and env.get('HOME') == '/home/runner') else None
        if caller_home:
            for name in list(env):
                if name.casefold() == 'npm_config_cache':
                    del env[name]
        for name, folder in [('HOME', 'home'), ('TMPDIR', 'tmp'), ('XDG_CONFIG_HOME', 'config'),
                             ('XDG_DATA_HOME', 'data'), ('XDG_STATE_HOME', 'state'), ('XDG_CACHE_HOME', 'cache')]:
            target = temporary / folder
            target.mkdir(exist_ok=True)
            env[name] = str(target)
        env.pop('PYTHONDONTWRITEBYTECODE', None)
        env.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8', CI='true')
        if caller_home:
            env['npm_config_cache'] = str(temporary / 'home' / '.npm')
        selected = recipe(directory)
        if selected or generic:
            record_backend('local')
        with npm_content_cache.content_store(caller_home, create=True) as caller_cache, \
                npm_content_cache.content_store(env['HOME'] if caller_home else None, create=True) as private_cache:
            seeded = npm_content_cache.copy_content(caller_cache, private_cache)
            if selected:
                command(selected, directory, env)
            elif generic:
                command([sys.executable, str(Path(__file__).resolve().parent / 'generic_verify.py')], directory, env)
            else:
                raise RuntimeError('a real verification recipe is required at .fleet/verify.sh or .fleet/verify.py')
            npm_content_cache.copy_content(private_cache, caller_cache, exclude=seeded)
    print(f'PASS: real Linux verification on {host}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path, nargs='?', default=Path.cwd())
    parser.add_argument('--platform', action='store_true')
    parser.add_argument('--generic', action='store_true', help='detect and run the repository tests when .fleet/verify.sh is absent')
    parser.add_argument('--export', type=Path, help='write a git checkout of the directory to this new folder and exit')
    args = parser.parse_args()
    try:
        directory = args.directory.resolve()
        if args.export:
            snapshot(directory, args.export.resolve())
            return 0
        repo = repository(directory)
        linux = verify_policy.linux_repository(repo)
        if args.platform:
            if verify_policy.macos_repository(repo):
                print('macos')
            elif linux:
                print('linux')
            else:
                print('local' if runs_locally(repo) else 'linux-generic')
            return 0
        generic = args.generic or (not linux and verify_policy.linux_runner_repository(repo))
        if not linux and not generic:
            raise RuntimeError('INCOMPLETE: repository has no approved Linux exception')
        if generic and not recipe(directory):
            reason = windows_lane.windows_reason(directory)
            if reason:
                raise RuntimeError('a complete .fleet/verify.sh or .fleet/verify.py is required for these checks on Linux: ' + reason)
        backend = verify_backend.configured()
        if backend == 'local' or backend is None and runs_locally(repo):
            if not runs_locally(repo) and (platform.system() != 'Linux' or 'microsoft' in platform.release().casefold()):
                raise RuntimeError('native verification needs native Linux, not WSL or another OS')
            verify(directory, generic, local=True)
            return 0
        if not backend and coldbox_guest():
            verify(directory, generic)
            return 0
        if backend == 'coldbox-api':
            try:
                return submit(directory, repo)
            except verify_backend.NoApi as error:
                print(f'{error}; running in a disposable Coldbox VM instead', file=sys.stderr, flush=True)
        return dispatch(directory, backend or 'coldbox', repo, generic)
    except RecipeFailed:
        print('FAIL: real verification command failed', file=sys.stderr)
        return 1
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    except verify_backend.ExecutionAborted as error:
        record_run_context(infrastructure_phase=SuitePhase.EXECUTION)
        print(f'INCOMPLETE: {error}', file=sys.stderr)
        return 2
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        record_run_context(infrastructure_phase=SuitePhase.SETUP)
        print(f'INCOMPLETE: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
