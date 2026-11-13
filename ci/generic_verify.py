#!/usr/bin/env python3
"""Detect and run a repository's tests on Linux when it has no .fleet/verify.sh.

Detects a package.json test script, Python, Cargo and Go. Zero tests is a failure, never a pass.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from junit_unittest import JUNIT_ENV, JUNIT_KEY_ENV, sign, take_junit_environment

IGNORED = {'.git', 'node_modules', '.venv-verify', '.worktrees', '.cold'}


def run(arguments, capture=False, env=None):
    result = subprocess.run(arguments, text=True, stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.STDOUT if capture else None, env=env)
    if capture:
        print(result.stdout, end='', flush=True)
    return result


def require(arguments):
    if run(arguments).returncode:
        print(f'FAILED: {" ".join(arguments)}', flush=True)
        raise SystemExit(1)


def files(root, pattern):
    return [path for path in root.rglob(pattern) if not IGNORED & set(path.relative_to(root).parts)]


def python_tests(root, junit_environment=(None, None)):
    junit, key = junit_environment
    venv = root / '.venv-verify'
    require([sys.executable, '-m', 'venv', str(venv)])
    python = str(venv / 'bin/python')
    for requirements in sorted(root.glob('requirements*.txt')):
        require([python, '-m', 'pip', 'install', '-q', '-r', str(requirements)])
    if (root / 'pyproject.toml').exists() or (root / 'setup.py').exists():
        require([python, '-m', 'pip', 'install', '-q', str(root)])
    config = [path for pattern in ('conftest.py', 'pytest.ini', 'requirements*.txt', 'pyproject.toml') for path in files(root, pattern)]
    if (root / 'conftest.py').exists() or any(b'pytest' in path.read_bytes() for path in config):
        require([python, '-m', 'pip', 'install', '-q', 'pytest'])
        result = run([python, '-m', 'pytest', '-q', *([f'--junit-xml={junit}/pytest.xml'] if junit else [])], capture=True)
        if junit:
            sign(Path(junit) / 'pytest.xml', key)
        count = re.search(r'(\d+) passed', result.stdout)
        return result.returncode, int(count[1]) if count else 0
    starts = ['.'] + (['tests'] if (root / 'tests').is_dir() and not (root / 'tests/__init__.py').exists() else [])
    runner = Path(__file__).with_name('unittest_evidence.py')
    env = {**os.environ, JUNIT_ENV: junit, JUNIT_KEY_ENV: key} if junit and key else None
    result = run([python, str(runner), *starts], capture=True, env=env)
    counts = re.findall(r'(?m)^GENERIC_EXECUTED_TESTS=(\d+)$', result.stdout)
    return result.returncode, int(counts[-1]) if counts else 0


def main():
    root = Path.cwd()
    junit_environment = take_junit_environment()
    package = root / 'package.json'
    if package.is_file() and json.loads(package.read_text()).get('scripts', {}).get('test'):
        require(['npm', 'ci'] if (root / 'package-lock.json').exists() else ['npm', 'install'])
        return run(['npm', 'test']).returncode
    if (root / 'pyproject.toml').exists() or (root / 'setup.py').exists() or files(root, 'test_*.py'):
        code, count = python_tests(root, junit_environment)
        if count < 1:
            print('FAILED: python run reported no tests executed', flush=True)
            return 1
        if code:
            print('FAILED: python tests', flush=True)
            return code
        print(f'python tests passed: {count}', flush=True)
        return 0
    if (root / 'Cargo.toml').exists():
        return run(['cargo', 'test']).returncode
    if (root / 'go.mod').exists():
        return run(['go', 'test', './...']).returncode
    print('FAILED: no tests detected. Add .fleet/verify.sh that runs this repo\'s tests.', flush=True)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
