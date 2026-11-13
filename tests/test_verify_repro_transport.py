import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ci'))
import verify_backend
import verify_context
import verify_repro_transport as transport
import synthetic_policy


def setUpModule():
    scratch = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(scratch.cleanup)
    unittest.addModuleCleanup(os.chdir, os.getcwd())
    os.chdir(scratch.name)
    synthetic_policy.use(unittest.addModuleCleanup)


class NativeReproTransport(unittest.TestCase):
    def test_generic_linux_repro_runs_locally_in_a_coldbox_guest(self):
        request = {'details': ['fix', '', 'base', 'head']}
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / 'host'
            marker.write_text('runner-a\n')
            with patch.dict(os.environ, {'COLDBOX_GUEST': '1'}), \
                 patch.object(verify_context, 'COLDBOX_HOST_FILE', marker), \
                 patch.object(transport, 'prepare', return_value=request), \
                 patch.object(transport.subprocess, 'check_output', return_value='linux-generic\n'), \
                 patch.object(transport.platform, 'node', return_value='outsider-box'), \
                 patch.object(transport.platform, 'system', return_value='Linux'), \
                 patch.object(transport.verify_repro, 'gate', return_value=0) as gate:
                self.assertEqual(transport.run(Path.cwd()), 0)
        gate.assert_called_once_with(Path.cwd(), False, None, request['details'], False)

    def test_windows_repro_is_blocked_before_any_ssh(self):
        with patch.object(transport, 'prepare', return_value={'details': ['fix', '', 'base', 'head']}), \
             patch.object(transport.subprocess, 'check_output', return_value='windows\n'), \
             patch.object(transport.subprocess, 'run') as remote:
            self.assertEqual(transport.run(Path.cwd()), 1)
        remote.assert_not_called()

    def test_remote_refuses_a_platform_it_cannot_run_before_cloning(self):
        for name in ('windows', 'solaris'):
            with self.subTest(platform=name), tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp)
                (folder / 'request.json').write_text(json.dumps({'platform': name, 'host': 'runner-a'}), encoding='utf-8')
                with patch.object(transport.platform, 'system', return_value='Linux'), \
                     patch.object(transport.platform, 'release', return_value='6.8.0-generic'), \
                     patch.object(transport.subprocess, 'run') as clone:
                    with self.assertRaises(Exception) as refused:
                        transport.remote(folder)
                self.assertIsInstance(refused.exception, RuntimeError)
                self.assertIn('wrong platform', str(refused.exception))
                clone.assert_not_called()

    def test_opt_out_does_not_need_a_base_or_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(['git', '-C', tmp, 'init', '-q', '-b', 'main'], check=True)
            subprocess.run(['git', '-C', tmp, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
                            'commit', '--allow-empty', '-qm', 'head'], check=True)
            with patch.object(transport.verify_repro, 'metadata', return_value=('', '', '', '')):
                request = transport.prepare(root, True, 'No observable test exists')
            self.assertEqual(request['details'][2], '')
            self.assertEqual(request['reason'], 'No observable test exists')

    def test_shallow_checkout_is_completed_before_packaging(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source'
            source.mkdir()
            def git(root, *args):
                return subprocess.check_output(['git', '-C', str(root), *args], text=True, stderr=subprocess.DEVNULL).strip()
            git(source, 'init', '-q', '-b', 'main')
            git(source, 'config', 'user.name', 'Fixture')
            git(source, 'config', 'user.email', 'fixture@localhost')
            for value in ('initial', 'base', 'head'):
                (source / 'value').write_text(value)
                git(source, 'add', '.')
                git(source, 'commit', '-qm', value)
                if value == 'base':
                    base = git(source, 'rev-parse', 'HEAD')
            head = git(source, 'rev-parse', 'HEAD')
            checkout = Path(tmp) / 'checkout'
            subprocess.run(['git', 'clone', '-q', '--depth=2', source.as_uri(), str(checkout)], check=True)
            git(checkout, 'switch', '-qc', 'fix/fixture')
            self.assertEqual(git(checkout, 'rev-parse', '--is-shallow-repository'), 'true')
            with patch.object(transport.verify_repro, 'metadata', return_value=('fix fixture', '', base, head)):
                request = transport.prepare(checkout, False, None)
            self.assertEqual(request['details'][2:], [base, head])
            self.assertEqual(git(checkout, 'rev-parse', '--is-shallow-repository'), 'false')


class InfrastructureErrorsAreIncomplete(unittest.TestCase):
    def setUp(self):
        for isolation in (patch.object(verify_context, 'COLDBOX_HOST_FILE', Path('/nonexistent/coldbox/host')),
                          patch.dict(os.environ, COLD_VERIFY_BACKEND='ssh:runner-a')):
            isolation.start()
            self.addCleanup(isolation.stop)

    def evidence(self, root):
        return json.loads((Path(root) / '.cold/verify-repro.json').read_text())

    def test_a_failed_fetch_while_preparing_is_incomplete_with_the_git_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(['git', '-C', tmp, 'init', '-q', '-b', 'fix/fixture'], check=True)
            subprocess.run(['git', '-C', tmp, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
                            'commit', '--allow-empty', '-qm', 'head'], check=True)
            (Path(tmp) / '.git/info/exclude').write_text('.cold/\n')
            head = subprocess.check_output(['git', '-C', tmp, 'rev-parse', 'HEAD'], text=True).strip()
            with patch.object(transport.verify_repro, 'metadata', return_value=('fix fixture', '', 'f' * 40, head)):
                self.assertEqual(transport.run(Path(tmp)), 2)
            evidence = self.evidence(tmp)
            self.assertEqual(evidence['result'], 'INCOMPLETE')
            self.assertTrue(evidence['error'].startswith('git fetch --depth=1 failed: '), evidence['error'])

    def test_no_pr_base_and_no_origin_main_is_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(['git', '-C', tmp, 'init', '-q'], check=True)
            subprocess.run(['git', '-C', tmp, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
                            'commit', '--allow-empty', '-qm', 'head'], check=True)
            (Path(tmp) / '.git/info/exclude').write_text('.cold/\n')
            with patch.object(transport.verify_repro, 'metadata', return_value=('', '', '', '')):
                self.assertEqual(transport.run(Path(tmp), force=True), 2)
            evidence = self.evidence(tmp)
            self.assertEqual(evidence['result'], 'INCOMPLETE')
            self.assertIn('origin/main', evidence['error'])

    def test_a_failed_transfer_is_incomplete_with_the_remote_error(self):
        def ssh(command, **kwargs):
            if command[0] == 'ssh' and command[-1].startswith('tar -xf'):
                return subprocess.CompletedProcess(command, 2, stderr='tar: write error: No space left on device')
            return subprocess.CompletedProcess(command, 0, stderr='')

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(transport, 'prepare', return_value={'details': ['fix', '', 'base', 'head'], 'checkout': 'head'}), \
             patch.object(transport.subprocess, 'check_output', return_value='linux\n'), \
             patch.object(transport.platform, 'node', return_value='control-a'), \
             patch.object(transport.subprocess, 'run', side_effect=ssh), \
             patch.object(transport.subprocess, 'Popen') as tar, \
             patch.object(transport.shutil, 'rmtree'):
            tar.return_value.wait.return_value = 0
            self.assertEqual(transport.run(Path(tmp)), 2)
            evidence = self.evidence(tmp)
        self.assertEqual(evidence, {'result': 'INCOMPLETE', 'backend': 'ssh:runner-a',
                                    'error': 'native repro transfer failed: tar: write error: No space left on device'})

    def test_a_failed_staging_fetch_is_incomplete_with_the_git_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(['git', '-C', tmp, 'init', '-q'], check=True)
            request = {'details': ['fix', '', 'f' * 40, 'e' * 40], 'checkout': 'e' * 40}
            with patch.object(transport, 'prepare', return_value=request), \
                 patch.object(transport.subprocess, 'check_output', return_value='linux\n'), \
                 patch.object(transport.platform, 'node', return_value='control-a'):
                self.assertEqual(transport.run(Path(tmp)), 2)
            evidence = self.evidence(tmp)
        self.assertEqual(evidence['result'], 'INCOMPLETE')
        self.assertTrue(evidence['error'].startswith('git fetch -q failed: '), evidence['error'])

    def test_a_connection_lost_after_the_transfer_is_incomplete(self):
        def ssh(command, **kwargs):
            if command[0] == 'ssh' and 'verify_repro_transport.py --remote' in command[-1]:
                return subprocess.CompletedProcess(command, 255, stdout='Connection to runner-a closed by remote host.')
            return subprocess.CompletedProcess(command, 0, stderr='')

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(transport, 'prepare', return_value={'details': ['fix', '', 'base', 'head'], 'checkout': 'head'}), \
             patch.object(transport.subprocess, 'check_output', return_value='linux\n'), \
             patch.object(transport.platform, 'node', return_value='control-a'), \
             patch.object(transport.subprocess, 'run', side_effect=ssh), \
             patch.object(transport.subprocess, 'Popen') as tar, \
             patch.object(transport.shutil, 'rmtree'):
            tar.return_value.wait.return_value = 0
            self.assertEqual(transport.run(Path(tmp)), 2)
            evidence = self.evidence(tmp)
        self.assertEqual(evidence, {'result': 'INCOMPLETE', 'backend': 'ssh:runner-a',
                                    'error': 'lost the connection to runner-a: Connection to runner-a closed by remote host.'})

    def test_a_local_staging_error_is_incomplete_with_the_error(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(transport, 'prepare', return_value={'details': ['fix', '', 'base', 'head'], 'checkout': 'head'}), \
             patch.object(transport.subprocess, 'check_output', return_value='linux\n'), \
             patch.object(transport.platform, 'node', return_value='control-a'), \
             patch.object(transport.verify_repro, 'setup_git'), \
             patch.object(transport.shutil, 'rmtree'), \
             patch.object(transport.shutil, 'copy2', side_effect=OSError(28, 'No space left on device')), \
             patch.object(transport.subprocess, 'run') as ssh:
            self.assertEqual(transport.run(Path(tmp)), 2)
            evidence = self.evidence(tmp)
        ssh.assert_not_called()
        self.assertEqual(evidence, {'result': 'INCOMPLETE',
                                    'error': 'could not stage the repro: [Errno 28] No space left on device'})


def stage_without_git(_, *args):
    if args[0] == 'init':
        Path(args[-1]).mkdir()


class ColdboxGuestRepro(unittest.TestCase):
    def run_repro(self, marker_text, release='6.8.0-generic', node='coldbox-job-7'):
        request = {'details': ['fix', '', 'base', 'head'], 'checkout': 'head'}
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / 'host'
            if marker_text is not None:
                marker.write_text(marker_text)
            with patch.object(verify_context, 'COLDBOX_HOST_FILE', marker), \
                 patch.object(transport, 'prepare', return_value=request), \
                 patch.object(transport.subprocess, 'check_output', return_value='linux\n'), \
                 patch.object(transport.platform, 'node', return_value=node), \
                 patch.object(transport.platform, 'system', return_value='Linux'), \
                 patch.object(transport.platform, 'release', return_value=release), \
                 patch.object(transport.verify_repro, 'setup_git', side_effect=stage_without_git), \
                 patch.object(verify_backend, 'shell', side_effect=verify_backend.Unavailable('no coldbox')) as shell, \
                 patch.object(transport.verify_repro, 'gate', return_value=0) as gate:
                code = transport.run(Path.cwd())
        return code, gate, shell

    def test_guest_marker_runs_the_repro_gate_locally_without_ssh(self):
        code, gate, shell = self.run_repro('runner-a\n')
        self.assertEqual(code, 0)
        gate.assert_called_once_with(Path.cwd(), False, None, ['fix', '', 'base', 'head'], False)
        shell.assert_not_called()

    def test_every_listed_host_runs_the_repro_gate_locally_without_ssh(self):
        for host in ('runner-a', 'ops-a', 'builder-b', 'builder-a'):
            with self.subTest(host=host):
                code, gate, shell = self.run_repro(host + '\n')
                self.assertEqual(code, 0)
                gate.assert_called_once_with(Path.cwd(), False, None, ['fix', '', 'base', 'head'], False)
                shell.assert_not_called()

    def test_guest_repro_evidence_records_the_marker_host(self):
        for host in ('runner-a', 'ops-a', 'builder-b', 'builder-a'):
            with self.subTest(host=host), tempfile.TemporaryDirectory() as tmp:
                marker = Path(tmp) / 'host'
                marker.write_text(host + '\n')
                with patch.object(verify_context, 'COLDBOX_HOST_FILE', marker), \
                     patch.object(transport.platform, 'node', return_value='coldbox-job-7'), \
                     patch.object(transport.verify_repro.subprocess, 'run', wraps=subprocess.run) as calls:
                    self.assertEqual(transport.verify_repro.gate(Path(tmp), force=True), 2)
                evidence = json.loads((Path(tmp) / '.cold/verify-repro.json').read_text())
                self.assertEqual(evidence['host'], host)
                self.assertFalse([c for c in calls.call_args_list if c.args[0][0] == 'ssh'])

    def test_missing_marker_routes_to_a_coldbox_vm(self):
        code, gate, shell = self.run_repro(None)
        self.assertEqual(code, 2)
        gate.assert_not_called()
        shell.assert_called_once_with('coldbox')

    def test_marker_naming_another_host_routes_to_a_coldbox_vm(self):
        code, gate, shell = self.run_repro('control-a\n')
        self.assertEqual(code, 2)
        gate.assert_not_called()
        shell.assert_called_once_with('coldbox')

    def test_wsl_named_runner_01_without_a_marker_never_runs_the_repro_locally(self):
        code, gate, shell = self.run_repro(None, release='6.6-microsoft-standard-WSL2', node='runner-a')
        self.assertEqual(code, 2)
        gate.assert_not_called()
        shell.assert_called_once_with('coldbox')

    def test_guest_marker_does_not_lift_the_wsl_refusal(self):
        code, gate, _ = self.run_repro('runner-a\n', release='6.6-microsoft-standard-WSL2')
        self.assertEqual(code, 1)
        gate.assert_not_called()


class StaleTransfers(unittest.TestCase):
    def test_only_own_transfer_directories_older_than_a_day_are_removed(self):
        with tempfile.TemporaryDirectory() as folder:
            temp = Path(folder)
            old, fresh, other, kept = (temp / 'cold-repro-transfer-old', temp / 'cold-repro-transfer-fresh',
                                       temp / 'cold-verify-export-old', temp / 'kept')
            for directory in (old, fresh, other, kept):
                directory.mkdir()
            link = temp / 'cold-repro-transfer-link'
            link.symlink_to(kept)
            two_days_ago = time.time() - 2 * 86400
            for path in (old, other, kept):
                os.utime(path, (two_days_ago, two_days_ago))
            os.utime(link, (two_days_ago, two_days_ago), follow_symlinks=False)
            with patch.object(transport.tempfile, 'gettempdir', return_value=folder):
                transport.sweep_stale_transfers()
            self.assertEqual(sorted(path.name for path in temp.iterdir()),
                             ['cold-repro-transfer-fresh', 'cold-repro-transfer-link', 'cold-verify-export-old', 'kept'])


class RemoteStaging(unittest.TestCase):
    def setUp(self):
        host_marker = patch.object(verify_context, 'COLDBOX_HOST_FILE', Path('/nonexistent/coldbox/host'))
        host_marker.start()
        self.addCleanup(host_marker.stop)

    def test_staged_files_import_on_the_remote_host_without_the_rest_of_ci(self):
        copied = []
        real_copy = transport.shutil.copy2

        def record(source, destination):
            copied.append((Path(source), Path(destination).name))
            return real_copy(source, destination)

        with patch.object(transport, 'prepare', return_value={'details': ['fix', '', 'base', 'head'], 'checkout': 'head'}), \
             patch.object(transport.subprocess, 'check_output', return_value='linux\n'), \
             patch.object(transport.platform, 'node', return_value='control-a'), \
             patch.object(transport.subprocess, 'run'), \
             patch.object(transport.subprocess, 'Popen'), \
             patch.object(transport.shutil, 'rmtree'), \
             patch.object(transport.shutil, 'copy2', side_effect=record):
            transport.run(Path.cwd())
        self.assertTrue(copied)
        with tempfile.TemporaryDirectory() as remote:
            for source, name in copied:
                real_copy(source, Path(remote) / name)
            result = subprocess.run([sys.executable, '-I', '-c', 'import sys; sys.path.insert(0, "."); import verify_repro_transport'],
                                    cwd=remote, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
