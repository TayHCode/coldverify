import json
from pathlib import Path
import sys
import tomllib

BUILDER = 'claude-opus-5-5'
COLD = f'''#!{sys.executable}
import json, os, sys
with open(os.environ['CRITERIA_LOG'], 'a') as log:
    log.write(json.dumps({{'tool': 'cold', 'args': sys.argv[1:]}}) + '\\n')
print(os.environ.get('SHIM_ROUTE') or json.dumps({{'provider': 'codex', 'model': 'gpt-6.1-terra', 'effort': 'high'}}))
'''
CODEX = f'''#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
if args[0] == 'sandbox':
    command = args[args.index('--') + 1:]
    with open(os.environ['CRITERIA_LOG'], 'a') as log:
        log.write(json.dumps({{'tool': 'sandbox', 'args': args, 'shim': os.path.realpath(__file__)}}) + '\\n')
    if os.environ.get('SHIM_SANDBOX_LEAKS'):
        os.execvp(command[0], command)
    sys.exit(0)
prompt = sys.stdin.read()
schema = json.load(open(args[args.index('--output-schema') + 1]))
cwd = os.getcwd()
files = sorted(os.path.relpath(os.path.join(top, name), cwd) for top, _, names in os.walk(cwd) for name in names)
contents = {{name: open(name, 'rb').read().hex() for name in files}} if os.environ.get('SHIM_RECORD_BYTES') else {{}}
with open(os.environ['CRITERIA_LOG'], 'a') as log:
    log.write(json.dumps({{'tool': 'codex', 'args': args, 'prompt': prompt, 'files': files, 'contents': contents}}) + '\\n')
if os.environ.get('SHIM_CODEX_FAIL'):
    print(os.environ['SHIM_CODEX_FAIL'], file=sys.stderr)
    sys.exit(1)
planned = json.loads(os.environ.get('SHIM_CRITERIA') or '[["the claim holds", "::"]]')
if 'approved' in schema['properties']:
    answer = os.environ.get('SHIM_SIGN_OFF') or json.dumps({{'approved': True, 'reason': 'the diff does what the claim says'}})
elif 'criteria' in schema['properties']:
    answer = os.environ.get('SHIM_DERIVED') or json.dumps(
        {{'criteria': [{{'id': f'C{{index}}', 'text': text}} for index, (text, _) in enumerate(planned, 1)]}})
else:
    proofs = json.loads(next(line for line in prompt.splitlines() if line.startswith('PROOF_TESTS_JSON: '))[18:])
    for line in prompt.splitlines():
        if line.startswith('FLOW_OBSERVATIONS_JSON: '):
            proofs.extend(json.loads(line.removeprefix('FLOW_OBSERVATIONS_JSON: ')))
    answer = os.environ.get('SHIM_LINKS') or json.dumps({{'links': [
        {{'id': f'C{{index}}', 'tests': [name for name in proofs if covers and covers in name]}}
        for index, (_, covers) in enumerate(planned, 1)]}})
open(args[args.index('--output-last-message') + 1], 'w').write(answer)
'''


def install(directory, log):
    for name, script in (('cold', COLD), ('codex', CODEX)):
        (Path(directory) / name).write_text(script)
        (Path(directory) / name).chmod(0o755)
    return {'CRITERIA_LOG': str(log)}


def filesystem(args):
    profile = args[args.index('default_permissions="cold-verifier"') + 2]
    return tomllib.loads(profile)['permissions']['cold-verifier']['filesystem']


def calls(log):
    return [json.loads(line) for line in Path(log).read_text().splitlines()] if Path(log).is_file() else []
