#!/usr/bin/env python3
"""Run one verify step, skipping it when its inputs and recipe are byte-identical to an earlier pass.

    verify_cache.py step TITLE [--input PATH]... -- COMMAND [ARGS...]

Run from the repository root (a recipe's working directory). Invariant and
environment: docs/verify-cache.md
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

CACHE_SCHEMA = 1
RECIPES = ('.fleet/verify.sh', '.fleet/verify.py', '.fleet/verify.ps1')
ENV_NAMES = ('COLD_VERIFY_CACHE', 'COLD_VERIFY_PARALLEL', 'COLD_VERIFY_CACHE_DIR', 'COLD_VERIFY_HEAD', 'COLD_VERIFY_TREE',
             'COLD_VERIFY_GATE', 'COLD_VERIFY_NO_CACHE', 'COLD_VERIFY_FILTER', 'COLD_VERIFY_CHANGES', 'COLD_VERIFY_PREVIEW')
GIT_VARIABLES = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR',
                 'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES')


class Uncacheable(Exception):
    pass


def git_output(root, *args):
    env = {key: value for key, value in os.environ.items() if key not in GIT_VARIABLES}
    result = subprocess.run(['git', '-C', str(root), *args], text=True, capture_output=True, env=env, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def head_sha(root):
    return git_output(root, 'rev-parse', 'HEAD') or None


def tree_sha(root):
    """Tree of HEAD, only when the working tree has no uncommitted, untracked or ignored files (except .cold/)."""
    status = git_output(root, 'status', '--porcelain', '--untracked-files=all', '--ignored')
    if status is None or any(not line.startswith('!! .cold/') for line in status.splitlines()):
        return None
    return git_output(root, 'rev-parse', 'HEAD^{tree}') or None


def recipe_contents(root):
    return {name: Path(root, name).read_bytes() for name in RECIPES if Path(root, name).is_file()}


def directory_files(directory):
    files = []
    for folder, folders, names in os.walk(directory):
        for name in folders + names:
            if Path(folder, name).is_symlink():
                raise Uncacheable(f'input directory contains a symlink: {Path(folder, name)}')
        files += [Path(folder, name) for name in names]
    return sorted(files)


def input_files(root, paths):
    """Declared input paths (files, or directories walked in sorted order) mapped to their bytes."""
    base = Path(root).resolve()
    contents = {}
    for declared in paths:
        path = Path(declared)
        if path.is_absolute() or '..' in path.parts:
            raise Uncacheable(f'input must be a relative path inside the repository: {declared}')
        resolved = (base / path).resolve()
        if not resolved.is_relative_to(base):
            raise Uncacheable(f'input leaves the repository: {declared}')
        if resolved.is_dir():
            files = directory_files(resolved)
        elif resolved.is_file():
            files = [resolved]
        else:
            raise Uncacheable(f'input is missing: {declared}')
        for file in files:
            if not file.resolve().is_relative_to(base):
                raise Uncacheable(f'input leaves the repository: {file}')
            contents[file.relative_to(base).as_posix()] = file.read_bytes()
    return contents


def inputs_hash(recipes, step, inputs=None):
    """sha256 over the step id, recipe bytes and declared input bytes.

    Every field is tagged and length-prefixed so no two different sets of
    names and contents can produce the same byte stream.
    """
    inputs = inputs or {}
    if not recipes and not inputs:
        return None
    digest = hashlib.sha256()

    def field(tag, value):
        data = value if isinstance(value, bytes) else value.encode()
        digest.update(tag + len(data).to_bytes(8, 'big') + data)

    field(b'S', step)
    for kind, files in ((b'R', recipes), (b'I', inputs)):
        for name in sorted(files):
            field(kind + b'N', name)
            field(kind + b'C', files[name])
    return digest.hexdigest()


def entry_path(cache_dir, key):
    return Path(cache_dir) / f'{key}.json'


def lookup(cache_dir, key, tree, gate):
    """The recorded head when a valid entry for key may be reused, else None. Anything unreadable is a miss."""
    try:
        entry = json.loads(entry_path(cache_dir, key).read_text(encoding='utf-8'))
        if entry['schema_version'] != CACHE_SCHEMA or entry['key'] != key:
            return None
        head, recorded = entry['head'], entry['tree']
        if not isinstance(head, str) or not head:
            return None
        if gate and (not tree or recorded != tree):
            return None
        return head
    except (OSError, ValueError, KeyError, TypeError):
        return None


def store(cache_dir, key, step, head, tree):
    path = entry_path(cache_dir, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    scratch.write_text(json.dumps({'schema_version': CACHE_SCHEMA, 'key': key, 'step': step, 'head': head, 'tree': tree}),
                       encoding='utf-8')
    os.replace(scratch, path)


def step_key(root, title, inputs):
    return inputs_hash(recipe_contents(root), title, input_files(root, inputs))


def unchanged(root, title, inputs, key):
    try:
        return step_key(root, title, inputs) == key
    except (Uncacheable, OSError):
        return False


def step(title, inputs, command, environ=None, root=None):
    environ = os.environ if environ is None else environ
    root = Path.cwd() if root is None else Path(root)
    cache_dir, head, tree = environ.get('COLD_VERIFY_CACHE_DIR'), environ.get('COLD_VERIFY_HEAD'), environ.get('COLD_VERIFY_TREE')
    key = None
    try:
        key = step_key(root, title, inputs)
    except (Uncacheable, OSError) as error:
        print(f'not cached: {error}', flush=True)
    if key:
        print(f'::inputs::{key}', flush=True)
    usable = bool(key and cache_dir and head)
    tree = tree if tree_sha(root) == tree else None
    if usable and not environ.get('COLD_VERIFY_NO_CACHE') and lookup(cache_dir, key, tree, bool(environ.get('COLD_VERIFY_GATE'))):
        print(f'::skip::{title}', flush=True)
        return 0
    print(f'::group::{title}', flush=True)
    code = subprocess.call(command, cwd=root)
    print('::endgroup::', flush=True)
    if code:
        print(f'FAILED ({code}): {title}', flush=True)
    elif usable and unchanged(root, title, inputs, key):
        store(cache_dir, key, title, head, tree if tree_sha(root) == tree else None)
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='action', required=True)
    runner = commands.add_parser('step')
    runner.add_argument('title')
    runner.add_argument('--input', action='append', default=[], dest='inputs')
    argv = sys.argv[1:]
    split = argv.index('--') if '--' in argv else len(argv)
    args, command = parser.parse_args(argv[:split]), argv[split + 1:]
    if not command:
        parser.error('a command is required after --')
    return step(args.title, args.inputs, command)


if __name__ == '__main__':
    raise SystemExit(main())
