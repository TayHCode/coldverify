import json
import os
import shutil


def confined(readable, outside, run):
    codex = os.path.realpath(shutil.which('codex') or 'codex')
    paths = {':minimal': 'read', codex: 'read', str(readable): 'read', '/etc': 'none'}
    profile = ['--config', 'default_permissions="cold-verifier"', '--config', 'permissions.cold-verifier.filesystem={'
               + ','.join(f'{json.dumps(path, ensure_ascii=False)}={json.dumps(access)}' for path, access in paths.items())
               + '}']
    run(['codex', 'sandbox', *profile, '--', 'sh', '-c', 'test -r "$1" && ! test -r "$2"', 'probe', str(readable),
         str(outside)], readable)
    return profile
