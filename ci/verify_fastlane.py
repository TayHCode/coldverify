"""Pull request fast lane and secret scan: docs/fast-lane.md."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request

import verify_cache
import verify_repro

ROOT = Path(__file__).resolve().parents[1]
RELEASE_CHECK = ROOT.parent / 'release-check' / 'release_check.py'
RECEIPT = re.compile(r'^Verified-Run:[ \t]*https://github\.com/([\w.-]+/[\w.-]+)/actions/runs/(\d+)[ \t]*\r?$', re.MULTILINE)
ENVIRONMENT = re.compile(r'^\S+ coldverify environment: ([0-9a-f]{64})\r?$', re.MULTILINE)
HUNK = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@')
WARN_ONLY_PATHS = '.fleet/secret-scan-warn-only'
HEURISTIC_KINDS = frozenset({'entropy', 'assigned-secret'})
CONFLICT_MARKER = re.compile(rb'^(?:<{7}|\|{7}|>{7})(?: |$)')


class NotEligible(Exception):
    pass


def files_digest(digest, folder):
    folder = Path(folder)
    if not folder.is_dir():
        digest.update(f'{folder}\0missing\0'.encode())
        return
    for directory, folders, names in os.walk(folder):
        folders[:] = sorted(name for name in folders if name not in ('__pycache__', '.git', '.worktrees', '.cold'))
        for name in sorted(names):
            path = Path(directory, name)
            digest.update(os.fsencode(path.relative_to(folder).as_posix()) + b'\0')
            digest.update(os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes())


def environment():
    digest = hashlib.sha256()
    for argv in (['dpkg-query', '-W'], ['python3', '--version'], ['node', '--version']):
        try:
            found = subprocess.run(argv, capture_output=True, text=True, timeout=120)
            text = found.stdout if found.returncode == 0 else 'unavailable'
        except (OSError, subprocess.TimeoutExpired):
            text = 'unavailable'
        digest.update(f'{argv[0]}\0{text}\0'.encode())
    files_digest(digest, ROOT)
    files_digest(digest, os.environ.get('COLDSTACK_ROOT') or ROOT.parent / 'coldstack')
    coldbox = shutil.which('coldbox')
    digest.update(Path(coldbox).read_bytes() if coldbox else b'no coldbox')
    return digest.hexdigest()


def api(path, raw=False):
    token = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')
    if not token:
        raise NotEligible('no GITHUB_TOKEN to look up the verified run')
    request = urllib.request.Request(os.environ.get('GITHUB_API_URL', 'https://api.github.com') + path)
    request.add_unredirected_header('Authorization', 'Bearer ' + token)
    request.add_header('Accept', 'application/vnd.github+json')
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
    except (OSError, urllib.error.URLError) as error:
        if isinstance(error, urllib.error.HTTPError):
            error.close()
        raise NotEligible(f'GitHub API {path}: {error}') from None
    return body.decode('utf-8', 'replace') if raw else json.loads(body)


def pull_request():
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
    return event['pull_request']


def verified_run(root, pull, current):
    found = set(RECEIPT.findall(pull.get('body') or ''))
    if not found:
        raise NotEligible('the PR description has no Verified-Run line')
    if len(found) > 1:
        raise NotEligible('the PR description has more than one Verified-Run line')
    (repo, number), = found
    if repo != os.environ.get('GITHUB_REPOSITORY'):
        raise NotEligible(f'the verified run is in {repo}, not this repository')
    workflow = os.environ.get('GITHUB_WORKFLOW_REF', '').split('@')[0].split('/', 2)[-1]
    run = api(f'/repos/{repo}/actions/runs/{number}')
    if run.get('path') != workflow or run.get('event') != 'workflow_dispatch':
        raise NotEligible(f'run {number} is not a manual run of {workflow}')
    if run.get('status') != 'completed' or run.get('conclusion') != 'success':
        raise NotEligible(f'run {number} did not pass')
    jobs = api(f"/repos/{repo}/actions/runs/{number}/attempts/{run['run_attempt']}/jobs?per_page=100")['jobs']
    jobs = [job for job in jobs if job.get('name') == os.environ.get('GITHUB_JOB')]
    if len(jobs) != 1 or jobs[0].get('conclusion') != 'success' or 'coldbox' not in (jobs[0].get('labels') or []):
        raise NotEligible(f'run {number} has no passing Coldbox {os.environ.get("GITHUB_JOB")} job')
    seen = set(ENVIRONMENT.findall(api(f"/repos/{repo}/actions/jobs/{jobs[0]['id']}/logs", raw=True)))
    if seen != {current}:
        raise NotEligible(f'run {number} ran on a different toolchain')
    tree = verify_cache.tree_sha(root)
    try:
        verified = verify_repro.git(root, 'rev-parse', run['head_sha'] + '^{tree}').decode().strip()
    except subprocess.CalledProcessError:
        raise NotEligible(f"commit {run['head_sha']} of run {number} is not in this checkout") from None
    if not tree or verified != tree:
        raise NotEligible(f'run {number} verified tree {verified}, but this run tests {tree or "a dirty tree"}')
    return run['html_url'], tree


def scan_base(root, pull):
    checkout = verify_repro.git(root, 'rev-parse', 'HEAD').decode().strip()
    parents = verify_repro.git(root, 'rev-parse', checkout + '^@').decode().split()
    if len(parents) == 2 and parents[1] == pull['head']['sha']:
        return parents[0]
    return verify_repro.git(root, 'merge-base', pull['base']['sha'], checkout).decode().strip()


def load_release_check():
    spec = importlib.util.spec_from_file_location('release_check', RELEASE_CHECK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DIFF_LINE_LIMIT = 1 << 20


def added_lines(root, base, head):
    names = verify_repro.git(root, 'diff', '--name-only', '-z', '--no-renames', '--ignore-submodules',
                             '--diff-filter=A', base, head)
    new = {os.fsdecode(name) for name in names.split(b'\0') if name}
    added = dict.fromkeys(new, ())
    path, line, header = None, 0, False
    with subprocess.Popen(['git', '-C', str(root), '-c', 'core.quotePath=false', 'diff', '-U0', '--no-color',
                           '--no-ext-diff', '--no-renames', '--ignore-submodules', base, head],
                          stdout=subprocess.PIPE, env=verify_repro.clean_environment()) as diff:
        while raw := diff.stdout.readline(DIFF_LINE_LIMIT + 1):
            if len(raw) > DIFF_LINE_LIMIT and not header and raw.startswith(b'-'):
                while not raw.endswith(b'\n') and (raw := diff.stdout.readline(DIFF_LINE_LIMIT + 1)):
                    pass
                continue
            if len(raw) > DIFF_LINE_LIMIT:
                diff.kill()
                raise ValueError(f'a diff line is longer than {DIFF_LINE_LIMIT} bytes')
            text = os.fsdecode(raw.rstrip(b'\n'))
            if text.startswith('diff --git '):
                path, header = None, True
            elif header and text.startswith('+++ '):
                if text.startswith('+++ "'):
                    raise ValueError('git quoted a changed path')
                path = text[6:].removesuffix('\t') if text.startswith('+++ b/') else None
                if path is not None:
                    added[path] = set(added.get(path, ()))
            elif match := HUNK.match(text):
                line, header = int(match[1]), False
            elif not header and text.startswith('+') and path is not None:
                added[path].add(line)
                line += 1
    if diff.returncode:
        raise subprocess.CalledProcessError(diff.returncode, 'git diff')
    return added, new


def warn_only_prefixes(root, base):
    try:
        text = verify_repro.git(root, 'show', f'{base}:{WARN_ONLY_PATHS}').decode()
    except subprocess.CalledProcessError:
        return ()
    prefixes = tuple(text.split())
    for prefix in prefixes:
        if not re.fullmatch(r'(?:[\w.-]+/)+', prefix) or {'.', '..'} & set(prefix.split('/')):
            raise ValueError(f'{WARN_ONLY_PATHS}: each entry must be a relative folder ending in /')
    return prefixes


def scan_secrets(root, base, head='HEAD'):
    """Unreviewed release-check secret findings on the lines head adds to base, as (hits, warnings)."""
    warn_only = warn_only_prefixes(root, base)
    release_check = load_release_check()
    approvals = []
    with tempfile.TemporaryDirectory() as folder:
        manifest = Path(folder, 'release-check.json')
        try:
            manifest.write_bytes(verify_repro.git(root, 'show', head + ':release-check.json'))
        except subprocess.CalledProcessError:
            pass
        else:
            approvals = release_check.manifest_for(Path(root), manifest)['approvals']
    scanner = release_check.Scanner([], approvals=approvals)
    added, new = added_lines(root, base, head)
    hits, warnings = [], []
    for path, lines in sorted(added.items()):
        before = len(scanner.findings)
        scanner.file(verify_repro.git(root, 'cat-file', 'blob', f'{head}:{path}'), 'tree', path)
        for found in scanner.findings[before:]:
            if found['category'] != 'secrets' or found['reviewed']:
                continue
            if not (found['line'] in lines or found['line'] == 0 and path in new):
                continue
            hit = f"{found['kind']} in {path}:{found['line']}"
            if found['kind'] in HEURISTIC_KINDS and path.startswith(warn_only):
                warnings.append(hit)
            else:
                hits.append(hit)
    return hits, warnings


def secret_scan(root, pull):
    try:
        hits, warnings = scan_secrets(root, scan_base(root, pull))
    except Exception as error:
        print(f'INCOMPLETE: secret scan could not run: {type(error).__name__}', flush=True)
        return 2
    for warning in warnings:
        print(f'WARN: secret scan: {warning} (heuristic; does not block in {WARN_ONLY_PATHS} folders)', flush=True)
    for hit in hits:
        print('FAIL: secret scan: ' + hit, flush=True)
    print(f'Secret scan: {"FAIL" if hits else "PASS"} (release-check secret findings on the added lines)', flush=True)
    return 1 if hits else 0


def conflict_markers(root, base, head='HEAD'):
    added, _ = added_lines(root, base, head)
    hits = []
    for path, lines in sorted(added.items()):
        blob = verify_repro.git(root, 'cat-file', 'blob', f'{head}:{path}') if lines else b''
        if b'\0' not in blob:
            hits += [f'{path}:{number}' for number, text in enumerate(blob.split(b'\n'), 1)
                     if number in lines and CONFLICT_MARKER.match(text.removesuffix(b'\r'))]
    return hits


def worktrees_ignored(root):
    found = subprocess.run(['git', '-C', str(root), '-c', 'core.excludesFile=' + os.devnull, 'check-ignore', '-v', '-z',
                            '--no-index', '--stdin'], input=b'.worktrees/probe/x\0', capture_output=True,
                           env=verify_repro.clean_environment())
    if found.returncode not in (0, 1):
        raise subprocess.CalledProcessError(found.returncode, 'git check-ignore')
    source, _, pattern, _ = (found.stdout.split(b'\0') + [b''] * 4)[:4]
    return found.returncode == 0 and Path(os.fsdecode(source)).name == '.gitignore' and not pattern.startswith(b'!')


def lesson_checks(root, pull):
    try:
        base = scan_base(root, pull)
        quoted = warn_only_prefixes(root, base)
        markers = conflict_markers(root, base)
        ignored = worktrees_ignored(root)
    except Exception as error:
        print(f'INCOMPLETE: lesson checks could not run: {type(error).__name__}', flush=True)
        return 2
    for marker in (marker for marker in markers if marker.startswith(quoted)):
        print(f'WARN: L13 conflict marker in {marker} (quoted text; does not block in {WARN_ONLY_PATHS} folders)',
              flush=True)
    markers = [marker for marker in markers if not marker.startswith(quoted)]
    for marker in markers:
        print(f'FAIL: L13 conflict marker in {marker}: finish the merge and delete the marker lines; '
              "a test fixture builds them at run time, for example '<' * 7", flush=True)
    if not ignored:
        print("FAIL: L14 .gitignore does not ignore .worktrees/: add the line /.worktrees/ to the repository's "
              '.gitignore', flush=True)
    failed = markers or not ignored
    print(f'Lesson checks: {"FAIL" if failed else "PASS"} (L13 conflict markers on the added lines, '
          'L14 .worktrees/ ignored)', flush=True)
    return 1 if failed else 0


def scans(root):
    """On a GitHub Actions pull_request event, run the secret scan and lesson checks; return their failing exit code, else None."""
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_EVENT_NAME') != 'pull_request':
        return None
    try:
        pull = pull_request()
    except (OSError, KeyError, ValueError) as error:
        print(f'INCOMPLETE: pull request event unreadable: {type(error).__name__}', flush=True)
        return 2
    scanned, checked = secret_scan(root, pull), lesson_checks(root, pull)
    return scanned or checked or None


NOT_DOCS_FOLDERS = frozenset({'tests', 'test', 'fixtures', 'testdata', '.github', '.fleet'})
NOT_MARKDOWN = ':(exclude,glob)**/*.md'
PLAIN_FILE_MODES = frozenset({'000000', '100644'})


def named_outside_markdown(root, text):
    found = subprocess.run(['git', '-C', str(root), 'grep', '-q', '-F', '-e', text, 'HEAD', '--', '.', NOT_MARKDOWN],
                           capture_output=True, env=verify_repro.clean_environment())
    if found.returncode not in (0, 1):
        raise subprocess.CalledProcessError(found.returncode, 'git grep')
    return found.returncode == 0


def docs_only(root):
    """On a pull_request event, return the changed paths when every one is a .md file that no other tracked file names; else None."""
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_EVENT_NAME') != 'pull_request':
        return None
    try:
        raw = verify_repro.git(root, 'diff', '--raw', '-z', '--no-renames', '--ignore-submodules=none',
                               scan_base(root, pull_request()), 'HEAD')
        fields = os.fsdecode(raw).split('\0')[:-1]
        changes = [(fields[i].split()[:2], fields[i + 1]) for i in range(0, len(fields), 2)]
        paths = sorted({path for _, path in changes})
        if not paths or any(not path.endswith('.md') or NOT_DOCS_FOLDERS & set(path.split('/')[:-1])
                            or not PLAIN_FILE_MODES.issuperset(mode.lstrip(':') for mode in modes)
                            for modes, path in changes):
            return None
        for path in paths:
            folder, _, name = path.rpartition('/')
            last = folder.rpartition('/')[2]
            needles = [name] + ([folder, f"'{last}'", f'"{last}"'] if folder else [])
            if any(named_outside_markdown(root, needle) for needle in needles):
                return None
    except Exception:
        return None
    return paths


def run(root):
    """Return an exit code when the suite must not run, or None to run the full suite."""
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        return None
    current = environment()
    print('coldverify environment: ' + current, flush=True)
    if os.environ.get('GITHUB_EVENT_NAME') != 'pull_request':
        return None
    failed = scans(root)
    if failed:
        return failed
    pull = pull_request()
    try:
        url, tree = verified_run(root, pull, current)
    except (NotEligible, KeyError, ValueError, TypeError) as reason:
        print(f'Full suite: {reason}.', flush=True)
        return None
    print(f'PASS: fast lane: tree {tree} already passed the full suite on this toolchain in {url}', flush=True)
    return 0
