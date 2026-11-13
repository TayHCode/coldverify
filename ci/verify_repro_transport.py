"""Route repro execution to the same native platform as coldverify."""
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

import evidence_export
import verify_repro
from verify_context import coldbox_guest


def bug_fix(root):
    """Return the PR metadata, the branch and whether the change says it fixes a bug."""
    details = verify_repro.metadata(root)
    branch = (os.environ.get('GITHUB_HEAD_REF') if verify_repro.pr_event_applies(root) else None) or verify_repro.git(root, 'branch', '--show-current').decode().strip() or os.environ.get('GITHUB_REF_NAME', '')
    return details, branch, verify_repro.is_bug_fix(details[0], branch, details[1])


def prepare(root, force, reason):
    root = Path(root)
    probe = subprocess.run(['git', '-C', str(root), 'rev-parse', '--is-inside-work-tree'],
                           capture_output=True, env=verify_repro.clean_environment())
    if probe.returncode:
        if force or reason or verify_repro.pr_event_applies(root):
            raise verify_repro.NoRepro('a Git checkout is required to reproduce a change')
        return None
    details, branch, fix = bug_fix(root)
    if not force and not fix:
        if reason:
            raise RuntimeError('an opt-out requires a bug-fix PR or --bug-fix')
        return None
    if verify_repro.git(root, 'status', '--porcelain', '--untracked-files=all').strip():
        raise verify_repro.NoRepro('commit all changes before running the repro gate')
    checkout = verify_repro.git(root, 'rev-parse', 'HEAD').decode().strip()
    if reason is not None and not reason.strip():
        raise RuntimeError('the opt-out reason cannot be empty')
    base = details[2] or ('' if reason is not None else verify_repro.default_base(root))
    head = details[3] or checkout
    if head != checkout:
        base = verify_repro.merge_checkout_base(root, base, head, checkout)
    if reason is not None:
        return {'details': [details[0], details[1], base, head], 'branch': branch, 'checkout': checkout,
                'force': True, 'reason': reason, 'event': os.environ.get('GITHUB_EVENT_NAME', '')}
    for commit in (base, head):
        try:
            verify_repro.git(root, 'cat-file', '-e', commit + '^{commit}')
        except subprocess.CalledProcessError:
            verify_repro.setup_git(root, 'fetch', '--depth=1', 'origin', commit)
    if verify_repro.git(root, 'rev-parse', '--is-shallow-repository').decode().strip() == 'true':
        verify_repro.setup_git(root, 'fetch', '--unshallow', 'origin', base, head)
    verify_repro.git(root, 'merge-base', base, head)
    return {'details': [details[0], details[1], base, head], 'branch': branch, 'checkout': checkout,
            'force': True, 'reason': reason, 'event': os.environ.get('GITHUB_EVENT_NAME', '')}


def remote(folder):
    folder = Path(folder)
    request = json.loads((folder / 'request.json').read_text(encoding='utf-8'))
    expected = request['platform']
    if expected == 'linux' and 'microsoft' in platform.release().casefold():
        raise RuntimeError('native repro cannot execute on WSL')
    if platform.system().lower() != {'macos': 'darwin', 'linux': 'linux'}.get(expected):
        raise RuntimeError('native repro host has the wrong platform')
    if platform.node().casefold() != request['host'].casefold():
        raise RuntimeError('native repro host identity differs')
    checkout = folder / 'checkout'
    env = verify_repro.clean_environment()
    subprocess.run(['git', 'clone', '--quiet', str(folder / 'source.bundle'), str(checkout)], check=True, env=env)
    subprocess.run(['git', '-C', str(checkout), 'checkout', '--quiet', '--detach', request['checkout']], check=True, env=env)
    os.environ['GITHUB_WORKSPACE'] = str(checkout)
    os.environ['GITHUB_HEAD_REF'] = request['branch']
    os.environ['GITHUB_EVENT_NAME'] = request['event']
    code = verify_repro.gate(checkout, request['force'], request['reason'], request['details'], request['exercised'])
    evidence = checkout / '.cold/verify-repro.json'
    if not evidence.is_file():
        raise RuntimeError('native repro returned no evidence')
    receipt = json.loads(evidence.read_text(encoding='utf-8'))
    print('COLD_REPRO_RECEIPT ' + json.dumps(receipt), flush=True)
    return code


def run(root, force=False, reason=None, exercised=False):
    root = Path(root).resolve()
    backend = [None]
    try:
        return route(root, force, reason, exercised, backend)
    except verify_repro.NoRepro as error:
        print('INCOMPLETE: native repro: ' + str(error), file=sys.stderr, flush=True)
        record_error(root, 'INCOMPLETE', error)
        return 2
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        print('FAIL: native repro: ' + str(error), file=sys.stderr, flush=True)
        record_error(root, 'FAIL', error)
        return 1
    finally:
        if backend[0]:
            stamp(root, backend[0])


def stamp(root, backend):
    evidence = root / '.cold/verify-repro.json'
    try:
        report = json.loads(evidence.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return
    if isinstance(report, dict):
        report['backend'] = backend
        evidence.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')


def sweep_stale_transfers(age=86400):
    cutoff = time.time() - age
    for stale in Path(tempfile.gettempdir()).glob('cold-repro-transfer-*'):
        try:
            status = stale.lstat()
            if status.st_uid == os.getuid() and status.st_mtime < cutoff and stale.is_dir() and not stale.is_symlink():
                shutil.rmtree(stale)
        except OSError:
            pass


def route(root, force, reason, exercised, backend):
    request = prepare(root, force, reason)
    if request is None:
        return 0
    here = Path(__file__).resolve().parent
    selected = subprocess.check_output([sys.executable, str(here / 'linux-verify.py'), '--platform', str(root)], text=True).strip()
    import verify_backend
    try:
        spec = None if selected == 'macos' else verify_backend.configured()
    except verify_backend.Unavailable as error:
        raise verify_repro.NoRepro(str(error)) from error
    if spec == 'local' or spec is None and selected == 'local':
        if selected != 'local' and (platform.system() != 'Linux' or 'microsoft' in platform.release().casefold()):
            raise RuntimeError('native repro needs native Linux, not WSL or another OS')
        backend[0] = 'local'
        return verify_repro.gate(root, force, reason, request['details'], exercised)
    if selected in ('local', 'linux', 'linux-generic'):
        selected = 'linux'
        if spec is None and coldbox_guest():
            if platform.system() != 'Linux' or 'microsoft' in platform.release().casefold():
                raise RuntimeError('native repro cannot execute on WSL')
            backend[0] = 'local'
            return verify_repro.gate(root, force, reason, request['details'], exercised)
        spec = spec or 'coldbox'
    elif selected == 'macos':
        import verify_policy
        spec = 'ssh:' + verify_policy.verification_policy()['macos_hosts'][0]
    else:
        raise RuntimeError('unknown verification platform')
    sweep_stale_transfers()
    with tempfile.TemporaryDirectory(prefix='cold-repro-transfer-') as temporary:
        stage = Path(temporary)
        bare = stage / 'objects.git'
        verify_repro.setup_git(stage, 'init', '--bare', '-q', str(bare))
        refs = [(sha, ref) for sha, ref in [(request['details'][2], 'refs/heads/base'), (request['details'][3], 'refs/heads/pr'),
                (request['checkout'], 'refs/heads/candidate')] if sha]
        verify_repro.setup_git(bare, 'fetch', '-q', str(root), *[sha + ':' + ref for sha, ref in refs])
        verify_repro.setup_git(bare, 'bundle', 'create', str(stage / 'source.bundle'), '--all')
        try:
            shutil.rmtree(bare)
            shutil.copy2(here / 'verify_repro.py', stage / 'verify_repro.py')
            shutil.copy2(here / 'verify_context.py', stage / 'verify_context.py')
            shutil.copy2(here / 'evidence_export.py', stage / 'evidence_export.py')
            shutil.copy2(Path(__file__), stage / 'verify_repro_transport.py')
            evidence_export.write_marker(stage, 'coldverify repro', evidence_export.REMOTE_DAYS)
        except OSError as error:
            raise verify_repro.NoRepro('could not stage the repro: ' + verify_repro.excerpt(error)) from error
        backend[0] = spec
        try:
            with verify_backend.shell(spec) as (remote, host):
                return transfer(root, stage, request, selected, host, remote, exercised)
        except verify_backend.Unavailable as error:
            raise verify_repro.NoRepro(verify_repro.excerpt(error)) from error


def transfer(root, stage, request, selected, host, remote, exercised):
    request.update(platform=selected, host=host, exercised=exercised)
    try:
        (stage / 'request.json').write_text(json.dumps(request), encoding='utf-8')
    except OSError as error:
        raise verify_repro.NoRepro('could not stage the repro: ' + verify_repro.excerpt(error)) from error
    remote_dir = '/tmp/cold-repro-' + uuid.uuid4().hex
    reached = subprocess.run([*remote, 'mkdir -m 700 ' + remote_dir], text=True, stderr=subprocess.PIPE)
    if reached.returncode:
        raise verify_repro.NoRepro(f'could not reach {host}: ' + (verify_repro.excerpt(reached.stderr) or f'exit {reached.returncode}'))
    packed = subprocess.Popen(['tar', '-cf', '-', '-C', str(stage), '.'], stdout=subprocess.PIPE)
    received = subprocess.run([*remote, 'tar -xf - -C ' + remote_dir], stdin=packed.stdout, text=True, stderr=subprocess.PIPE)
    packed.stdout.close()
    if packed.wait() or received.returncode:
        raise verify_repro.NoRepro('native repro transfer failed: ' + (verify_repro.excerpt(received.stderr)
                                                                      or f'tar exit {packed.returncode}, remote exit {received.returncode}'))
    result = subprocess.run([*remote, 'COLDBOX_GUEST=0 python3 ' + remote_dir + '/verify_repro_transport.py --remote ' + remote_dir],
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for line in result.stdout.splitlines():
        if not line.startswith('COLD_REPRO_RECEIPT '):
            print(line, flush=True)
    lines = [line.removeprefix('COLD_REPRO_RECEIPT ') for line in result.stdout.splitlines() if line.startswith('COLD_REPRO_RECEIPT ')]
    if not lines and result.returncode == 255:
        raise verify_repro.NoRepro(f'lost the connection to {host}: ' + verify_repro.excerpt(result.stdout))
    if len(lines) != 1:
        raise RuntimeError('native repro returned no receipt: ' + verify_repro.excerpt(result.stdout))
    receipt = json.loads(lines[0])
    if (receipt.get('base') != (request['details'][2] or None) or receipt.get('head') != request['details'][3]
            or receipt.get('host', '').casefold() != host.casefold()
            or receipt.get('platform') != {'macos': 'darwin', 'linux': 'linux'}[selected]):
        raise RuntimeError('native repro receipt commit differs')
    evidence = root / '.cold/verify-repro.json'
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.parent.joinpath('.gitignore').write_text('*\n')
    evidence.write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    code = {'PASS': 0, 'INCOMPLETE': 2}.get(receipt.get('result'), 1)
    return code if result.returncode in (0, code) else 1


def record_error(root, result, error):
    evidence = root / '.cold/verify-repro.json'
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.parent.joinpath('.gitignore').write_text('*\n')
    evidence.write_text(json.dumps({'result': result, 'error': verify_repro.excerpt(error)}, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--remote':
        folder = Path(sys.argv[2])
        try:
            sys.exit(remote(folder))
        finally:
            shutil.rmtree(folder, ignore_errors=True)
    raise SystemExit('internal module')
