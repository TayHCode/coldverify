"""Receipt archive: one line per strict run, queryable by change type and proof strategy. docs/verify-receipt.md#archive"""
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

import verify_repro

ARCHIVE = Path('coldverify/receipts.jsonl')
ARCHIVE_SCHEMA = 1
RESULTS = ('PASS', 'FAIL', 'INCOMPLETE')
STRATEGIES = ('repro', 'missing-on-base', 'flow')
DOC_SUFFIXES = ('.md', '.rst', '.txt', '.adoc')
TEST_FOLDERS = ('test', 'tests', '__tests__', 'spec')


def archive_path(root):
    common = verify_repro.git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir').decode().strip()
    return Path(common) / ARCHIVE


def path_kind(path):
    parts = Path(path).parts
    name = parts[-1].lower()
    if parts[0] in ('.github', '.fleet'):
        return 'ci'
    if any(part in TEST_FOLDERS for part in parts[:-1]) or name.startswith('test') or '_test.' in name or '.test.' in name \
            or '.spec.' in name:
        return 'tests'
    if parts[0] == 'docs' or Path(name).suffix in DOC_SUFFIXES:
        return 'docs'
    return Path(name).suffix.lstrip('.') or 'other'


def changed_paths(root, repro):
    base, head = (repro or {}).get('base'), (repro or {}).get('head')
    if not base or not head:
        return []
    listed = verify_repro.git(root, 'diff', '--name-only', '--no-renames', '-z', f'{base}...{head}')
    return sorted(path for path in listed.decode(errors='replace').split('\0') if path)


def proofs(receipt, proved):
    found = [{'strategy': test['kind'] if test.get('kind') == 'missing-on-base' else 'repro', 'name': test['name']}
             for test in (receipt.get('repro') or {}).get('tests', []) if proved(test)]
    found += [{'strategy': 'flow', 'name': flow['name']}
              for flow in (receipt.get('flows') or {}).get('flows', []) if flow.get('kind') == 'proof']
    return found


def entry(root, receipt, proved, now=None):
    paths = changed_paths(root, receipt.get('repro'))
    return {'schema_version': ARCHIVE_SCHEMA,
            'archived_at': (now or datetime.now(timezone.utc)).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'commit': receipt['commit'], 'tree': receipt['tree'], 'pull_request': receipt['pull_request'],
            'result': receipt['result'], 'change_type': '+'.join(sorted({path_kind(path) for path in paths})) or 'unknown',
            'paths': paths, 'proofs': proofs(receipt, proved)}


def record(root, receipt, proved):
    path = archive_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = ('\n' + json.dumps(entry(root, receipt, proved), sort_keys=True) + '\n').encode()
    with path.open('ab', buffering=0) as stream:
        written = stream.write(line)
    if written != len(line):
        raise OSError(f'wrote {written} of {len(line)} bytes to {path}')
    return path


def suggest(root, repro, flows=None):
    paths = set(changed_paths(root, repro))
    ran = {('flow', flow['name']) for flow in (flows or {}).get('flows', [])} | {
        ('test', test['name']) for test in (repro or {}).get('tests', [])}
    for item in reversed(read(archive_path(root))[0] if paths else []):
        shared = sorted(paths & set(item['paths']))
        if item['result'] == 'PASS' and item['proofs'] and shared:
            return {'commit': item['commit'], 'archived_at': item['archived_at'], 'shared_paths': shared, 'proofs': item['proofs'],
                    'used': any(('flow' if proof['strategy'] == 'flow' else 'test', proof['name']) in ran for proof in item['proofs'])}
    return None


def described(suggestion, code=lambda value: f'`{value}`'):
    return (f"the last PASS for these paths, {code(suggestion['commit'])}, proved it with "
            + ', '.join(f"{proof['strategy']} {code(proof['name'])}" for proof in suggestion['proofs']))


def announce(root):
    try:
        base = verify_repro.git(root, 'merge-base', 'HEAD', 'origin/main').decode().strip()
        found = suggest(root, {'base': base, 'head': 'HEAD'})
    except (OSError, subprocess.CalledProcessError):
        return None
    if found:
        print(f'Suggestion: {described(found)}. Run them too; a suggestion never changes the verdict.', flush=True)
    return found


def text(value):
    return isinstance(value, str) and bool(value)


def valid(item):
    return (isinstance(item, dict) and type(item.get('schema_version')) is int and item['schema_version'] == ARCHIVE_SCHEMA
            and item.get('result') in RESULTS and all(text(item.get(key)) for key in ('archived_at', 'change_type', 'commit'))
            and 'tree' in item and (item['tree'] is None or text(item['tree']))
            and 'pull_request' in item and (item['pull_request'] is None or type(item['pull_request']) is int)
            and isinstance(item.get('paths'), list) and all(text(path) for path in item['paths'])
            and isinstance(item.get('proofs'), list)
            and all(isinstance(proof, dict) and proof.get('strategy') in STRATEGIES and text(proof.get('name'))
                    for proof in item['proofs']))


def read(path):
    entries, rejected = [], 0
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines() if path.is_file() else []:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            item = None
        if valid(item):
            entries.append(item)
        else:
            rejected += 1
    return entries, rejected


def table(entries):
    counts = Counter()
    for item in entries:
        strategy = '+'.join(sorted({proof['strategy'] for proof in item['proofs']})) or 'none'
        counts[(item['change_type'], strategy, item['result'])] += 1
    rows = sorted({key[:2] for key in counts})
    return ['\t'.join(('change_type', 'strategy', *RESULTS))] + [
        '\t'.join((change, strategy, *(str(counts[(change, strategy, result)]) for result in RESULTS)))
        for change, strategy in rows]


def show(root):
    try:
        path = archive_path(root)
    except subprocess.CalledProcessError:
        print(f'coldverify: {root} has no Git directory to hold a receipt archive', flush=True)
        return 2
    entries, rejected = read(path)
    print(f'Receipt archive: {path} ({len(entries)} runs' + (f', {rejected} unreadable lines skipped' if rejected else '') + ')',
          flush=True)
    print('\n'.join(table(entries)), flush=True)
    return 0
