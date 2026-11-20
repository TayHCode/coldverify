"""Working-tree identity and host-local concurrency for verify traces."""
import hashlib
import json
import os
import platform
import re
import stat
import time
import uuid
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path


class SuitePhase(StrEnum):
    SETUP = 'setup'
    EXECUTION = 'execution'


def snapshot_tree(root):
    digest = hashlib.sha256()
    def field(value):
        digest.update(len(value).to_bytes(8, 'big'))
        digest.update(value)
    entries = []
    root = Path(root)
    def failed(error):
        raise error
    for directory, folders, files in os.walk(root, followlinks=False, onerror=failed):
        if Path(directory) == root:
            folders[:] = [name for name in folders if name != '.git']
            files = [name for name in files if name != '.git']
        entries.extend(Path(directory) / name for name in files)
        entries.extend(Path(directory) / name for name in folders if (Path(directory) / name).is_symlink())
    for path in sorted(entries, key=lambda p: os.fsencode(p.relative_to(root).as_posix())):
        relative = path.relative_to(root)
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if stat.S_ISLNK(mode):
            data = os.fsencode(os.readlink(path))
        elif stat.S_ISREG(mode):
            data = path.read_bytes()
        else:
            raise ValueError('unsupported snapshot entry')
        field(os.fsencode(relative.as_posix()))
        field(format(stat.S_IFMT(mode) | stat.S_IMODE(mode), 'o').encode())
        field(data)
    return digest.hexdigest()


def record_snapshot(root, **identity):
    receipt = os.environ.get('COLD_VERIFY_SNAPSHOT_RECEIPT')
    if receipt:
        Path(receipt).write_text(json.dumps({'snapshot_tree': snapshot_tree(root),
                                            'snapshot_ready_at': time.time(), **identity}))



def record_backend(backend):
    record_run_context(backend=backend)


def record_run_context(**fields):
    receipt = os.environ.get('COLD_VERIFY_SNAPSHOT_RECEIPT')
    if receipt:
        path = Path(receipt)
        recorded = json.loads(path.read_text()) if path.exists() else {}
        path.write_text(json.dumps({**recorded, **fields}))


def lock(file, blocking=True):
    file.seek(0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(file.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(file, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))


@contextmanager
def concurrent_run(directory=None):
    directory = Path(directory) if directory is not None else Path.home() / '.local/state/cold/verify-runs'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / 'registry').open('a+b') as guard:
        if guard.tell() == 0:
            guard.write(b'0')
            guard.flush()
        lock(guard)
        path = directory / (uuid.uuid4().hex + '.run')
        active = path.open('w+b')
        active.write(b'0')
        active.flush()
        lock(active)
        count = 1
        for other in directory.glob('*.run'):
            if other == path:
                continue
            with other.open('r+b') as candidate:
                try:
                    lock(candidate, blocking=False)
                except OSError:
                    count += 1
                    continue
            other.unlink()
    try:
        yield count
    finally:
        with (directory / 'registry').open('r+b') as guard:
            lock(guard)
            active.close()
            path.unlink()


COLDBOX_HOST_FILE = Path('/etc/coldbox/host')


def coldbox_guest():
    if os.environ.get('COLDBOX_GUEST') == '0':
        return None
    try:
        marker = COLDBOX_HOST_FILE.read_text().strip()
    except OSError:
        return None
    import verify_policy
    hosts = verify_policy.verification_policy()['linux_hosts']
    return next((host for host in hosts if host.casefold() == marker.casefold()), None)


def host_name():
    return coldbox_guest() or platform.node()


REDACTED = '[REDACTED]'
SECRET_NAME = re.compile(r'token|secret|passw|api[_-]?key|credential|private[_-]?key', re.IGNORECASE)
REDACTIONS = [
    (re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)', re.DOTALL), REDACTED),
    (re.compile(r'(?i)(\bauthorization\s*[:=]\s*(?:bearer|basic|token)\s+)\S+'), r'\1' + REDACTED),
    (re.compile(r'(?i)(\b[\w.-]*(?:token|secret|passw\w*|api[_-]?key|credential\w*)[\w.-]*\s*[=:]\s*)("[^"\n]*"|\'[^\'\n]*\'|[^\s,;]+)'),
     r'\1' + REDACTED),
    (re.compile(r'(://)[^/\s:@]+:[^/\s@]+@'), r'\1' + REDACTED + '@'),
    (re.compile(r'\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}|\bsk-[A-Za-z0-9_-]{16,}'
                r'|\bAKIA[0-9A-Z]{16}\b|\bxox[abprs]-[A-Za-z0-9-]{10,}'
                r'|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}'), REDACTED),
]


def redact(text, environ=None):
    for pattern, replacement in REDACTIONS:
        text = pattern.sub(replacement, text)
    values = {value for name, value in (environ if environ is not None else os.environ).items()
              if SECRET_NAME.search(name) and len(value) >= 8}
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, REDACTED)
    return text
