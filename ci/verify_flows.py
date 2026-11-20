"""Run the project's declared verification flows (.fleet/flows/*) on the repro base and head."""
import contextlib
import http.server
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import uuid
import urllib.error
import urllib.request
from http.client import HTTPException
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import verify_backend
import verify_flow_systemd
import verify_repro
from verify_context import host_name, redact

FLOWS = '.fleet/flows'
EVIDENCE = Path('.cold/verify-flows.json')
REPRO = Path('.cold/verify-repro.json')
ARTIFACTS = Path('.cold/flows')
PASSED_THROUGH = ('CDP_ENDPOINT',)
TARGET = 'COLD_DEPLOYED_TARGET'
PROBE_TIMEOUT = 10
NOT_PROVEN = 'flow contact with the target is not proven'
PROVEN = "each flow got a 2xx or 3xx answer from the target through coldverify's read-only proxy"
READS = ('GET', 'HEAD')
BODY_LIMIT = 16 * 1024 * 1024
HOP_BY_HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'proxy-connection', 'te', 'trailer',
              'transfer-encoding', 'upgrade', 'host', 'content-length'}


def declared(root, commit):
    listing = verify_repro.git(root, 'ls-tree', '-z', commit, '--', FLOWS + '/').decode(errors='replace')
    return sorted(Path(path).name for entry in listing.split('\0') if entry
                  for meta, path in [entry.split('\t', 1)] if meta.split()[0] == '100755')


def present(root, commit):
    return not subprocess.run(['git', '-C', str(root), 'cat-file', '-e', commit + '^{commit}'],
                              capture_output=True, env=verify_repro.clean_environment()).returncode


def kind(base_exit, head_exit):
    if head_exit == 1:
        return 'regression'
    if base_exit not in (0, 1) or head_exit != 0:
        return 'unrunnable'
    return 'proof' if base_exit == 1 else 'acceptance'


def proof_observations(report, repro):
    if not report or not repro or not (repro.get('base') and repro.get('head')):
        return {}
    if (report['base'], report['head']) != (repro['base'], repro['head']):
        return {}
    return {
        f"{FLOWS}/{flow['name']}": {'base': flow['base_output'], 'head': flow['head_output']}
        for flow in report['flows']
        if flow['kind'] == 'proof' and (flow['base_exit'], flow['head_exit']) == (1, 0)
        and flow['base_output'] and flow['head_output'] and flow['base_output'] != flow['head_output']
    }


def keep(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = source.read_bytes()
    try:
        destination.write_text(redact(data.decode('utf-8')), encoding='utf-8')
    except UnicodeDecodeError:
        destination.write_bytes(data)


def run_flow(flow, checkout, side, scratch, saved, extra=None, sandbox_root=None):
    home = scratch / f'{side}-home'
    produced = scratch / f'{side}-artifacts'
    home.mkdir()
    produced.mkdir()
    env = verify_repro.case_environment(home)
    env.update({key: os.environ[key] for key in PASSED_THROUGH if key in os.environ}, COLD_FLOW_ARTIFACTS=str(produced))
    env.update(extra or {})
    log = scratch / f'{side}.log'
    with log.open('w+b') as output:
        try:
            if sandbox_root is not None:
                prefix = verify_repro.sandbox(sandbox_root, (checkout, home, produced), False,
                                              optional_readable=(sys.prefix, sys.base_prefix))
                if verify_flow_systemd.needs_manager(flow):
                    code = verify_flow_systemd.run(prefix, flow, checkout, env, output, log.with_suffix('.exit'))
                else:
                    code = verify_repro.run_sandboxed(prefix, [str(flow)], checkout, env, output=output)
            else:
                code = verify_repro.run_test([str(flow)], checkout, env, output=output, errors=subprocess.STDOUT)
        except OSError as error:
            output.write(str(error).encode())
            code = 127
    return code, collect(log, produced, saved)


def kept_member(member, path):
    if not (member.isfile() or member.isdir()):
        return None
    try:
        return tarfile.data_filter(member, path)
    except tarfile.FilterError:
        return None


def run_remote(remote, flow, checkout, side, scratch, saved):
    there = f'/tmp/cold-flow-{uuid.uuid4().hex}'

    def send(command, **options):
        try:
            return subprocess.run([*remote, command], timeout=verify_repro.CASE_TIMEOUT, **options)
        except subprocess.TimeoutExpired as error:
            raise verify_backend.Unavailable(f'flow {flow.name} on {side} gave no answer within '
                                             f'{verify_repro.CASE_TIMEOUT} s on the backend') from error

    packed = subprocess.Popen(['tar', '-cf', '-', '--exclude=./.git', '-C', str(checkout), '.'], stdout=subprocess.PIPE)
    sent = send(f'mkdir -m 700 {there} && mkdir {there}/checkout {there}/home {there}/artifacts && '
                f'tar -xf - -C {there}/checkout', stdin=packed.stdout, stderr=subprocess.PIPE, text=True)
    packed.stdout.close()
    if packed.wait() or sent.returncode:
        raise verify_backend.Unavailable(f'could not send {side} to the backend: '
                                         + (verify_repro.excerpt(sent.stderr) or f'exit {sent.returncode}'))
    env = {key: value for key, value in verify_repro.case_environment(f'{there}/home').items()
           if key not in ('PATH', 'SYSTEMROOT', 'WINDIR', 'PATHEXT', 'USERPROFILE')}
    env.update({key: os.environ[key] for key in PASSED_THROUGH if key in os.environ}, COLD_FLOW_ARTIFACTS=f'{there}/artifacts')
    run = ('env -i PATH="$PATH" ' + ' '.join(shlex.quote(f'{key}={value}') for key, value in env.items())
           + f' {there}/checkout/{FLOWS}/{shlex.quote(flow.name)} > {there}/output.txt 2>&1 </dev/null')
    command = f'cd {there}/checkout || exit 125; {run}; code=$?; echo $code > {there}/exit; exit $code'
    ran = send(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    archive = scratch / f'{side}.tar'
    with archive.open('wb') as output:
        fetched = send(f'cd {there} && tar -cf - output.txt exit artifacts; status=$?; rm -rf {there}; exit $status',
                       stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.PIPE, text=False)
    back = scratch / f'{side}-back'
    try:
        if fetched.returncode:
            raise OSError(verify_repro.excerpt(fetched.stderr.decode(errors='replace')) or f'exit {fetched.returncode}')
        with tarfile.open(archive) as members:
            members.extractall(back, filter=kept_member)
        recorded = int((back / 'exit').read_text().strip())
        if not (back / 'output.txt').is_file():
            raise OSError('output.txt is missing or not a regular file')
        if recorded != ran.returncode:
            raise ValueError(f'the backend exited {ran.returncode} but the flow recorded {recorded}')
    except (OSError, ValueError, tarfile.TarError) as error:
        raise verify_backend.Unavailable(f'flow {flow.name} on {side} returned no result from the backend: {error}') from error
    return ran.returncode, collect(back / 'output.txt', back / 'artifacts', saved), redact(shlex.join([*remote, command]))


def collect(log, produced, saved):
    saved.mkdir(parents=True, exist_ok=True)
    (saved / 'output.txt').write_text(redact(log.read_bytes().decode('utf-8', errors='replace')), encoding='utf-8')
    for path in sorted(produced.rglob('*')):
        if path.is_file() and not path.is_symlink():
            keep(path, saved / 'artifacts' / path.relative_to(produced))
    return verify_repro.excerpt((saved / 'output.txt').read_text(encoding='utf-8', errors='replace'),
                                      verify_repro.OUTPUT_LIMIT)


def last_words(root, flow, side):
    text = (root / ARTIFACTS / flow['name'] / side / 'output.txt').read_text(encoding='utf-8', errors='replace')
    last = next((line for line in reversed(text.splitlines()) if line.strip()), 'no output')
    return f"{side} exit {flow[f'{side}_exit']}: {verify_repro.excerpt(last, 200)}"


def judge(root, flows):
    failed = [flow['name'] for flow in flows if flow['kind'] == 'regression']
    if failed:
        return 'FAIL', 'flows failed on head: ' + ', '.join(failed)
    unrunnable = [f"{flow['name']} (" + '; '.join(last_words(root, flow, side) for side in ('base', 'head')
                                                   if flow[f'{side}_exit'] not in (0, 1)) + ')'
                  for flow in flows if flow['kind'] == 'unrunnable']
    if unrunnable:
        return 'INCOMPLETE', 'flows could not run: ' + ', '.join(unrunnable)
    proofs = sum(flow['kind'] == 'proof' for flow in flows)
    return 'PASS', f'{len(flows)} flows passed on head, {proofs} of them failed on base'


def run_both(root, name, base, head, folder, remote=None):
    checkouts = {side: folder / side for side in ('base', 'head')}
    added = []
    try:
        for side, commit in (('base', base), ('head', head)):
            verify_repro.setup_git(root, 'worktree', 'add', '--detach', str(checkouts[side]), commit)
            added.append(checkouts[side])
        if (checkouts['base'] / '.fleet').is_symlink():
            raise verify_repro.NoRepro('the base worktree has a symlinked .fleet directory')
        shutil.rmtree(checkouts['base'] / FLOWS, ignore_errors=True)
        shutil.copytree(checkouts['head'] / FLOWS, checkouts['base'] / FLOWS, symlinks=True)
        result = {'name': name, 'commands': []}
        for side in ('base', 'head'):
            flow, saved = checkouts[side] / FLOWS / name, root / ARTIFACTS / name / side
            if remote:
                code, output, command = run_remote(remote, flow, checkouts[side], side, folder, saved)
            else:
                sandbox_root = root if platform.system() == 'Linux' else None
                code, output = run_flow(flow, checkouts[side], side, folder, saved, sandbox_root=sandbox_root)
                command = f'cd {shlex.quote(str(checkouts[side]))} && {shlex.quote(str(flow))}'
            result.update({f'{side}_exit': code, f'{side}_output': output})
            result['commands'].append(command)
            print(f'Flow {name} on {side}: exit {code}', flush=True)
    finally:
        for checkout in reversed(added):
            verify_repro.git(root, 'worktree', 'remove', '--force', str(checkout))
    result['kind'] = kind(result['base_exit'], result['head_exit'])
    return result


def evaluate(root, base, head, there, backend):
    here = host_name()
    if not base or not head:
        return 'INCOMPLETE', 'flows: the repro gate recorded no base and head to run them on', [], here
    backend = backend or 'local'
    if not isinstance(backend, str) or not verify_backend.SPEC.fullmatch(backend):
        return 'INCOMPLETE', f'flows: the repro named an unknown backend {backend!r}', [], here
    if backend.startswith('ssh:'):
        return 'INCOMPLETE', 'flows do not run on an ssh: backend: the repro ran on ' + backend, [], here
    if backend == 'local' and (there or '').casefold() != here.casefold():
        return 'INCOMPLETE', f'flows run only on the repro host: the repro ran on {there or "no host"}, this is {here}', [], here
    for commit in (base, head):
        if not present(root, commit):
            verify_repro.setup_git(root, 'fetch', '--depth=1', 'origin', commit)
    names = declared(root, head)
    missing = sorted(set(declared(root, base)) - set(names))
    if missing:
        return 'INCOMPLETE', 'flows on base missing from head (rebase onto base): ' + ', '.join(missing), [], here
    with tempfile.TemporaryDirectory(prefix='cold-flows-', dir=os.environ.get('RUNNER_TEMP') or None) as temporary:
        if backend == 'local':
            flows = [run_both(root, name, base, head, Path(temporary) / name) for name in names]
        else:
            with verify_backend.shell(backend) as (remote, here):
                flows = [run_both(root, name, base, head, Path(temporary) / name, remote) for name in names]
    return *judge(root, flows), flows, here


def reset(root):
    shutil.rmtree(root / ARTIFACTS, ignore_errors=True)
    (root / EVIDENCE).unlink(missing_ok=True)


def save(root, result, reason, base, head, flows, host=None, **extra):
    report = {'result': result, 'reason': reason, 'base': base, 'head': head, 'host': host or host_name(), 'flows': flows, **extra}
    (root / EVIDENCE).write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(f'Flows: {result}: {reason}', flush=True)
    return report


def failure(error):
    detail = error.stderr.decode(errors='replace') if isinstance(error, subprocess.CalledProcessError) and error.stderr else error
    return 'INCOMPLETE', 'flows: ' + verify_repro.excerpt(detail), []


class Unredirected(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def direct():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), Unredirected)


def ask(url):
    opener = direct()
    try:
        with opener.open(urllib.request.Request(url, method='HEAD'), timeout=PROBE_TIMEOUT) as response:
            status = response.status
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
    except (urllib.error.URLError, OSError, ValueError, HTTPException) as error:
        return None, f'HEAD {url} got no response: {getattr(error, "reason", error)}'
    return f'HEAD {url} answered HTTP {status}', None


def probe(target):
    url = target if '://' in target else f'http://{target}/'
    answers = []
    asking = threading.Thread(target=lambda: answers.append(ask(url)), daemon=True)
    asking.start()
    asking.join(PROBE_TIMEOUT)
    return answers[0] if answers else (None, f'HEAD {url} got no response within {PROBE_TIMEOUT}s')


class ReadOnly(http.server.BaseHTTPRequestHandler):
    def __getattr__(self, name):
        if name.startswith('do_'):
            return self.relay
        raise AttributeError(name)

    def relay(self):
        if self.command not in READS or not self.path.startswith('/'):
            self.record(405, True)
            self.send_error(405, 'coldverify lets a deployed flow send only GET and HEAD requests for a path on the target')
        else:
            self.forward()

    def record(self, status, refused=False):
        entry = (self.command, self.path.split('?', 1)[0], status, refused)
        with self.server.lock:
            self.server.exchanges.append(entry)

    @contextlib.contextmanager
    def replying(self, status):
        entry = (self.command, self.path.split('?', 1)[0], status, False)
        with self.server.lock:
            self.server.exchanges.append(entry)
            self.server.pending.append(entry)
        try:
            yield
        except BaseException:
            with self.server.lock:
                self.server.exchanges[:] = [other for other in self.server.exchanges if other is not entry]
            raise
        finally:
            with self.server.lock:
                self.server.pending[:] = [other for other in self.server.pending if other is not entry]

    def fail(self, message):
        with self.replying(502):
            self.send_error(502, message)

    def forward(self):
        headers = {key: value for key, value in self.headers.items() if key.lower() not in HOP_BY_HOP}
        request = urllib.request.Request(self.server.origin + self.path, method=self.command, headers=headers)
        try:
            with direct().open(request, timeout=PROBE_TIMEOUT) as response:
                status, answer, body = response.status, response.headers, response.read(BODY_LIMIT + 1)
        except urllib.error.HTTPError as error:
            status, answer, body = error.code, error.headers, error.read(BODY_LIMIT + 1)
            error.close()
        except (urllib.error.URLError, OSError, ValueError, HTTPException) as error:
            self.fail(f'coldverify got no answer from the target: {getattr(error, "reason", error)}')
            return
        if len(body) > BODY_LIMIT:
            self.fail(f'the target answered with more than {BODY_LIMIT} bytes')
            return
        location = self.inside(answer['Location']) if 'Location' in answer and 300 <= status < 400 else ''
        if location is None:
            self.fail('the target redirected away from itself')
            return
        with self.replying(status):
            self.send_response(status)
            for key, value in answer.items():
                if key.lower() == 'location' and location:
                    self.send_header(key, location)
                elif key.lower() not in HOP_BY_HOP:
                    self.send_header(key, value)
            self.send_header('Content-Length', answer.get('Content-Length', '0') if self.command == 'HEAD' else str(len(body)))
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)

    def inside(self, location):
        if '\\' in location or location.lstrip().startswith('//'):
            return None
        parts = urlsplit(location)
        if not parts.scheme and not parts.netloc:
            return location
        if origin(location) != origin(self.server.origin):
            return None
        return urlunsplit(('http', self.server.local, parts.path, parts.query, parts.fragment))

    def log_message(self, *args):
        pass


def origin(url):
    parts = urlsplit(url)
    try:
        port = parts.port or {'http': 80, 'https': 443}.get(parts.scheme.lower())
    except ValueError:
        return None
    return parts.scheme.lower(), (parts.hostname or '').lower(), port


@contextlib.contextmanager
def read_only(target):
    parts = urlsplit(target if '://' in target else f'http://{target}')
    server = http.server.ThreadingHTTPServer(('localhost', 0), ReadOnly)
    server.origin, server.lock, server.pending = f'{parts.scheme}://{parts.netloc}', threading.Lock(), []
    exchanges = server.exchanges = []
    server.local = local = f'localhost:{server.server_port}'
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield (urlunsplit(('http', local, parts.path, parts.query, parts.fragment)) if '://' in target else local), exchanges
    finally:
        server.shutdown()
        with server.lock:
            exchanges[:] = [entry for entry in exchanges if not any(entry is other for other in server.pending)]
            server.exchanges = []
        server.server_close()


def refusal(exchanges):
    return next((f'{method} {path}' for method, path, _, refused in exchanges if refused), None)


def reached(exchanges):
    return any(not refused and 200 <= status < 400 for _, _, status, refused in exchanges)


def run_deployed(root, name, head, target, folder):
    checkout = folder / 'head'
    saved = root / ARTIFACTS / name / 'deployed'
    verify_repro.setup_git(root, 'worktree', 'add', '--detach', str(checkout), head)
    try:
        with read_only(target) as (proxied, exchanges):
            code, output = run_flow(checkout / FLOWS / name, checkout, 'deployed', folder, saved, {TARGET: proxied})
    finally:
        verify_repro.git(root, 'worktree', 'remove', '--force', str(checkout))
    exchanges = list(exchanges)
    (saved / 'exchanges.txt').write_text(redact(''.join(
        f"{method} {path} {status}{' refused' if refused else ''}\n" for method, path, status, refused in exchanges)), encoding='utf-8')
    print(f'Flow {name} against {target}: exit {code}, {len(exchanges)} requests through the read-only proxy', flush=True)
    return {'name': name, 'base_exit': None, 'head_exit': code,
            'kind': {0: 'deployed', 1: 'regression'}.get(code, 'unrunnable'), 'base_output': '', 'head_output': output,
            'commands': [f'cd {shlex.quote(str(checkout))} && {TARGET}={shlex.quote(target)} '
                         f'{shlex.quote(str(checkout / FLOWS / name))}']}, exchanges


def judge_deployed(checked, target, skipped):
    flows = [flow for flow, _ in checked]
    writes = [f"{flow['name']} ({refusal(exchanges)})" for flow, exchanges in checked if refusal(exchanges)]
    if writes:
        return 'INCOMPLETE', f'flows tried to change {target} and coldverify refused it: ' + redact(', '.join(writes))
    failed, unrunnable = (', '.join(flow['name'] for flow in flows if flow['kind'] == kind) for kind in ('regression', 'unrunnable'))
    if failed:
        return 'FAIL', f'flows failed against {target}: ' + failed
    if unrunnable:
        return 'INCOMPLETE', f'flows could not run against {target}: ' + unrunnable
    if not flows:
        return 'INCOMPLETE', f'no flow in {FLOWS}/ reads {TARGET}, so none checked {target}'
    silent = [flow['name'] for flow, exchanges in checked if not reached(exchanges)]
    if silent:
        return 'INCOMPLETE', f'flows passed without a 2xx or 3xx answer from {target} through coldverify\'s read-only proxy: ' + ', '.join(silent)
    unread = f'; not run, they do not read {TARGET}: ' + ', '.join(skipped) if skipped else ''
    return 'PASS', f'{len(flows)} flows passed against {target}' + unread


def deployed(root, target):
    root = Path(root)
    reset(root)
    head = contact = None
    flows = []
    try:
        head = verify_repro.git(root, 'rev-parse', 'HEAD').decode().strip()
        names = declared(root, head)
        reading = [name for name in names if TARGET.encode() in verify_repro.git(root, 'show', f'{head}:{FLOWS}/{name}')]
        answered, error = probe(target) if reading else (None, None)
        if reading and not answered:
            result, reason = 'INCOMPLETE', f'coldverify could not reach {target} ({error}), so no flow ran against it'
        else:
            with tempfile.TemporaryDirectory(prefix='cold-flows-', dir=os.environ.get('RUNNER_TEMP') or None) as temporary:
                checked = [run_deployed(root, name, head, target, Path(temporary) / name) for name in reading]
            flows = [flow for flow, _ in checked]
            result, reason = judge_deployed(checked, target, [name for name in names if name not in reading])
            if answered:
                proven = all(reached(exchanges) for _, exchanges in checked)
                contact = {'established_by': 'probed by coldverify', 'probe': answered,
                           'flow_contact': 'proven' if proven else 'not proven'}
                reason += f"; target contact {contact['established_by']} ({answered}); {PROVEN if proven else NOT_PROVEN}"
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        result, reason, flows = failure(error)
    return save(root, result, reason, None, head, flows, contact=contact)


def run(root, repro=None):
    root = Path(root)
    reset(root)
    if repro is None:
        try:
            repro = json.loads((root / REPRO).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            repro = {}
    base, head = repro.get('base'), repro.get('head')
    try:
        if not any(declared(root, commit) for commit in dict.fromkeys(('HEAD', head, base)) if commit and present(root, commit)):
            return None
        result, reason, flows, host = evaluate(root, base, head, repro.get('host'), repro.get('backend'))
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        (result, reason, flows), host = failure(error), None
    return save(root, result, reason, base, head, flows, host)
