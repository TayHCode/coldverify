"""Top-level integration tests for flows that launch Bubblewrap themselves."""
import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import criteria_shim
from repo_checkout import SOURCE, git
from test_verify_explicit import needs_bwrap
import verify_criteria
import verify_flows
import verify_receipt
import verify_repro


ARCHIVE_FLOW = '.fleet/flows/coldpass-archive-host'
ARCHIVE_CLAIM = 'The archive reports the guest hostname as its source host.'
ARCHIVE_UNIT = '[Service]\nType=oneshot\nExecStart=/bin/false\n'


@needs_bwrap
class SystemdFlows(unittest.TestCase):
    def archive_revision_pair(self, head_script=None):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        repo = Path(folder.name) / 'repo'
        git(repo.parent, 'init', '-q', str(repo))
        deploy = repo / 'deploy'
        deploy.mkdir()
        unit = deploy / 'coldpass-archive.service'
        unit.write_text(ARCHIVE_UNIT + 'Environment=COLDPASS_ARCHIVE_HOST=wrong-host\n')
        archive = deploy / 'archive-to-nas.sh'
        archive.write_text(
            'set -eu\n'
            'test -z "$COLDPASS_ARCHIVE_NAS"\n'
            'test -f "$COLDPASS_ARCHIVE_ROOT/local/sample.txt"\n'
            'printf "archive-to-nas: host=%s\\n" "${COLDPASS_ARCHIVE_HOST:-$(hostname -s)}"\n')

        def commit(message):
            git(repo, 'add', '-A')
            git(repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
                '-c', 'commit.gpgsign=false', 'commit', '-qm', message)
            return git(repo, 'rev-parse', 'HEAD').strip()

        base = commit('Pin the archive source host to the wrong hostname')
        unit.write_text(ARCHIVE_UNIT)
        if head_script is not None:
            archive.write_text(head_script)
        flow = repo / ARCHIVE_FLOW
        flow.parent.mkdir(parents=True)
        shutil.copy2(SOURCE / 'examples/flows/coldpass-archive-host', flow)
        head = commit('Use the guest hostname for the archive source')
        (repo / '.cold').mkdir()
        return repo, dict(base=base, head=head, host=verify_flows.host_name(), backend='local',
                          tests=[], error=verify_repro.NO_TEST_CASES)

    def archive_receipt(self, repo, repro, flows):
        shims = repo.parent / 'shims'
        shims.mkdir()
        log = repo.parent / 'criteria.jsonl'
        env = criteria_shim.install(shims, log)
        with mock.patch.dict(os.environ, {**env, 'PATH': f'{shims}{os.pathsep}{os.environ["PATH"]}',
                                         'SHIM_CRITERIA': json.dumps([[ARCHIVE_CLAIM, ARCHIVE_FLOW]])}), \
                mock.patch.object(verify_criteria, 'coldbox_guest', return_value=None), \
                contextlib.redirect_stdout(io.StringIO()):
            verify_receipt.finish(repo, ARCHIVE_CLAIM, None, 2, ['coldverify', '--ci'],
                                  {'version': 'fixture', 'commit': None}, builders=(criteria_shim.BUILDER,),
                                  repro=repro, flows=flows)
        return json.loads((repo / '.cold/verify-receipt.json').read_text())

    def test_archive_unit_observation_covers_the_claim(self):
        repo, repro = self.archive_revision_pair()
        flows = verify_flows.run(repo, repro)
        self.assertEqual(flows['result'], 'PASS', flows['reason'])
        [flow] = flows['flows']
        self.assertEqual((flow['base_exit'], flow['head_exit'], flow['kind']), (1, 0, 'proof'))
        hostname = subprocess.check_output(['hostname', '-s'], text=True).strip()
        for side, host in (('base', 'wrong-host'), ('head', hostname)):
            summary = f'Result=success. Archive source host={host}. Guest hostname={hostname}.'
            self.assertTrue(flow[f'{side}_output'].endswith(summary), flow[f'{side}_output'])
            saved = repo / '.cold/flows/coldpass-archive-host' / side / 'artifacts'
            self.assertEqual((saved / 'archive-output.txt').read_text(), f'archive-to-nas: host={host}\n')
            self.assertIn('Result=success', (saved / 'unit-result.txt').read_text().splitlines())
        receipt = self.archive_receipt(repo, repro, flows)
        self.assertEqual(receipt['checks']['repro'],
                         ['PASS', 'observed flows reproduced the claim on base and passed on head'])
        self.assertEqual(receipt['acceptance_criteria'],
                         [{'id': 'C1', 'text': ARCHIVE_CLAIM, 'covered_by': [ARCHIVE_FLOW]}])

    def test_archive_flow_without_systemd_leaves_the_claim_incomplete(self):
        repo, repro = self.archive_revision_pair()
        with mock.patch.object(verify_flows.verify_flow_systemd, 'needs_manager', return_value=False):
            flows = verify_flows.run(repo, repro)
        self.assertEqual(flows['result'], 'INCOMPLETE', flows['reason'])
        [flow] = flows['flows']
        self.assertEqual((flow['base_exit'], flow['head_exit'], flow['kind']), (125, 125, 'unrunnable'))
        self.assertIn('the Coldverify private systemd manager is unavailable', flows['reason'])
        receipt = self.archive_receipt(repo, repro, flows)
        self.assertEqual(receipt['result'], 'INCOMPLETE')
        self.assertEqual(receipt['checks']['flows'][0], 'INCOMPLETE')
        self.assertEqual(receipt['acceptance_criteria'][0]['covered_by'], [])

    def test_archive_command_failure_keeps_stderr_and_fails_the_flow(self):
        repo, repro = self.archive_revision_pair('echo "archive input unreadable" >&2\nexit 3\n')
        flows = verify_flows.run(repo, repro)
        self.assertEqual(flows['result'], 'FAIL', flows['reason'])
        [flow] = flows['flows']
        self.assertEqual((flow['head_exit'], flow['kind']), (1, 'regression'))
        self.assertIn('archive input unreadable Archive command exited 3.', flow['head_output'])
        saved = repo / '.cold/flows/coldpass-archive-host/head/artifacts'
        self.assertEqual((saved / 'archive-error.txt').read_text(), 'archive input unreadable\n')
        self.assertIn('ExecMainStatus=3', (saved / 'unit-result.txt').read_text().splitlines())

    def run_flow(self, script):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            repo = folder / 'repo'
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            flow = folder / 'checkout' / 'flow'
            flow.parent.mkdir()
            flow.write_text('#!/bin/sh\nset -eu\n' + script)
            flow.chmod(0o755)
            code, output = verify_flows.run_flow(flow, flow.parent, 'head', folder, folder / 'saved',
                                                 sandbox_root=repo)
        self.assertEqual(code, 0, output)

    def test_systemd_flow_runs_a_user_unit_and_reads_its_private_journal(self):
        self.run_flow('''
systemd-run --user --unit=flow-probe --property=Type=oneshot /bin/false && exit 2
[ "$(systemctl --user is-failed flow-probe)" = failed ]
for attempt in $(seq 1 20); do
    journalctl --user --unit flow-probe --output json --no-pager | grep -q '"JOB_RESULT":"failed"' && break
    sleep 0.1
done
journalctl --user --unit flow-probe --output json --no-pager | grep -q '"JOB_RESULT":"failed"'
systemctl --user reset-failed flow-probe
[ "$(systemctl --user show --property LoadState --value flow-probe)" = not-found ]
''')

    def test_systemd_flow_cannot_reach_the_outer_user_bus(self):
        runtime = f'/run/user/{os.getuid()}'
        bus = f'unix:abstract=coldverify-{uuid.uuid4().hex}'
        with subprocess.Popen(['dbus-daemon', '--session', '--nofork', f'--address={bus}', '--print-address=1'],
                              text=True, stdout=subprocess.PIPE) as daemon:
            try:
                self.assertTrue(daemon.stdout.readline().strip().startswith(bus))
                subprocess.run(['busctl', f'--address={bus}', 'list'], check=True, stdout=subprocess.DEVNULL)
                self.run_flow(f'''
systemctl --user show --property SystemState >/dev/null
[ "$DBUS_SESSION_BUS_ADDRESS" != {shlex.quote(bus)} ]
[ ! -S {shlex.quote(runtime + '/bus')} ]
[ ! -S {shlex.quote(runtime + '/systemd/private')} ]
[ ! -S /run/dbus/system_bus_socket ]
if busctl --address={shlex.quote(bus)} list; then exit 2; fi
''')
            finally:
                daemon.terminate()
