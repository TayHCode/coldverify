from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import uuid

MARKER = '.cold-artifact.json'
RECORD = Path('.cold/verify-evidence.json')
OWNER = 'coldverify'
CHECKOUT_DAYS = 14
REMOTE_DAYS = 1
REPOSITORY = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')
GITHUB_REMOTE = re.compile(r'(?:https?://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com(?:\:22)?/)'
                           r'([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?', re.IGNORECASE)
CAUSES = {'tests': 'test_failure'}


def utc(moment):
    return moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def retention(now, days):
    return {'owner': OWNER, 'policy': 'temporary', 'expires': utc(now + timedelta(days=days))}


def write_marker(directory, producer, days, run_id=None, repo=None, now=None):
    now = now or datetime.now(timezone.utc)
    provenance = {'producer': producer, 'created_at': utc(now)}
    if run_id:
        provenance['run_id'] = run_id
    if repo:
        provenance['repo'] = repo
    marker = {'schema_version': 1, 'retention': retention(now, days), 'provenance': provenance}
    Path(directory, MARKER).write_text(json.dumps(marker, indent=2) + '\n', encoding='utf-8')
    return marker


def repository(root, environ=os.environ):
    named = environ.get('GITHUB_REPOSITORY', '')
    if REPOSITORY.fullmatch(named):
        return named
    found = subprocess.run(['git', '-C', str(root), 'remote', 'get-url', 'origin'], text=True, capture_output=True, check=False,
                           env={key: value for key, value in environ.items() if not key.startswith('GIT_')})
    match = GITHUB_REMOTE.fullmatch(found.stdout.strip()) if found.returncode == 0 else None
    return match[1] if match else None


def evidence_record(receipt, repo, root, run_id, retained, claim_source):
    checks = receipt['checks']
    steps = [{'step': 'check ' + name, 'command': shlex.join(receipt['command']),
              'status': {'PASS': 'pass', 'FAIL': 'fail'}.get(result, 'skip'), 'duration_ms': 0, 'inputs_hash': None,
              **({'failing_excerpt': reason} if result == 'FAIL' else {})}
             for name, (result, reason) in checks.items()]
    record = {'schema_version': 1, 'run_id': run_id,
              'repo': {'name': repo, 'commit': receipt['commit']},
              'model': {'provider': 'unknown', 'model': 'unknown', 'effort': 'unknown'},
              'steps': steps}
    if any(step['status'] == 'skip' for step in steps):
        record.update(outcome='incomplete', failure={'class': 'harness', 'cause': 'skipped_step', 'detail': receipt['reason']})
    elif any(step['status'] == 'fail' for step in steps):
        failed = [name for name, (result, _) in checks.items() if result == 'FAIL']
        record.update(outcome='fail', failure={'class': 'product', 'cause': CAUSES.get(failed[0], 'behavior_mismatch'),
                                               'detail': receipt['reason']})
    else:
        record['outcome'] = 'pass'
    environment = receipt['environment']
    record.update(
        time={'waiting_ms': None, 'setup_ms': None, 'running_ms': None},
        cost={'usd': None, 'input_tokens': None, 'output_tokens': None},
        environment={'host': environment['host'], 'os': environment['os'], 'isolation': environment['isolation'],
                     **({'guest_id': environment['guest_id']} if environment['guest_id'] else {}),
                     'tools': {'python': environment['python'], 'coldverify': receipt['verifier']['version']}},
        procedure={'recipe': 'coldverify', 'commands': [step['command'] for step in receipt['procedure']]},
        artifacts=[{'path': artifact['path'], 'sha256': artifact['sha256']} for artifact in receipt['artifacts']]
        + [{'path': '.cold/verify-receipt.json', 'sha256': hashlib.sha256((Path(root) / '.cold/verify-receipt.json').read_bytes()).hexdigest(),
            'description': 'coldverify receipt'}],
        retention=retained)
    if receipt['claim']:
        record['claim'] = {'statement': receipt['claim'], 'source': claim_source}
    return record


def export(root, receipt, producer, claim_source, environ=os.environ):
    root = Path(root)
    repo = repository(root, environ)
    now = datetime.now(timezone.utc)
    run_id = uuid.uuid4().hex
    commit = receipt['commit']
    known = repo and commit and re.fullmatch(r'[0-9a-f]{40}', commit)
    marker = write_marker(root / '.cold', producer, CHECKOUT_DAYS, run_id=run_id,
                          repo={'name': repo, 'commit': commit} if known else None, now=now)
    if not known:
        return None
    record = evidence_record(receipt, repo, root, run_id, marker['retention'], claim_source)
    (root / RECORD).write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    return record
