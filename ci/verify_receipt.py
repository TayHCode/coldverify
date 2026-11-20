import ast
from collections import Counter
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys

from verify_context import SECRET_NAME, coldbox_guest, redact
import evidence_export
import verify_backend
import verify_cache
import verify_criteria
import verify_flows
import verify_history
import verify_repro
import verify_trace

REPRO_PATH = Path('.cold/verify-repro.json')
RECEIPT_JSON = Path('.cold/verify-receipt.json')
RECEIPT_MD = Path('.cold/verify-receipt.md')
SCHEMA_VERSION = 10
CLAIM = re.compile(r'^##[ \t]+Claim[ \t]*\r?\n(.*?)(?=^##[ \t]|\Z)', re.MULTILINE | re.DOTALL | re.IGNORECASE)
CODES = {'PASS': 0, 'INCOMPLETE': 2}
BUILDER = re.compile(r'^[ \t]*Builder-Model:[ \t]*(\S+)[ \t]*\r?$', re.MULTILINE | re.IGNORECASE)
NO_PULL_REQUEST = ('no pull requests found', 'no git remotes found')
RECIPES = ':(glob).fleet/verify.*'
RISK_CONFIG = '.fleet/risk.json'
SKIP = re.compile(r'[.@](?:skip|skipif|skipIf|skipUnless|skipTest|xfail|expectedFailure)\b|\bSkipTest\b|\bx(?:it|describe|test)\s*\(')
ASSERTING = ('assert', 'fail', 'raises')
RECEIPT_CHECK = 'coldverify receipt'
TRUSTED_APP = 'github-actions'
ATTACHED = re.compile(r'^Coldverify-Receipt:[ \t]*(?:https://\S+/runs/)?(\d+)[ \t\r]*$', re.MULTILINE)
CHECK_TEXT_LIMIT = 65535
CLAIM_DISPLAY_LIMIT = 8000
CRITERIA_CHECK = 'coldverify criteria'
RECEIPT_AUTHOR = 'github-actions[bot]'
REPRO_BLOCK = '<!-- coldverify repro: '
STRICT_HEADER = re.compile(r'## coldverify [A-Z]+\n')
REPRO_BLOCK_LIMIT = 20000
MAX_PROOFS = 200
MAX_PROOF_NAME = 300
INFORMATIONAL = ('Informational only: nothing may require or gate on this check until a publisher that other workflows '
                 'cannot imitate exists. The host resolves the builder from a T3 pull request link.')
FLEET_VERIFY = Path(__file__).with_name('fleet-verify.sh')
SCHEMA = Path(__file__).with_name('verify-receipt.schema.json')


class PullRequestUnknown(Exception):
    pass


class GitHubError(Exception):
    pass


def builders_from(body):
    return BUILDER.findall(body or '')


def builder_set(models):
    return sorted({model.lower() for model in models})


def pr_body(root, number):
    try:
        query = subprocess.run(['gh', 'pr', 'view', str(number), '--json', 'body'], cwd=root, text=True,
                               capture_output=True, env=verify_repro.clean_environment(), check=False)
    except FileNotFoundError:
        raise GitHubError('gh is not installed') from None
    if query.returncode:
        raise GitHubError(verify_repro.excerpt(query.stderr) or f'gh exited {query.returncode}')
    try:
        body = json.loads(query.stdout)['body']
    except (KeyError, TypeError, ValueError):
        body = None
    if not isinstance(body, str):
        raise GitHubError(f'gh pr view {number} returned no PR body')
    return body


def claim_from(body):
    match = CLAIM.search(body or '')
    return match[1].strip() if match else ''


def has_no_remote(root):
    try:
        remotes = subprocess.run(['git', '-C', str(root), 'remote'], capture_output=True, text=True,
                                 env=verify_repro.clean_environment(), check=False)
    except OSError:
        return False
    return remotes.returncode == 0 and not remotes.stdout.strip()


def pull_request(root):
    event = os.environ.get('GITHUB_EVENT_PATH')
    if event and verify_repro.pr_event_applies(root):
        pull = json.loads(Path(event).read_text(encoding='utf-8')).get('pull_request')
        return (pull['number'], pull.get('body') or '') if pull else None
    if not os.environ.get('GH_REPO') and not verify_repro.pr_event_applies(root) and has_no_remote(root):
        return None
    try:
        query = subprocess.run(['gh', 'pr', 'view', '--json', 'number,body'], cwd=root, text=True,
                               capture_output=True, env=verify_repro.clean_environment(), check=False)
    except FileNotFoundError:
        raise PullRequestUnknown('gh is not installed') from None
    if query.returncode:
        if any(message in query.stderr.lower() for message in NO_PULL_REQUEST):
            return None
        raise PullRequestUnknown(verify_repro.excerpt(query.stderr) or f'gh pr view exited {query.returncode}')
    pull = json.loads(query.stdout)
    return pull['number'], pull.get('body') or ''


def clear(root):
    for path in (REPRO_PATH, verify_trace.RESULT_PATH, verify_flows.EVIDENCE, RECEIPT_JSON, RECEIPT_MD, evidence_export.RECORD):
        (Path(root) / path).unlink(missing_ok=True)
    shutil.rmtree(Path(root) / verify_flows.ARTIFACTS, ignore_errors=True)


def read(path):
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else None


def proved(test):
    if 'kind' in test:
        return test['kind'] in ('proof', 'missing-on-base')
    return test['base_exit'] == 1 and test['head_exit'] == 0


def repro_check(code, evidence):
    evidence = evidence or {}
    if code == 0:
        proofs = sum(map(proved, evidence.get('tests', [])))
        return 'PASS', f"{proofs} repro tests failed on base and passed on head"
    if code == 2:
        if evidence.get('opt_out_reason'):
            return 'INCOMPLETE', 'repro opted out: ' + evidence['opt_out_reason']
        return 'INCOMPLETE', 'repro: ' + verify_repro.excerpt(evidence.get('error', 'nothing reproduced the claim'))
    if evidence.get('error'):
        return 'FAIL', 'repro: ' + verify_repro.excerpt(evidence['error'])
    if any(map(proved, evidence.get('tests', []))):
        return 'FAIL', 'repro has proof but a selected test regressed'
    return 'FAIL', 'repro tests did not fail on base and pass on head'


def recipe_change(root, base, head):
    try:
        fork = verify_repro.git(root, 'merge-base', base, head).decode().strip()
        changed = verify_repro.git(root, 'diff', '--name-only', '--no-renames', '-z', fork, head, '--', RECIPES)
        listing = verify_repro.git(root, 'ls-tree', '-r', '-z', head, '--', '.fleet')
        dirty = verify_repro.git(root, 'status', '--porcelain', '-z', '--untracked-files=all', '--', RECIPES)
    except subprocess.CalledProcessError as error:
        return 'could not compare the verify recipe with base: ' + verify_repro.excerpt((error.stderr or b'').decode(errors='replace'))
    if dirty.strip(b'\0'):
        return 'recipe has uncommitted changes; needs review: ' + ', '.join(
            entry[3:] for entry in dirty.decode(errors='replace').split('\0') if entry[3:])
    entries = [entry.split('\t', 1) for entry in listing.decode(errors='replace').split('\0') if entry]
    links = [path for mode, path in entries if mode.startswith('120000 ') and (path == '.fleet' or path.startswith('.fleet/verify.'))]
    if links:
        return 'recipe is a symlink; needs review: ' + ', '.join(links)
    changed = [path for path in changed.decode().split('\0') if path]
    recipes = [path for _, path in entries if path.startswith('.fleet/verify.')]
    return 'recipe changed; needs review: ' + ', '.join(changed) if changed and recipes else None


def assertions(function):
    found = Counter()

    def visit(node, context):
        name = getattr(getattr(node, 'func', None), 'attr', None) or getattr(getattr(node, 'func', None), 'id', '')
        if isinstance(node, ast.Assert) or isinstance(node, ast.Call) and name.startswith(ASSERTING):
            found[(context, ast.dump(node, include_attributes=False))] += 1
        header = type(node).__name__ + ''.join(ast.dump(value, include_attributes=False)
                                               for _, value in ast.iter_fields(node) if isinstance(value, ast.AST))
        for field, value in ast.iter_fields(node):
            for child in value if isinstance(value, list) else [value]:
                if isinstance(child, ast.AST):
                    visit(child, context + ((header, field),))

    for statement in function.body:
        visit(statement, ())
    return found


def test_cases(source):
    cases = verify_repro.test_functions(source)
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef) and node.name.startswith('Test'):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith('test'):
                    cases.setdefault(f'{node.name}.{item.name}', item)
    return cases


def marked_lines(text):
    return Counter(line.strip() for line in text.splitlines() if SKIP.search(line))


def exits(function):
    return sum(isinstance(node, (ast.Return, ast.Break, ast.Continue)) for node in ast.walk(function))


def skips(node):
    return len(SKIP.findall(ast.unparse(node)))


def weakened_file(path, before, after):
    skip = f'skip or xfail marker added in {path}'
    reasons = [skip] if marked_lines(after) - marked_lines(before) else []
    if not path.endswith('.py') or not before:
        return reasons
    try:
        old, new = test_cases(before), test_cases(after)
    except (SyntaxError, ValueError) as error:
        return reasons + [f'could not read the test cases in {path}: {error}']
    if not reasons and any(skips(new[name]) > skips(old[name]) for name in old.keys() & new.keys()):
        reasons.append(skip)
    deleted = sorted(old.keys() - new.keys())
    if deleted:
        reasons.append(f'test cases deleted in {path}: ' + ', '.join(deleted))
    loosened = sorted(name for name in old.keys() & new.keys() if assertions(old[name]) - assertions(new[name]) or exits(new[name]) > exits(old[name]))
    if loosened:
        reasons.append(f'assertions of existing tests changed in {path}: ' + ', '.join(loosened))
    return reasons


def is_test(path):
    return bool(verify_repro.TEST_PATH.search(path)) or Path(path).name == 'conftest.py'


def weakened_tests(root, base, head):
    try:
        fork = verify_repro.git(root, 'merge-base', base, head).decode().strip()
        listing = verify_repro.git(root, 'diff', '--name-status', '--find-renames', '-z', fork, head).decode().split('\0')
        reasons = []
        index = 0
        while index < len(listing) and listing[index]:
            status, old = listing[index], listing[index + 1]
            new = listing[index + 2] if status[0] in 'RC' else old
            index += 3 if status[0] in 'RC' else 2
            if not is_test(old) and not is_test(new):
                continue
            if status == 'D' or not is_test(new):
                reasons.append(f'test file deleted: {old}')
                continue
            before = '' if status == 'A' else verify_repro.git(root, 'show', f'{fork}:{old}').decode(errors='replace')
            reasons += weakened_file(new, before, verify_repro.git(root, 'show', f'{head}:{new}').decode(errors='replace'))
        return reasons
    except subprocess.CalledProcessError as error:
        return ['could not compare the tests with base: ' + verify_repro.excerpt((error.stderr or b'').decode(errors='replace'))]


def claim_change(root, claim, pull):
    if not pull:
        return None
    try:
        body = pr_body(root, pull[0])
    except GitHubError as error:
        return f'could not re-read the claim of PR #{pull[0]}: {error}'
    if claim_from(body) != claim:
        return f'the claim differs from the "## Claim" section of PR #{pull[0]}; rerun coldverify on the current claim'
    if builders_from(body) != builders_from(pull[1]):
        return f'the Builder-Model lines of PR #{pull[0]} changed during the run; rerun coldverify on the current description'
    return None


def tamper_check(root, repro, claim, pull):
    base, head = (repro or {}).get('base'), (repro or {}).get('head')
    if base and head:
        reasons = [recipe_change(root, base, head), flow_change(root, base, head), *weakened_tests(root, base, head)]
    else:
        reasons = ['could not compare the change with base: the repro gate recorded no base and head']
    reasons = [reason for reason in [*reasons, claim_change(root, claim, pull)] if reason]
    return ('INCOMPLETE', '; '.join(reasons)) if reasons else ('PASS', 'no tests weakened, recipe and claim unchanged')


def risk_patterns(text):
    config = json.loads(text)
    if not (isinstance(config, dict) and set(config) == {'high'} and isinstance(config['high'], list)):
        raise ValueError('it must be an object with only a "high" list of path globs')
    bad = [pattern for pattern in config['high'] if not (isinstance(pattern, str) and pattern.strip() and not pattern.startswith(':'))]
    if bad:
        raise ValueError('every "high" entry must be a non-empty path glob not starting with ":", not ' + json.dumps(bad[0]))
    return config['high']


def risk_tier(root, repro):
    base, head = (repro or {}).get('base'), (repro or {}).get('head')
    if not (base and head):
        return None, 'could not tell the risk tier: the repro gate recorded no base and head'
    try:
        fork = verify_repro.git(root, 'merge-base', base, head).decode().strip()
        configs = [commit for commit in dict.fromkeys((fork, base))
                   if verify_repro.git(root, 'ls-tree', commit, '--', RISK_CONFIG).strip()]
        patterns = sorted({pattern for commit in configs
                           for pattern in risk_patterns(verify_repro.git(root, 'show', f'{commit}:{RISK_CONFIG}'))})
        changed = verify_repro.git(root, 'diff', '--name-only', '--no-renames', '-z', fork, head, '--',
                                   RISK_CONFIG, *[':(glob)' + pattern for pattern in patterns])
    except subprocess.CalledProcessError as error:
        return None, 'could not tell the risk tier: ' + verify_repro.excerpt((error.stderr or b'').decode(errors='replace'))
    except ValueError as error:
        return None, f'could not tell the risk tier: {RISK_CONFIG} at base is invalid: {verify_repro.excerpt(error)}'
    matched = sorted(path for path in changed.decode(errors='replace').split('\0') if path)
    return {'tier': 'high' if matched else 'normal', 'config': RISK_CONFIG if configs else None,
            'matched': matched, 'signoff': None}, None


def risk_check(root, claim, repro, builders, checks, declared=None):
    risk, error = risk_tier(root, repro)
    if error:
        return ('INCOMPLETE', error), None
    if risk['tier'] == 'normal':
        return None, risk
    if combine(checks)[0] != 'PASS':
        return ('INCOMPLETE', f"high risk ({', '.join(risk['matched'])}): no sign-off sought until every other check passes"), risk
    check, risk['signoff'] = verify_criteria.sign_off(root, claim, repro, builders, risk['matched'], declared)
    return check, risk


def flow_change(root, base, head):
    try:
        fork = verify_repro.git(root, 'merge-base', base, head).decode().strip()
        listing = verify_repro.git(root, 'diff', '--name-status', '--no-renames', '-z', fork, head, '--', verify_flows.FLOWS)
    except subprocess.CalledProcessError as error:
        return 'could not compare the flows with base: ' + verify_repro.excerpt((error.stderr or b'').decode(errors='replace'))
    entries = listing.decode(errors='replace').split('\0')
    changed = [path for status, path in zip(*[iter(entries)] * 2) if status != 'A']
    return 'flow changed; needs review: ' + ', '.join(changed) if changed else None


GUEST_CRITERIA = ('criteria not evaluated in a CI guest, so this verdict does not cover them; '
                  'see the "coldverify criteria" check on the PR head')
GUEST_BUILDER = ('the builder identity was not checked: neither --builder-model nor the PR\'s "Builder-Model:" lines were '
                 'compared with a record the author cannot write')


def combine(checks):
    if tuple(checks.get('criteria') or ()) == ('INCOMPLETE', verify_criteria.GUEST) and coldbox_guest():
        result, reason = combine({name: check for name, check in checks.items() if name != 'criteria'})
        return result, f'{reason}; {GUEST_CRITERIA}; {GUEST_BUILDER}'
    for wanted in ('FAIL', 'INCOMPLETE'):
        reasons = [reason for result, reason in checks.values() if result == wanted]
        if reasons:
            return wanted, '; '.join(reasons)
    return 'PASS', '; '.join(reason for _, reason in checks.values())


def revision(root, name):
    found = subprocess.run(['git', '-C', str(root), 'rev-parse', name], text=True, capture_output=True,
                           env=verify_repro.clean_environment(), check=False)
    return found.stdout.strip() if found.returncode == 0 else None


def environment(repro, run=None):
    guest = coldbox_guest()
    host = guest or platform.node()
    ran_on = (repro or {}).get('host', host)
    try:
        configured = verify_backend.configured()
    except verify_backend.Unavailable:
        configured = None
    backend = (run or {}).get('backend') or (repro or {}).get('backend') or configured or ('local' if ran_on.casefold() == host.casefold() else 'ssh:' + ran_on)
    return {'host': host, 'os': platform.platform(), 'python': platform.python_version(), 'backend': backend,
            'isolation': 'coldbox-guest' if guest and backend == 'local' else verify_backend.isolation(backend),
            'guest_id': platform.node() if guest else None}


def fingerprint(root, repro, verifier, env=None):
    found = environment(repro)
    try:
        suite = verify_backend.configured()
    except verify_backend.Unavailable:
        suite = None
    variables = {name: None if SECRET_NAME.search(name) else value
                 for name, value in (os.environ if env is None else env).items() if name not in ('_', 'OLDPWD')}
    setting = {**{key: found[key] for key in ('host', 'os', 'python', 'backend', 'isolation')}, 'verifier': verifier,
               'suite_backend': suite, 'variables': variables}
    known = verifier['commit'] and not verifier['commit'].endswith('-dirty')
    recipes = {**verify_cache.recipe_contents(root), 'coldverify:ci/fleet-verify.sh': FLEET_VERIFY.read_bytes()}
    return {'tree': verify_cache.tree_sha(root), 'recipe': verify_cache.inputs_hash(recipes, 'recipe'),
            'environment': hashlib.sha256(json.dumps(setting, sort_keys=True).encode()).hexdigest() if known else None}


def valid_result(run):
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        return False
    definitions = json.loads(SCHEMA.read_text(encoding='utf-8'))['$defs']
    return Draft202012Validator({'$defs': definitions, '$ref': '#/$defs/result'}).is_valid(run)


def reuse(root, wanted, pull):
    root = Path(root)
    risk, error = risk_tier(root, read(root / REPRO_PATH))
    if error or risk['tier'] != 'normal' or not all(wanted.values()):
        return None
    try:
        runs = published_runs(root, head_commit(root, revision(root, 'HEAD')), pull[1] if pull else '')
    except (GitHubError, KeyError, TypeError):
        return None
    for run in runs:
        try:
            prior = None if distrust(run, wanted['tree']) else json.loads(run['output']['text'])
        except (KeyError, TypeError, ValueError):
            continue
        tests = prior.get('run') if isinstance(prior, dict) else None
        if (isinstance(tests, dict) and prior.get('schema_version') == SCHEMA_VERSION and prior.get('fingerprint') == wanted
                and tests.get('result') == 'PASS' and valid_result(tests)):
            url = run.get('html_url')
            reused = {**tests, 'reason': f"reused from {url}: {tests.get('reason')}", 'reused_from': url}
            (root / verify_trace.RESULT_PATH).write_text(json.dumps(reused, indent=2) + '\n', encoding='utf-8')
            print(f"TESTS PASS: {reused['reason']}", flush=True)
            return url
    return None


def procedure(command, repro, run, flows=None, sides=('base', 'head')):
    steps = [{'step': 'coldverify', 'command': shlex.join(command)}]
    for test in (repro or {}).get('tests', []):
        steps += [{'step': f"repro {side} {test['name']}", 'command': command}
                  for side, command in zip(('base', 'head'), test.get('commands', []))]
    steps += [{'step': step['step'], 'command': step['command']} for step in (run or {}).get('steps', [])]
    for flow in (flows or {}).get('flows', []):
        steps += [{'step': f"flow {side} {flow['name']}", 'command': command} for side, command in zip(sides, flow['commands'])]
    return steps


def artifacts(root):
    paths = [REPRO_PATH, verify_trace.RESULT_PATH, verify_flows.EVIDENCE]
    paths += sorted(path.relative_to(root) for path in (root / verify_flows.ARTIFACTS).rglob('*') if path.is_file())
    return [{'path': path.as_posix(), 'sha256': hashlib.sha256((root / path).read_bytes()).hexdigest()}
            for path in paths if (root / path).is_file()]


def summarize(receipt):
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if not summary:
        return
    text = markdown(receipt)
    if private_repository():
        text += '\n<details><summary>Evidence package (verify-receipt.json)</summary>\n\n```json\n' + \
                json.dumps(receipt, indent=2) + '\n```\n\n</details>\n'
    with open(summary, 'a', encoding='utf-8') as stream:
        stream.write(text)


def private_repository():
    try:
        event = json.loads(Path(os.environ.get('GITHUB_EVENT_PATH', '')).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    return isinstance(event, dict) and (event.get('repository') or {}).get('private') is True


def markdown_field(value, *, code=False, limit=None):
    text = ' '.join(redact(str(value)).split())
    if limit is not None and len(text) > limit:
        half = limit // 2
        text = text[:half] + ' … ' + text[-half:]
    if code and text and not any(char in text for char in '`|'):
        return f'`{text}`'
    text = re.sub(r'[\\`*_\[\]<>#&|~]', lambda match: '\\' + match[0], text)
    return re.sub(r'^(\d*)([-+=.)])', r'\1\\\2', text)


def markdown_code(value):
    return markdown_field(value, code=True)


def risk_line(risk):
    if not risk:
        return 'Risk: unknown'
    line = f"Risk: {risk['tier']}" + (f" ({', '.join(risk['matched'])})" if risk['matched'] else '')
    signoff = risk['signoff']
    if signoff:
        line += f"; {signoff['model']} {'signed off' if signoff['approved'] else 'did not sign off'}: {signoff['reason']}"
    return markdown_field(line)


def suggestion_line(suggestion):
    if not suggestion:
        return 'Suggestion: none (no earlier PASS in the receipt archive changed these paths)'
    return (f"Suggestion: {verify_history.described(suggestion, markdown_code)}; {'used' if suggestion['used'] else 'not used'} here. "
            'A suggestion never changes the verdict.')


def check_rows(checks):
    return [f'| {markdown_field(name)} | {markdown_field(result)} | {markdown_field(reason)} |'
            for name, (result, reason) in checks.items()]


def criterion_rows(criteria):
    return [f"| {markdown_field(criterion['id'])}: {markdown_field(criterion['text'])} | "
            f"{', '.join(map(markdown_code, criterion['covered_by'])) or '**no proof test**'} |" for criterion in criteria]


def deployed_markdown(receipt):
    contact = receipt['flows'].get('contact')
    reached = (f"Target contact: {contact['established_by']} ({contact['probe']}); "
               f"flow contact with the target is {contact['flow_contact']}" if contact else
               'Target contact: not established, so no flow ran against the target')
    lines = [f"## coldverify {receipt['result']} against {markdown_code(receipt['deployed'])}", '',
             markdown_field(receipt['reason']), '', markdown_field(reached), '',
             f"Commit: {markdown_code(receipt['commit'])}" + (' (its flows ran against the deployed target)' if receipt['flows']['flows'] else ''), '',
             markdown_field(f"Verifier: coldverify {receipt['verifier']['version']} ({receipt['verifier']['commit'] or 'unknown commit'}) "
                            f"on {receipt['environment']['host']}"), '', '### Claim', '',
             markdown_field(receipt['claim'] or '', limit=CLAIM_DISPLAY_LIMIT) or '_none stated_', '',
             '### Checks', '', '| check | result | why |', '| --- | --- | --- |']
    lines += check_rows(receipt['checks'])
    lines += ['', '### Flows', '', '| flow | exit | kind |', '| --- | ---: | --- |']
    lines += [f"| {markdown_code(flow['name'])} | {flow['head_exit']} | {flow['kind']} |" for flow in receipt['flows']['flows']]
    return '\n'.join(lines) + '\n'


def markdown(receipt):
    if receipt.get('deployed'):
        return deployed_markdown(receipt)
    lines = [f"## coldverify {receipt['result']}", '', markdown_field(receipt['reason']), '',
             f"Commit: {markdown_code(receipt['commit'])}", '', f"Tree: {markdown_code(receipt['tree'])}", '',
             markdown_field(f"Verifier: coldverify {receipt['verifier']['version']} ({receipt['verifier']['commit'] or 'unknown commit'}) "
                            f"on {receipt['environment']['host']} ({receipt['environment']['isolation']})"), '', risk_line(receipt['risk']), '',
             suggestion_line(receipt.get('suggestion')), '', '### Claim', '',
             markdown_field(receipt['claim'] or '', limit=CLAIM_DISPLAY_LIMIT) or '_none stated_', '',
             '### Acceptance criteria', '']
    if receipt['acceptance_criteria']:
        lines += ['| criterion | covered by |', '| --- | --- |']
        lines += criterion_rows(receipt['acceptance_criteria'])
    else:
        lines.append('None derived.')
    lines += ['', '### Checks', '', '| check | result | why |', '| --- | --- | --- |']
    lines += check_rows(receipt['checks'])
    repro = receipt['repro'] or {}
    lines += ['', '### Repro (at least one proof required)', '']
    if repro.get('tests'):
        lines += [f"Base {markdown_code(repro.get('base'))}, head {markdown_code(repro.get('head'))} on {markdown_field(repro.get('host'))}", '',
                  '| test | base exit | head exit | kind |', '| --- | ---: | ---: | --- |']
        lines += [f"| {markdown_code(test['name'])} | {test['base_exit']} | {test['head_exit']} | {markdown_field(test.get('kind', 'unclassified'))} |"
                  for test in repro['tests']]
    else:
        lines.append('No repro test ran.')
    run = receipt['run']
    lines += ['', '### Test run', '']
    if run:
        source = {'junit': 'from JUnit XML', 'output': 'unbound: counted from output text, not JUnit XML'}.get(run.get('tests_source'))
        lines += [f"Command: {markdown_code(' '.join(run['command']))}, exit {run['exit_code']}, tests ran: {run['tests_ran']}"
                  + (f' ({source})' if source else ''), '',
                  '| step | status | seconds | command |', '| --- | --- | ---: | --- |']
        lines += [f"| {markdown_field(step['step'])} | {step['status']} | {step['duration_ms'] / 1000:.1f} | {markdown_code(step['command'])} |"
                  for step in run['steps']]
    else:
        lines.append('The tests did not run.')
    flows = receipt.get('flows')
    if flows:
        lines += ['', '### Flows', '', f"Base {markdown_code(flows['base'])}, head {markdown_code(flows['head'])} on {markdown_field(flows['host'])}", '',
                  '| flow | base exit | head exit | kind |', '| --- | ---: | ---: | --- |']
        lines += [f"| {markdown_code(flow['name'])} | {flow['base_exit']} | {flow['head_exit']} | {flow['kind']} |" for flow in flows['flows']]
    return '\n'.join(lines) + '\n' + repro_block(receipt)


def claim_digest(claim):
    return hashlib.sha256(claim.encode()).hexdigest()


def repro_block(receipt):
    repro = receipt['repro'] or {}
    claim = receipt['claim'] or ''
    data = {'claim': claim_digest(claim), 'criteria_claim': claim_digest(criteria_claim(claim)),
            'base': repro.get('base'), 'head': repro.get('head'),
            'tests': [{key: test[key] for key in ('name', 'base_exit', 'head_exit', 'kind') if key in test}
                      for test in repro.get('tests') or []]}
    block = json.dumps(data, separators=(',', ':')).replace('>', '\\u003e')
    return f'{REPRO_BLOCK}{block} -->\n' if len(block) <= REPRO_BLOCK_LIMIT else ''


def settle(receipt, name=None, check=None):
    checks = {**receipt['checks'], name: check} if name else receipt['checks']
    result, reason = combine(checks)
    return {**receipt, 'checks': checks, 'result': result, 'reason': reason}


def write(root, receipt, saved=None):
    (root / RECEIPT_JSON).parent.mkdir(parents=True, exist_ok=True)
    (root / RECEIPT_JSON).parent.joinpath('.gitignore').write_text('*\n')
    (root / RECEIPT_MD).write_text(markdown(receipt), encoding='utf-8')
    partial = (root / RECEIPT_JSON).with_suffix('.json.partial')
    partial.write_text(json.dumps(saved or receipt, indent=2) + '\n', encoding='utf-8')
    os.replace(partial, root / RECEIPT_JSON)


def post(root, number):
    try:
        posted = subprocess.run(['gh', 'pr', 'comment', str(number), '--body-file', str(root / RECEIPT_MD)], cwd=root,
                                text=True, capture_output=True, env=verify_repro.clean_environment(), check=False)
    except FileNotFoundError:
        return False
    return posted.returncode == 0


def github(root, path, *options, payload=None, lines=False):
    repository = os.environ.get('GITHUB_REPOSITORY') or '{owner}/{repo}'
    try:
        found = subprocess.run(['gh', 'api', *options, f'repos/{repository}/{path}'], cwd=root, input=payload, text=True,
                               capture_output=True, env=verify_repro.clean_environment(), check=False)
    except FileNotFoundError:
        raise GitHubError('gh is not installed') from None
    if found.returncode:
        raise GitHubError(verify_repro.excerpt(found.stderr) or f'gh api exited {found.returncode}')
    try:
        return [json.loads(line) for line in found.stdout.splitlines() if line.strip()] if lines else json.loads(found.stdout)
    except ValueError:
        raise GitHubError('gh api returned no JSON') from None


def head_commit(root, checkout):
    event = os.environ.get('GITHUB_EVENT_PATH')
    if event and verify_repro.pr_event_applies(root):
        return json.loads(Path(event).read_text(encoding='utf-8'))['pull_request']['head']['sha']
    return checkout


def publish(root, receipt):
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        return None, 'only a GitHub Actions job can publish a trusted receipt'
    if not receipt['tree'] or not receipt['commit']:
        return None, 'the checkout has no commit or tree to bind the receipt to'
    text = json.dumps(receipt, indent=2)
    if len(text) > CHECK_TEXT_LIMIT:
        text = f'The receipt JSON is {len(text)} characters, over the check run limit; the job summary keeps it.'
    conclusion = 'success' if receipt['result'] == 'PASS' else 'failure'
    run = {'name': RECEIPT_CHECK, 'head_sha': head_commit(root, receipt['commit']), 'status': 'in_progress',
           'external_id': receipt['tree'], 'output': {'title': f"{receipt['result']} for tree {receipt['tree']}",
                                                      'summary': markdown(receipt)[:CHECK_TEXT_LIMIT], 'text': text}}
    try:
        created = github(root, 'check-runs', '--method', 'POST', '--input', '-', payload=json.dumps(run))
        number, url = created['id'], created['html_url']
    except (GitHubError, KeyError, TypeError) as error:
        return None, str(error) if isinstance(error, GitHubError) else 'GitHub returned no check run id and URL'
    try:
        complete(root, number, conclusion)
    except GitHubError as error:
        if conclusion == 'success':
            try:
                complete(root, number, 'failure')
            except GitHubError:
                pass
        return None, f'could not complete check run {url}: {error}'
    return url, None


def complete(root, number, conclusion):
    github(root, f'check-runs/{number}', '--method', 'PATCH', '--input', '-',
           payload=json.dumps({'status': 'completed', 'conclusion': conclusion}))


def distrust(run, tree):
    app = (run.get('app') or {}).get('slug')
    if app != TRUSTED_APP:
        return f'published by {app}, not GitHub Actions'
    if run.get('name') != RECEIPT_CHECK:
        return f"named {run.get('name')}, not {RECEIPT_CHECK}"
    if (run.get('status'), run.get('conclusion')) != ('completed', 'success'):
        return f"conclusion {run.get('conclusion') or run.get('status')}, not success"
    if run.get('external_id') != tree:
        return f"bound to tree {run.get('external_id')}, not {tree}"
    return None


def published_runs(root, head, body):
    runs = github(root, f'commits/{head}/check-runs', '--method', 'GET', '--paginate',
                  '--jq', '.check_runs[]', '-f', f'check_name={RECEIPT_CHECK}', '-f', 'per_page=100', lines=True)
    return runs + [github(root, f'check-runs/{number}') for number in dict.fromkeys(ATTACHED.findall(body))]


def live_pull(root, number=None):
    selector = [str(number)] if number is not None else []
    env = verify_repro.clean_environment()
    if repository := env.get('GITHUB_REPOSITORY'):
        env['GH_REPO'] = repository
    try:
        query = subprocess.run(['gh', 'pr', 'view', *selector, '--json', 'number,body,baseRefOid,headRefOid'],
                               cwd=root, text=True, capture_output=True, env=env, check=False)
    except FileNotFoundError:
        raise GitHubError('gh is not installed') from None
    if query.returncode:
        if number is None and any(message in query.stderr.lower() for message in NO_PULL_REQUEST):
            return None
        raise GitHubError(verify_repro.excerpt(query.stderr) or f'gh exited {query.returncode}')
    try:
        pull = json.loads(query.stdout)
        if (type(pull['number']) is not int or pull['number'] <= 0 or (number is not None and pull['number'] != number)
                or not isinstance(pull['body'], str)
                or not all(isinstance(pull[key], str) and re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', pull[key])
                           for key in ('baseRefOid', 'headRefOid'))):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise GitHubError('gh pr view returned no PR body or commit identity') from None
    return pull


def receipt_mismatch(root, receipt, check, checkout, tree, pull, claim):
    if pull:
        head, base = pull['headRefOid'], pull['baseRefOid']
        claim = claim_from(pull['body'])
        builders, recorded = builder_set(builders_from(pull['body'])), receipt['builders']
        if builders != recorded:
            return (f'issued for builders {", ".join(recorded) or "none"}, but the "Builder-Model:" lines of PR '
                    f'#{pull["number"]} now name {", ".join(builders) or "none"}; rerun coldverify on the current description')
        if checkout != head and (not verify_repro.pr_event_applies(root)
                                 or verify_repro.commit_parents(root, checkout) != [base, head]):
            return 'checkout is neither the current PR head nor its merge with the current base'
    else:
        head, base = checkout, verify_repro.default_base(root)
        claim = (claim or '').strip()
    if not claim:
        raise GitHubError('no current claim: add a "## Claim" section to the PR description or pass --claim before opening a PR')
    if check.get('head_sha') != head:
        return 'check run does not name the current PR head'
    issued = receipt.get('pull_request')
    if issued is not None and (type(issued) is not int or pull is None or issued != pull['number']):
        return 'receipt names a different PR'
    if (receipt.get('result'), receipt.get('commit'), receipt.get('tree')) != ('PASS', checkout, tree):
        return 'receipt verdict, checkout commit or tree differs'
    repro, suite = receipt.get('repro'), receipt.get('run')
    if not isinstance(repro, dict) or (repro.get('base'), repro.get('head')) != (base, head):
        return 'repro base or head differs from the current inputs'
    if not isinstance(suite, dict) or (suite.get('result'), suite.get('head'), suite.get('tree')) != ('PASS', checkout, tree):
        return 'suite commit or tree differs from the current checkout'
    recorded_claim = receipt.get('claim')
    if not isinstance(recorded_claim, str) or claim_digest(recorded_claim) != claim_digest(claim):
        return 'receipt claim differs from the current claim'
    return None


def require(root, claim=None):
    root = Path(root)
    tree, checkout = revision(root, 'HEAD^{tree}'), revision(root, 'HEAD')
    if not tree or not checkout:
        return 'INCOMPLETE', 'could not read the commit and tree of HEAD'
    try:
        event = pull_request(root) if verify_repro.pr_event_applies(root) and os.environ.get('GITHUB_EVENT_PATH') else None
        pull = live_pull(root, event[0] if event else None)
    except (GitHubError, PullRequestUnknown, OSError, ValueError, KeyError, TypeError) as error:
        return 'INCOMPLETE', f'could not read the current PR inputs: {error}'
    try:
        runs = published_runs(root, pull['headRefOid'] if pull else checkout, pull['body'] if pull else '')
    except (GitHubError, KeyError, TypeError) as error:
        return 'INCOMPLETE', 'could not read the receipts: ' + (str(error) if isinstance(error, GitHubError) else 'unexpected reply')
    rejected, unread, issued_pulls = [], [], {}
    for check in runs:
        url = check.get('html_url')
        problem = distrust(check, tree)
        if problem:
            rejected.append(f'{url}: {problem}')
            continue
        try:
            receipt = json.loads(check['output']['text'])
        except (KeyError, TypeError, ValueError):
            receipt = None
        builders = receipt.get('builders') if isinstance(receipt, dict) else None
        if not isinstance(builders, list) or not all(isinstance(model, str) for model in builders):
            rejected.append(f'{url}: records no builders it was issued for (a receipt from before #123, '
                            'or one too large to keep its JSON); rerun coldverify')
            continue
        try:
            current = pull
            issued = receipt.get('pull_request')
            if current is None and type(issued) is int:
                if issued not in issued_pulls:
                    try:
                        issued_pulls[issued] = live_pull(root, issued)
                    except GitHubError as error:
                        issued_pulls[issued] = error
                current = issued_pulls[issued]
                if isinstance(current, GitHubError):
                    raise current
            problem = receipt_mismatch(root, receipt, check, checkout, tree, current, claim)
        except (GitHubError, verify_repro.NoRepro, subprocess.CalledProcessError) as error:
            unread.append(f'could not read the current PR inputs for {url}: {error}')
            continue
        if not problem:
            return 'PASS', f'{url} is a PASS receipt from GitHub Actions for tree {tree}'
        rejected.append(f'{url}: {problem}')
    if unread:
        return 'INCOMPLETE', '; '.join(unread)
    return 'FAIL', f'no PASS receipt from GitHub Actions for tree {tree}' + ('; rejected ' + '; '.join(rejected) if rejected else '')


def finish(root, claim, pull, repro_code, command, verifier, pull_error=None, builders=(), publish_check=False,
           claim_source='coldverify --claim', hashes=None, deployed=None, declared=None, suite_revision=None, flows=None,
           repro=None, criteria_adapter=None):
    root = Path(root)
    if repro is None:
        repro = read(root / REPRO_PATH)
    checks = {'claim': ('PASS', 'claim stated') if claim else
              ('INCOMPLETE', 'no claim: add a "## Claim" section to the PR description or pass --claim')}
    if not deployed:
        checks['repro'] = repro_check(repro_code, repro)
    if pull_error:
        checks['receipt'] = ('INCOMPLETE', f'could not tell whether this branch has a PR, so no receipt was posted: {pull_error}')
    run = None if deployed else read(root / verify_trace.RESULT_PATH)
    if suite_revision:
        commit = revision(root, 'HEAD')
        tree = revision(root, f'{commit}^{{tree}}') if commit else None
        if run and run.get('result') == 'PASS' and (commit != suite_revision or not tree
                or (run.get('head'), run.get('tree')) != (commit, tree)):
            run.update(result='INCOMPLETE', reason=f'suite evidence does not match the requested revision {suite_revision}')
    else:
        commit = tree = None
    if run and run.get('schema_version') != verify_trace.RESULT_SCHEMA_VERSION:
        checks['tests'] = ('INCOMPLETE', f"{verify_trace.RESULT_PATH} has schema_version {run.get('schema_version')!r}; "
                                         f'this coldverify reads only {verify_trace.RESULT_SCHEMA_VERSION}')
        run = None
    elif not deployed:
        checks['tests'] = (run['result'], run['reason']) if run else ('INCOMPLETE', 'the tests did not run')
    if flows:
        checks['flows'] = (flows['result'], flows['reason'])
    criteria, criteria_verifier, risk, suggestion = [], None, None, None
    if not deployed:
        checks['tamper'] = tamper_check(root, repro, claim, pull)
        if pull_error:
            checks['criteria'] = ('INCOMPLETE', 'no criteria verifier chosen: the PR\'s "Builder-Model:" lines could not be read '
                                                f'to compare with --builder-model: {pull_error}')
        else:
            observations = verify_flows.proof_observations(flows, repro)
            checks['criteria'], criteria, criteria_verifier = verify_criteria.check(root, claim, repro, list(builders), proved,
                                                                                    declared, flows=observations,
                                                                                    adapter=criteria_adapter)
            if (observations and checks['criteria'][0] == 'PASS' and repro_code == 2
                    and repro.get('error') == verify_repro.NO_TEST_CASES
                    and any(name in observations for criterion in criteria for name in criterion['covered_by'])):
                checks['repro'] = ('PASS', 'observed flows reproduced the claim on base and passed on head')
        risk_result, risk = risk_check(root, claim, repro, list(builders), checks, declared)
        if risk_result:
            checks['risk'] = risk_result
        try:
            suggestion = verify_history.suggest(root, repro, flows)
        except (OSError, subprocess.CalledProcessError) as error:
            print(f'coldverify: the receipt archive was not searched for a suggestion: {verify_repro.excerpt(error)}',
                  file=sys.stderr, flush=True)
    receipt = settle({'schema_version': SCHEMA_VERSION, 'claim': claim,
                      'commit': commit if suite_revision else revision(root, 'HEAD'),
                      'tree': tree if suite_revision else revision(root, 'HEAD^{tree}'),
                      'fingerprint': hashes or fingerprint(root, {'backend': 'local'} if deployed else repro, verifier),
                      'pull_request': pull[0] if pull else None,
                      'builders': builder_set(builders if declared is None else declared),
                      'command': command, 'verifier': verifier, 'environment': environment({'backend': 'local'} if deployed else repro, run), 'deployed': deployed,
                      'procedure': procedure(command, repro, run, flows, ('deployed',) if deployed else ('base', 'head')),
                      'artifacts': artifacts(root),
                      'acceptance_criteria': criteria, 'criteria_verifier': criteria_verifier, 'risk': risk, 'suggestion': suggestion,
                      'checks': checks, 'repro': repro, 'run': run, 'flows': flows})
    if pull:
        write(root, receipt, settle(receipt, 'receipt', ('INCOMPLETE', f'posting the receipt to PR #{pull[0]} was not confirmed')))
        if not post(root, pull[0]):
            receipt = settle(receipt, 'receipt', ('INCOMPLETE', f'the receipt could not be posted to PR #{pull[0]}'))
    if publish_check:
        write(root, receipt, settle(receipt, 'receipt', ('INCOMPLETE', 'publishing the receipt as a check run was not confirmed')))
        url, error = publish(root, receipt)
        if error:
            earlier = receipt['checks'].get('receipt')
            reason = 'the receipt was not published as a check run: ' + error
            receipt = settle(receipt, 'receipt', ('INCOMPLETE', f'{earlier[1]}; {reason}' if earlier else reason))
        else:
            print(f'Coldverify-Receipt: {url}', flush=True)
    write(root, receipt)
    try:
        if not deployed:
            verify_history.record(root, receipt, proved)
    except (OSError, subprocess.CalledProcessError) as error:
        print(f'coldverify: the receipt was not archived: {verify_repro.excerpt(error)}', file=sys.stderr, flush=True)
    summarize(receipt)
    try:
        evidence_export.export(root, receipt, 'coldverify ' + verifier['version'], claim_source)
    except OSError as error:
        print(f'coldverify: the shared evidence record was not written: {verify_repro.excerpt(error)}', file=sys.stderr, flush=True)
    print(markdown(receipt), flush=True)
    print(f"COLDVERIFY {receipt['result']}: {receipt['reason']}", flush=True)
    return CODES.get(receipt['result'], 1)


BUG_FIX_HELP = ('This PR is marked as a fix; add a test that fails without your change and passes with it. '
                'A PR counts as a fix when its title or branch starts with "fix", or its description says '
                '"fixes a bug", "Bug fix: yes" or "Type: bug". If it is not a fix, change those and rerun.')


def ci_exit(receipt, bug_fix):
    """Require a passing suite, passing declared flows, and repro evidence for bug fixes."""
    checks = receipt['checks']
    tests_passed = receipt['run'] is not None and (checks.get('tests') or ('',))[0] == 'PASS'
    broken = [] if tests_passed else ['tests']
    if 'flows' in checks and checks['flows'][0] != 'PASS':
        broken.append('flows')
    evidence = receipt['repro'] or {}
    tests = evidence.get('tests') or []
    bash_unavailable = fails_then_passes = False
    for test in tests:
        base, head = test.get('base_exit'), test.get('head_exit')
        if test.get('name', '').endswith('.sh::bash'):
            bash_unavailable |= base not in (0, 1) or head not in (0, 1)
        else:
            fails_then_passes |= (base, head) == (1, 0)
    fails_then_passes = fails_then_passes and not any(test.get('kind') == 'regression' for test in tests)
    repro = (checks.get('repro') or ('INCOMPLETE',))[0]
    incomplete_allowed = (repro == 'INCOMPLETE' and not bash_unavailable
                          and (evidence.get('opt_out_reason') or fails_then_passes))
    if bug_fix and repro != 'PASS' and not incomplete_allowed:
        broken.append('repro of a bug fix')
    if broken:
        if 'repro of a bug fix' in broken:
            print(BUG_FIX_HELP, flush=True)
        print(f"COLDVERIFY CI FAIL: {', '.join(broken)}", flush=True)
        return 1
    print(f"COLDVERIFY CI PASS: the receipt says {receipt['result']}. CI requires a passing test suite, "
          'passing declared flows, and repro evidence for bug fixes.', flush=True)
    return 0


def receipt_proofs(body, claim, base, head):
    last = body.rstrip('\n').rpartition('\n')[2]
    if not claim or not last.startswith(REPRO_BLOCK) or not last.endswith(' -->'):
        return None
    try:
        data = json.loads(last[len(REPRO_BLOCK):-len(' -->')])
    except ValueError:
        return None
    if not isinstance(data, dict) or (data.get('base'), data.get('head')) != (base, head):
        return None
    if data.get('claim') != claim_digest(claim):
        if 'criteria_claim' in data:
            if data['criteria_claim'] != claim_digest(criteria_claim(claim)):
                return None
        elif not same_criteria_claim(body, data.get('claim'), claim):
            return None
    tests = data.get('tests')
    return [test if proof_row(test) else None for test in tests] if isinstance(tests, list) else [None]


def criteria_claim(claim):
    return ''.join(line for line in claim.splitlines(keepends=True) if not BUILDER.fullmatch(line.rstrip('\r\n'))).strip()


def same_criteria_claim(body, digest, claim):
    return any(claim_digest(match[1]) == digest and criteria_claim(match[1]) == criteria_claim(claim)
               for match in re.finditer(r'^### Claim\n\n(.*?)\n\n(?=### Acceptance criteria\n)', body, re.M | re.S))


def proof_row(test):
    return (isinstance(test, dict) and isinstance(test.get('name'), str) and isinstance(test.get('kind', ''), str)
            and all(type(test.get(side)) is int for side in ('base_exit', 'head_exit')))


def strict_receipt(root, number, claim, base, head):
    comments = github(root, f'issues/{number}/comments', '--paginate', '--jq', '.[]', lines=True)
    receipts = [comment for comment in comments if (comment.get('user') or {}).get('login') == RECEIPT_AUTHOR
                and STRICT_HEADER.match(str(comment.get('body')))]
    if not receipts:
        return None, None
    tests = receipt_proofs(receipts[-1]['body'], claim, base, head)
    return (receipts[-1].get('html_url'), tests) if tests is not None else (None, None)


def t3_builder(repository, number):
    try:
        state = Path.home() / '.t3/userdata/statev2.sqlite'
        with closing(sqlite3.connect(state.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
            rows = db.execute("""
                SELECT DISTINCT json_extract(r.payload_json, '$.modelSelection.model')
                FROM orchestration_v2_projection_threads t
                JOIN json_each(t.payload_json, '$.pullRequests') link
                LEFT JOIN orchestration_v2_projection_runs r ON r.thread_id = t.thread_id
                WHERE json_extract(link.value, '$.host') = 'github.com'
                  AND lower(json_extract(link.value, '$.repository')) = lower(?)
                  AND json_extract(link.value, '$.number') = ?
                """, (repository, number)).fetchall()
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        raise verify_criteria.Unverified('could not read T3 launch state') from None
    if not rows or any(not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]*', model)
                       for model, in rows):
        raise verify_criteria.Unverified(f'no T3 builder model for {repository}#{number}')
    models = builder_set(model for model, in rows)
    if len(models) != 1:
        raise verify_criteria.Unverified(f'conflicting T3 builder models for {repository}#{number}')
    return models[0]


def criteria_result(root, number):
    try:
        pull = github(root, f'pulls/{number}')
        head, base, body = pull['head']['sha'], pull['base']['sha'], pull.get('body') or ''
        repository = pull['base']['repo']['full_name']
    except (GitHubError, KeyError, TypeError) as error:
        return None, ('INCOMPLETE', 'could not read the PR: ' + (str(error) if isinstance(error, GitHubError) else 'unexpected reply'))
    result = {'pull_request': number, 'head': head, 'base': base,
              'builder_source': None, 'receipt': None,
              'acceptance_criteria': [], 'criteria_verifier': None}
    try:
        builder = t3_builder(repository, number)
    except verify_criteria.Unverified as error:
        return result, ('INCOMPLETE', f'builder identity unverified. {error}')
    result['builder_source'] = 'T3 PR link'
    claim = claim_from(body)
    try:
        result['receipt'], tests = strict_receipt(root, number, claim, base, head)
    except (GitHubError, KeyError, TypeError) as error:
        return result, ('INCOMPLETE', 'could not read the receipts: ' + (str(error) if isinstance(error, GitHubError) else 'unexpected reply'))
    if tests is None:
        return result, ('INCOMPLETE', f'no strict receipt from GitHub Actions on PR #{number} for base {base}, '
                                      f'head {head} and the current claim')
    if len(tests) > MAX_PROOFS or any(test is None or len(test['name']) > MAX_PROOF_NAME or '::' not in test['name'] for test in tests):
        return result, ('INCOMPLETE', f'the receipt lists more than {MAX_PROOFS} repro tests or a malformed test name')
    for commit in (base, head):
        try:
            verify_repro.git(root, 'cat-file', '-e', f'{commit}^{{commit}}')
        except subprocess.CalledProcessError:
            try:
                verify_repro.git(root, 'fetch', '-q', 'origin', commit)
            except subprocess.CalledProcessError as error:
                return result, ('INCOMPLETE', f'could not fetch {commit}: ' + verify_repro.excerpt(error.stderr.decode(errors='replace')))
    check, result['acceptance_criteria'], result['criteria_verifier'] = verify_criteria.check(
        root, criteria_claim(claim), {'base': base, 'head': head, 'tests': tests}, [builder], proved)
    return result, check


def criteria_markdown(result, check):
    lines = [f'## {CRITERIA_CHECK} {check[0]}', '', markdown_field(check[1]), '', INFORMATIONAL, '',
             f"Builder source: {result['builder_source'] or 'none'}", '',
             f"Head: `{result['head']}`", '', f"Receipt: {result['receipt'] or 'none'}", '']
    if result['acceptance_criteria']:
        lines += ['| criterion | covered by |', '| --- | --- |']
        lines += criterion_rows(result['acceptance_criteria'])
    return '\n'.join(lines)


def criteria_check(root, number, publish=False):
    from verify_context import coldbox_guest
    if os.environ.get('GITHUB_ACTIONS') == 'true' or coldbox_guest():
        mode = '--publish-check' if publish else '--criteria-check'
        print(f'COLDVERIFY CRITERIA INCOMPLETE: {mode} is refused in CI; {CRITERIA_CHECK} runs on a trusted host', flush=True)
        return 2
    result, check = criteria_result(Path(root), number)
    if result:
        result.update(result=check[0], reason=check[1])
        print(json.dumps(result, indent=2), flush=True)
    if publish and result:
        run = {'name': CRITERIA_CHECK, 'head_sha': result['head'], 'status': 'completed',
               'conclusion': 'success' if check[0] == 'PASS' else 'failure',
               'output': {'title': f"{check[0]}: {check[1]}"[:255], 'summary': criteria_markdown(result, check)[:CHECK_TEXT_LIMIT],
                          'text': json.dumps(result, indent=2)[:CHECK_TEXT_LIMIT]}}
        try:
            github(root, 'check-runs', '--method', 'POST', '--input', '-', payload=json.dumps(run))
        except GitHubError as error:
            check = ('INCOMPLETE', f'{check[1]}; the {CRITERIA_CHECK} check was not published: {error}')
    print(f'COLDVERIFY CRITERIA {check[0]}: {check[1]}', flush=True)
    return CODES.get(check[0], 1)
