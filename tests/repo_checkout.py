import atexit
import shutil
import subprocess
import tempfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
_snapshot = None


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout


def is_checkout(path):
    found = subprocess.run(['git', '-C', str(path), 'rev-parse', '--show-toplevel'], capture_output=True, text=True)
    return found.returncode == 0 and Path(found.stdout.strip()).resolve() == path.resolve()


def root():
    global _snapshot
    if is_checkout(SOURCE):
        return SOURCE
    if _snapshot is None:
        parent = Path(tempfile.mkdtemp(prefix='coldverify-checkout-'))
        atexit.register(shutil.rmtree, parent, True)
        snapshot = parent / SOURCE.name
        shutil.copytree(SOURCE, snapshot, symlinks=True, ignore=shutil.ignore_patterns('__pycache__'))
        git(snapshot, 'init', '--quiet')
        git(snapshot, 'add', '--all', '--force')
        git(snapshot, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
            '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
            'commit', '--quiet', '--no-verify', '-m', 'Working-tree snapshot')
        _snapshot = snapshot
    return _snapshot
