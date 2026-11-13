"""Decide whether a checkout needs the Windows verification lane."""
import os
import json
from pathlib import Path
import re
import subprocess

SUFFIXES = {'.ps1', '.psm1', '.psd1', '.cmd', '.bat', '.csproj', '.sln', '.wxs', '.vbs', '.reg'}
SOURCE_SUFFIXES = {'.py', '.js', '.mjs', '.ts', '.rs', '.go', '.sh'}
MARKER = re.compile(rb'sys\.platform\s*(?:==|\.startswith\()\s*[\'"]win|os\.name\s*==\s*[\'"]nt|PureWindowsPath'
                    rb'|platform\.system\(\)\s*==\s*[\'"]Windows|msvcrt|ctypes\.windll|win32api|process\.platform\s*===?\s*[\'"]win32'
                    rb'|cfg\(windows\)|GOOS\s*=\s*[\'"]?windows|powershell(?:\.exe)?\b|cmd\.exe|\b[A-Za-z]:\\\\', re.I)
BASES = ('origin/HEAD', 'origin/main', 'origin/master', 'main', 'master')
MAX_SCAN_BYTES = 2_000_000


def git(directory, *arguments):
    environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    return subprocess.run(['git', '-C', str(directory), *arguments], capture_output=True, env=environment)


def touched(directory):
    listing = git(directory, 'ls-files', '-co', '--exclude-standard', '-z')
    if listing.returncode:
        return None, None
    everything = {name for name in listing.stdout.decode(errors='replace').split('\0') if name}
    event_name = os.environ.get('GITHUB_EVENT_NAME')
    event_checkout = event_name and (not os.environ.get('GITHUB_WORKSPACE') or
                                    Path(os.environ['GITHUB_WORKSPACE']).resolve() == Path(directory).resolve())
    if event_checkout and event_name not in ('push', 'pull_request', 'workflow_dispatch'):
        return None, None
    push = event_checkout and event_name == 'push'
    pull_request = event_checkout and event_name == 'pull_request'
    bases = BASES
    if push:
        try:
            event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
            before, after = event['before'], event['after']
            if not all(isinstance(value, str) and re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', value) for value in (before, after)):
                return None, None
            if git(directory, 'rev-parse', 'HEAD').stdout.decode().strip() != after:
                return None, None
            if set(before) == {'0'}:
                return everything, None
            bases = (before,)
        except (OSError, ValueError, KeyError, TypeError):
            return None, None
    elif pull_request:
        try:
            event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
            base = event['pull_request']['base']['sha']
            if not isinstance(base, str) or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', base):
                return None, None
            bases = (base,)
        except (OSError, ValueError, KeyError, TypeError):
            return None, None
    elif event_checkout and event_name == 'workflow_dispatch':
        try:
            event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
            default = event['repository']['default_branch']
            if not isinstance(default, str) or git(directory, 'check-ref-format', 'refs/heads/' + default).returncode:
                return None, None
            base = git(directory, 'merge-base', 'HEAD', 'refs/remotes/origin/' + default)
            head = git(directory, 'rev-parse', 'HEAD')
            if base.returncode or head.returncode:
                return None, None
            commit = base.stdout.decode().strip()
            if commit == head.stdout.decode().strip():
                raw = git(directory, 'cat-file', '-p', 'HEAD')
                if raw.returncode:
                    return None, None
                parents = [line[7:].decode() for line in raw.stdout.split(b'\n\n', 1)[0].splitlines()
                           if line.startswith(b'parent ')]
                if not parents:
                    return everything, None
                commit = parents[0]
            changed = git(directory, 'diff', '--no-renames', '--name-only', '-z', commit)
            if changed.returncode:
                return None, None
            return everything | {name.decode(errors='replace') for name in changed.stdout.split(b'\0') if name}, commit
        except (OSError, ValueError, KeyError, TypeError):
            return None, None
    for base in bases:
        point = git(directory, 'rev-parse', '--verify', base + '^{commit}') if push else git(directory, 'merge-base', 'HEAD', base)
        if point.returncode == 0:
            commit = point.stdout.decode().strip()
            changed = git(directory, 'diff', '--no-renames', '--name-only', '-z', commit)
            untracked = git(directory, 'ls-files', '-o', '--exclude-standard', '-z')
            if changed.returncode == 0 and untracked.returncode == 0:
                names = changed.stdout.split(b'\0') + untracked.stdout.split(b'\0')
                return {name.decode(errors='replace') for name in names if name}, commit
    return (None, None) if push or pull_request else (everything, None)


def removed_windows_code(directory, commit, names):
    for name in sorted(names):
        if Path(name).suffix.lower() not in SOURCE_SUFFIXES:
            continue
        diff = git(directory, 'diff', '--no-renames', '--no-ext-diff', '--no-textconv', '--text', '-U0', '--no-color', commit, '--', name)
        if diff.returncode:
            return 'cannot inspect removed code; Windows evidence required'
        for line in diff.stdout.split(b'\n'):
            if line.startswith(b'-') and not line.startswith(b'---') and MARKER.search(line):
                return f'change removes Windows-specific code from {name}'
    return None


def windows_reason(directory):
    directory = Path(directory)
    if (directory / '.fleet/verify.ps1').is_file():
        return 'repository ships a Windows recipe (.fleet/verify.ps1)'
    names, commit = touched(directory)
    if names is None:
        return 'cannot inspect changed files; Windows evidence required'
    for name in sorted(names):
        path = directory / name
        if path.suffix.lower() in SUFFIXES or name.startswith('ci/windows/'):
            return f'change touches Windows file {name}'
        if path.suffix.lower() in SOURCE_SUFFIXES and path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_SCAN_BYTES:
            if MARKER.search(path.read_bytes()):
                return f'change touches Windows-specific code in {name}'
    if commit:
        removed = removed_windows_code(directory, commit, names)
        if removed:
            return removed
    return None
