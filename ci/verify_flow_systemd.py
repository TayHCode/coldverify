"""Start a user manager, session bus and journal inside a Linux flow sandbox."""
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path


def needs_manager(flow):
    return any(re.search(rb'(?<![\w-])(?:systemctl|systemd-run|loginctl)(?![\w-])', line)
               for line in flow.read_bytes().splitlines() if not line.lstrip().startswith(b'#'))


def run(prefix, flow, checkout, env, output, status):
    from verify_context import coldbox_guest
    from verify_repro import CASE_TIMEOUT, NoRepro, run_sandboxed

    unit = f'coldverify-flow-{uuid.uuid4().hex}'
    launcher = [sys.executable, '-I', str(Path(__file__).resolve())]
    uid = os.getuid()
    guest = coldbox_guest()
    if guest:
        service = unit + '.service'
        command = [
            '/usr/bin/sudo', '-n', '/usr/bin/systemd-run', '--quiet', '--wait', '--pipe', '--collect',
            '--expand-environment=no', f'--unit={service}', f'--uid={uid}', f'--gid={os.getgid()}',
            '--property=Delegate=yes', '--property=NoNewPrivileges=yes', f'--property=RuntimeMaxSec={CASE_TIMEOUT}s',
            f'--working-directory={checkout}', '--', '/usr/bin/env', '-i',
            *(f'{key}={value}' for key, value in env.items()), *launcher, service, str(status), *prefix,
        ]
    else:
        scope = unit + '.scope'
        command = ['systemd-run', '--user', '--scope', '--quiet', f'--unit={scope}', '--property=Delegate=yes',
                   '--', *launcher, scope, '', *prefix]
        env = dict(env, XDG_RUNTIME_DIR=f'/run/user/{uid}')

    code = run_sandboxed(command, [str(flow)], checkout, env, output=output)
    if guest and (not status.is_file() or status.read_text() != str(code)):
        raise NoRepro('the delegated systemd service did not return the sandbox exit status')
    return code


BOOTSTRAP = r'''
set -eu
group=$1
shift
[ "$(cat /proc/self/cgroup)" = '0::/' ] || { echo 'flow cgroup namespace is unavailable' >&2; exit 125; }
mkdir -p /run/systemd/system /run/systemd/journal
touch /run/systemd/journal/flushed
/usr/lib/systemd/systemd-journald &
for attempt in $(seq 1 100); do
    [ ! -S /run/systemd/journal/socket ] || break
    sleep 0.1
done
[ -S /run/systemd/journal/socket ] || { echo 'private journal did not start' >&2; exit 125; }
dbus-daemon --session --address="$DBUS_SESSION_BUS_ADDRESS" --fork --nopidfile
/bin/sh -ec '
    echo 0 > "$1/cgroup.procs"
    export SYSTEMD_LOG_TARGET=journal SYSTEMD_LOG_LEVEL=info
    exec /usr/lib/systemd/systemd --user --unit=basic.target
' sh "$group" &
for attempt in $(seq 1 100); do
    case $(systemctl --user is-system-running 2>/dev/null) in
        running|degraded) exec "$@" ;;
    esac
    sleep 0.1
done
echo 'private user manager did not start' >&2
exit 125
'''


def run_in_cgroup(unit, status, command):
    group = next((line[3:] for line in Path('/proc/self/cgroup').read_text().splitlines()
                  if line.startswith('0::')), '')
    if not group.startswith('/') or Path(group).name != unit:
        sys.exit('could not enter a dedicated cgroup v2 subtree for the flow')
    cgroups = Path('/sys/fs/cgroup')
    delegated = cgroups / group.lstrip('/')
    uid = os.getuid()
    # journalctl --user derives the journal's owner UID from the user slice.
    user_group = Path(f'user.slice/user-{uid}.slice/user@{uid}.service')
    (delegated / user_group).mkdir(parents=True)

    config = os.memfd_create('coldverify-journald')
    os.write(config, b'[Journal]\nStorage=persistent\nSplitMode=uid\n')
    os.lseek(config, 0, os.SEEK_SET)
    remount = command.index('--remount-ro')
    command[remount:remount] = [
        '--tmpfs', '/run/systemd', '--tmpfs', '/run/log', '--tmpfs', '/var/log/journal',
        '--perms', '0700', '--tmpfs', '/run/coldverify',
        '--setenv', 'XDG_RUNTIME_DIR', '/run/coldverify',
        '--setenv', 'DBUS_SESSION_BUS_ADDRESS', 'unix:path=/run/coldverify/bus',
        '--ro-bind-data', str(config), '/etc/systemd/journald.conf',
        '--ro-bind', str(delegated), str(cgroups),
        '--bind', str(delegated / user_group), str(cgroups / user_group),
    ]
    entry = command.index('--') + 1
    command[entry:entry] = ['/bin/sh', '-c', BOOTSTRAP, 'sh', str(cgroups / user_group)]
    if status:
        code = subprocess.call(command, pass_fds=(config,))
        code = code if code >= 0 else 128 - code
        Path(status).write_text(str(code))
        sys.exit(code)
    os.set_inheritable(config, True)
    os.execv(command[0], command)


if __name__ == '__main__':
    run_in_cgroup(sys.argv[1], sys.argv[2], sys.argv[3:])
