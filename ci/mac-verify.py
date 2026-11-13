#!/usr/bin/env python3
import argparse
import os
import json
import platform
from pathlib import Path
import shlex
import shutil
import sys
import subprocess
import tempfile
import uuid

import verify_policy
import inspect
from verify_context import snapshot_tree


def git(directory, *args):
    env = {key: value for key, value in os.environ.items() if key not in
           ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR',
            'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES')}
    return subprocess.check_output(['git', '-C', str(directory), *args], text=True, env=env).strip()


REMOTE = r'''
import json,os,pathlib,platform,shutil,subprocess,sys,hashlib,stat
from pathlib import Path
__SNAPSHOT_FUNCTION__
for key in ['GIT_DIR','GIT_WORK_TREE','GIT_INDEX_FILE','GIT_COMMON_DIR','GIT_OBJECT_DIRECTORY','GIT_ALTERNATE_OBJECT_DIRECTORIES']:
 os.environ.pop(key,None)
root=pathlib.Path(sys.argv[1]); sha,tree,host,repo=sys.argv[2:]
if platform.system()!='Darwin' or platform.node().casefold()!=host.casefold():
 raise SystemExit('INCOMPLETE: approved native macOS host required')
checkout=root/'checkout'
try:
 subprocess.run(['git','clone','--quiet',str(root/'source.bundle'),str(checkout)],check=True)
 def git(*args):return subprocess.check_output(['git','-C',str(checkout),*args],text=True).strip()
 subprocess.run(['git','-C',str(checkout),'checkout','--quiet','--detach',sha],check=True)
 subprocess.run(['git','-C',str(checkout),'remote','set-url','origin','https://github.com/'+repo+'.git'],check=True)
 if git('rev-parse','HEAD')!=sha or git('rev-parse','HEAD^{tree}')!=tree or git('status','--porcelain'):
  raise SystemExit('INCOMPLETE: native checkout identity differs')
 for name in git('ls-files','-z').split('\0'):
  if not name:continue
  path=checkout/name
  if path.is_symlink() and not path.resolve().is_relative_to(checkout.resolve()):
   raise SystemExit('INCOMPLETE: repository symlink escapes the native checkout')
 env=os.environ.copy()
 for name in ['GH_TOKEN','GITHUB_TOKEN','FLEET_GITHUB_APP','OPENAI_API_KEY','ANTHROPIC_API_KEY','CLAUDE_CODE_OAUTH_TOKEN','CODEX_API_KEY','XAI_API_KEY','GROK_API_KEY']:
  env.pop(name,None)
 for name,folder in [('HOME','home'),('TMPDIR','scratch'),('XDG_CONFIG_HOME','config'),('XDG_STATE_HOME','state'),('XDG_CACHE_HOME','cache'),('XDG_DATA_HOME','data')]:
  target=root/folder;target.mkdir();env[name]=str(target)
 env['CI']='true'
 snapshot_digest=snapshot_tree(checkout)
 result=subprocess.run(['bash','.fleet/verify.sh'],cwd=checkout,env=env,stdin=subprocess.DEVNULL)
 clean=not git('status','--porcelain')
 receipt=dict(snapshot_tree=snapshot_digest,repo=repo,host=platform.node(),platform='macos',sha=sha,tree=tree,exit_code=result.returncode,clean=clean,evidence=str(root))
 (root/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
 print('FLEET_NATIVE_RECEIPT '+json.dumps(receipt),flush=True)
 raise SystemExit(result.returncode if result.returncode else (0 if clean else 2))
finally:
 if checkout.exists() and not checkout.is_symlink():
  shutil.rmtree(checkout)
 (root/'source.bundle').unlink(missing_ok=True)

'''


def verify(directory):
    if platform.system() not in ('Linux', 'Darwin'):
        raise RuntimeError('run native verification from Linux or the approved Mac host')
    # Reuse the established owned-repository resolver; no credentials travel to macOS.
    import importlib.util
    spec = importlib.util.spec_from_file_location('linux_verify_resolver', Path(__file__).resolve().parent / 'linux-verify.py')
    resolver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(resolver)
    repo = resolver.repository(directory)
    if not verify_policy.macos_repository(repo):
        raise ValueError('repository has no approved macOS exception')
    if git(directory, 'status', '--porcelain'):
        raise ValueError('commit the candidate before exact-tree native macOS verification')
    sha, tree = git(directory, 'rev-parse', 'HEAD'), git(directory, 'rev-parse', 'HEAD^{tree}')
    host = verify_policy.verification_policy()['macos_hosts'][0]
    native = platform.system() == 'Darwin'
    if native:
        if platform.node().casefold() not in {h.casefold() for h in verify_policy.verification_policy()['macos_hosts']}:
            raise RuntimeError('approved native macOS host required')
    elif platform.node().casefold() not in {h.casefold() for h in verify_policy.verification_policy()['linux_hosts']}:
        raise RuntimeError('approved native coordinator required')
    run_id = uuid.uuid4().hex
    # Local and remote Mac proofs use the same isolated checkout and environment.
    remote = '.local/state/agentfleet/native-macos/' + run_id
    options = ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=30', '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=6']
    if native:
        proof = Path.home() / remote
        proof.mkdir(parents=True, mode=0o700)
        path = str(proof)
    else:
        setup = 'umask 077; mkdir -p "$HOME/.local/state/agentfleet/native-macos"; mkdir "$HOME/' + remote + '"; printf "%s" "$HOME/' + remote + '"'
        path = subprocess.check_output(['ssh', *options, '-n', host, setup], text=True).strip()
        if not path.startswith('/') or not path.endswith('/' + remote) or '\n' in path:
            raise RuntimeError('native proof directory readback differs')
    with tempfile.TemporaryDirectory(prefix='agentfleet-mac-bundle-') as folder:
        bundle = Path(folder) / 'source.bundle'
        git(directory, 'bundle', 'create', str(bundle), 'HEAD')
        if native:
            shutil.copyfile(bundle, Path(path) / 'source.bundle')
        else:
            subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=30', str(bundle), host + ':' + path + '/source.bundle'], check=True)
    if native:
        command = [sys.executable, '-', path, sha, tree, host, repo]
    else:
        command = ['ssh', *options, host, shlex.join(['python3', '-', path, sha, tree, host, repo])]
    result = subprocess.run(command, input=REMOTE.replace('__SNAPSHOT_FUNCTION__', inspect.getsource(snapshot_tree)), text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=3600)
    print(result.stdout, end='', flush=True)
    records = [line.removeprefix('FLEET_NATIVE_RECEIPT ') for line in result.stdout.splitlines() if line.startswith('FLEET_NATIVE_RECEIPT ')]
    if not records:
        raise RuntimeError('native macOS verification has no receipt')
    receipt = json.loads(records[-1])
    if (receipt.get('repo') != repo or receipt.get('sha') != sha or receipt.get('tree') != tree or receipt.get('host', '').casefold() != host.casefold()
            or receipt.get('platform') != 'macos'):
        raise RuntimeError('native macOS receipt identity differs')
    if os.environ.get('COLD_VERIFY_SNAPSHOT_RECEIPT'):
        Path(os.environ['COLD_VERIFY_SNAPSHOT_RECEIPT']).write_text(json.dumps({'snapshot_tree': receipt.get('snapshot_tree'), 'backend': 'local' if native else 'ssh:' + host}))
    if receipt.get('exit_code') != 0 or receipt.get('clean') is not True:
        raise subprocess.CalledProcessError(result.returncode or 1, ['native-macos-suite'])
    if result.returncode:
        raise RuntimeError('native macOS transport failed')
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    try:
        verify(args.directory.resolve())
    except subprocess.CalledProcessError:
        raise SystemExit('FAIL: native macOS suite failed; inspect retained native logs') from None
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        raise SystemExit('INCOMPLETE: native macOS verification failed; inspect retained native logs') from None
