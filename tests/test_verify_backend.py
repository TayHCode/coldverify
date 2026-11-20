import io
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import criteria_shim
from run_alone import run_alone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ci'))
import synthetic_policy
import verify_backend
import verify_context
import verify_criteria
import verify_receipt
import verify_trace

GIT_LOCATION = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR',
                'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES')
REFUSE = '#!/usr/bin/env bash\necho "$(basename "$0") $3" >> "$SHIM_CALLS"\nexit 255\n'
SSH_TO_FAKE_HOST = '#!/usr/bin/env bash\necho "ssh $3" >> "$SHIM_CALLS"\nPYTHONPATH="$FAKE_REMOTE" exec sh -c "$4"\n'
NO_REMOTE_GH ='#!/usr/bin/env bash\necho "no git remotes found" >&2\nexit 1\n'
FAKE_BACKEND = '''#!/usr/bin/env bash
echo "$(basename "$0") $1 $2" >> "$SHIM_CALLS"
run_remote() {
  local remote_prefix=/tmp/cold-
  local fixture_prefix="${SHIM_CALLS%/*}/cold-"
  exec "$1" "$2" "${3//"$remote_prefix"/"$fixture_prefix"}"
}
case "$1 $2" in
  "run -d"|"vm up")
    [ -n "$FAKE_BACKEND_DOWN" ] && { echo "backend is down" >&2; exit 125; }
    echo fake-box ;;
  "exec -i") shift 3; run_remote "$@" ;;
  "vm exec")
    shift 4
    [ -n "$FAKE_SUITE_STATUS" ] && [ "$3" != "uname -n" ] && exit "$FAKE_SUITE_STATUS"
    run_remote "$@" ;;
  "rm -f") ;;
  "vm down") if [ -n "$FAKE_VM_DOWN_SECONDS" ]; then sleep "$FAKE_VM_DOWN_SECONDS"; echo "$(basename "$0") deleted" >> "$SHIM_CALLS"; fi ;;
  "api "*) echo "coldbox: error: argument command: invalid choice: 'api' (choose from run, vm)" >&2; exit 2 ;;
  *) exit 64 ;;
esac
'''
COLDBOX_OVER_SSH = '''#!/usr/bin/env bash
shift 2
host=$1
shift
echo "$host $*" >> "$SHIM_CALLS"
case "$*" in
  "coldbox vm up")
    [ -n "$FAKE_SSH_DOWN" ] && { echo "ssh: connect to host $host port 22: Connection refused" >&2; exit 255; }
    echo cbx-test ;;
  "coldbox vm down cbx-test") [ -n "$FAKE_SSH_DOWN_REFUSED" ] && { echo "vm busy" >&2; exit 1; } ;;
  "coldbox vm exec cbx-test -- "*)
    [ -n "$FAKE_SSH_DOWN_AFTER_UP" ] && { echo "ssh: connect to host $host port 22: Connection refused" >&2; exit 255; }
    eval "set -- $*"; shift 5; exec "$@" ;;
  *) exit 64 ;;
esac
'''

SHA = 'a' * 40
COLDBOX_API = '''#!/usr/bin/env bash
echo "coldbox $1 $2" >> "$SHIM_CALLS"
case "$1 $2" in
  "api submit")
    [ "$*" = "api submit --repo me/app --sha $FAKE_SHA" ] || exit 2
    echo '{"id": "cbx-1-abcdef-app", "receipt": "/var/lib/coldbox/receipts/cbx-1-abcdef-app.json"}' ;;
  "api status")
    [ -n "$FAKE_STATUS_BYTES" ] && { printf "$FAKE_STATUS_BYTES"; exit 0; }
    read -r state rest < "$FAKE_API_STATES"
    [ -n "$rest" ] && echo "$rest" > "$FAKE_API_STATES"
    printf '{"id": "%s", "receipt": "r", "state": "%s"}\\n' "$3" "$state" ;;
  "api receipt")
    code=${FAKE_RECEIPT_EXIT:-0}
    case $code in
      0|1) verdict=pass; [ "$code" = 1 ] && verdict=fail
           printf '{"schema": "%s", "id": "%s", "verdict": "%s", "repo": "%s", "sha": "%s"}\\n' "${FAKE_RECEIPT_SCHEMA:-coldbox.receipt/v1}" "${FAKE_RECEIPT_ID:-$3}" \\
             "${FAKE_RECEIPT_VERDICT:-$verdict}" "${FAKE_RECEIPT_REPO:-me/app}" "${FAKE_RECEIPT_SHA:-$FAKE_SHA}" ;;
      125) printf '{"schema": "coldbox.receipt/v1", "id": "%s", "verdict": "incomplete", "repo": "me/app", "sha": "%s", "checks": [], "cause": "infra_error", "error": "fatal: could not read Username"}\\n' "$3" "$FAKE_SHA" ;;
      75) echo "coldbox: $3 is running; ask again later" >&2 ;;
    esac
    exit "$code" ;;
  *) exit 64 ;;
esac
'''

HOLDER = ('import os, sys, time\nsys.path.insert(0, sys.argv[1])\nimport verify_backend\n'
          'with verify_backend.shell("coldbox"):\n    print("up", os.getpid(), flush=True)\n    time.sleep(60)\n')

@unittest.skipUnless(os.name == 'posix', 'drives the native snapshot export')
class ConfiguredBackend(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        base = Path(folder.name)
        self.repo, self.calls = base / 'sample', base / 'shim-calls'
        shims, home = base / 'shims', base / 'home'
        for directory in (self.repo, shims, home):
            directory.mkdir()
        for name, script in (('ssh', REFUSE), ('gh', NO_REMOTE_GH), ('docker', FAKE_BACKEND), ('coldbox', FAKE_BACKEND)):
            (shims / name).write_text(script)
            (shims / name).chmod(0o755)
        self.env = {key: value for key, value in os.environ.items()
                    if key not in GIT_LOCATION and not key.startswith(('GITHUB_', 'COLD_VERIFY_')) and key != 'PYTHONPATH'}
        self.env.update(criteria_shim.install(shims, base / 'criteria-calls'))
        self.env.update(PATH=f'{shims}:{self.env["PATH"]}', HOME=str(home), SHIM_CALLS=str(self.calls), COLDBOX_GUEST='0',
                        GIT_AUTHOR_NAME='Outsider', GIT_AUTHOR_EMAIL='outsider@localhost',
                        GIT_COMMITTER_NAME='Outsider', GIT_COMMITTER_EMAIL='outsider@localhost')
        self.write('calc.py', 'def add(a, b):\n    return a - b\n')
        self.write('tests/test_other.py', 'import unittest\nclass Other(unittest.TestCase):\n'
                   '    def test_truth(self):\n        self.assertTrue(True)\n')
        self.write('.fleet/verify.sh', 'set -euo pipefail\npython3 "$COLD_VERIFY_UNITTEST" discover -s tests -v\n')
        self.git('init', '-q', '-b', 'main')
        self.git('add', '-A')
        self.git('commit', '-qm', 'base')
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.git('switch', '-qc', 'fix/add')
        self.write('calc.py', 'def add(a, b):\n    return a + b\n')
        self.write('tests/test_calc.py', 'import sys, unittest\nsys.path.insert(0, ".")\nimport calc\n'
                   'class Add(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(calc.add(2, 3), 5)\n')
        self.git('add', '-A')
        self.git('commit', '-qm', 'Fix add')

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def git(self, *args):
        subprocess.run(['git', '-C', str(self.repo), '-c', 'commit.gpgsign=false', *args], check=True, env=self.env)

    def verify(self, backend, *options, **extra):
        result = subprocess.run([sys.executable, str(ROOT / 'coldverify'), '--ci', '--builder-model', criteria_shim.BUILDER, '--claim', 'add adds', *options], cwd=self.repo,
                                env={**self.env, 'COLD_VERIFY_BACKEND': backend, **extra}, text=True, capture_output=True, timeout=300)
        receipt = self.repo / '.cold/verify-receipt.json'
        return result, json.loads(receipt.read_text()) if receipt.exists() else None

    def calls_made(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def assert_python_replay_incomplete(self, result, receipt):
        output = result.stdout + result.stderr
        self.assertEqual((result.returncode, receipt['result']), (2, 'INCOMPLETE'), output)
        self.assertEqual(receipt['checks']['repro'][0], 'INCOMPLETE')
        self.assertIn('python-coverage-not-runner-observed', receipt['checks']['repro'][1])
        self.assertEqual(receipt['checks']['tests'], ['PASS', '2 tests ran fresh and passed'])
        self.assertIn('tests/test_calc.py::Add.test_add: base=1 head=0 (unproven)', output)

    def test_container_backend_runs_the_repro_and_the_suite_in_the_container(self):
        result, receipt = self.verify('container:ubuntu:24.04')
        self.assert_python_replay_incomplete(result, receipt)
        self.assertEqual((receipt['environment']['backend'], receipt['environment']['isolation']),
                         ('container:ubuntu:24.04', 'container'))
        self.assertEqual(receipt['repro']['backend'], 'container:ubuntu:24.04')
        self.assertIn('tree', receipt['run'])
        self.assertEqual((receipt['run']['head'], receipt['run']['tree']), (receipt['commit'], receipt['tree']))
        calls = self.calls_made()
        self.assertEqual(calls.count('docker run -d'), 2, calls)
        self.assertEqual(calls.count('docker rm -f'), 2, calls)
        self.assertIn('docker exec -i', calls)
        self.assertFalse([call for call in calls if call.startswith('ssh')], calls)

    def test_coldbox_backend_runs_in_a_disposable_vm(self):
        del self.env['COLDBOX_GUEST']
        result, receipt = self.verify('coldbox')
        with patch.dict(os.environ):
            os.environ.pop('COLDBOX_GUEST', None)
            in_guest = verify_context.coldbox_guest()
        criteria = receipt['checks'].pop('criteria')
        self.assertEqual(criteria[0], 'INCOMPLETE')
        if in_guest:
            self.assertEqual(criteria[1], verify_criteria.GUEST)
        else:
            self.assertIn('acceptance criteria no proof test covers', criteria[1])
        self.assert_python_replay_incomplete(result, receipt)
        self.assertEqual({name: check[0] for name, check in receipt['checks'].items()},
                         {name: 'INCOMPLETE' if name == 'repro' else 'PASS' for name in receipt['checks']})
        self.assertEqual('criteria not evaluated in a CI guest' in receipt['reason'], bool(in_guest), receipt['reason'])
        self.assertEqual((receipt['environment']['backend'], receipt['environment']['isolation']), ('coldbox', 'coldbox-vm'))
        calls = self.calls_made()
        self.assertEqual(calls.count('coldbox vm up'), 2, calls)
        self.assertEqual(calls.count('coldbox vm down'), 2, calls)

    def test_coldbox_api_without_an_api_subcommand_runs_in_a_disposable_vm(self):
        result, receipt = self.verify('coldbox-api', GITHUB_REPOSITORY='example-org/native-tool')
        output = result.stdout + result.stderr
        self.assert_python_replay_incomplete(result, receipt)
        self.assertIn('the coldbox backend has no api subcommand; running in a disposable Coldbox VM instead', output)
        self.assertEqual((receipt['environment']['backend'], receipt['environment']['isolation']), ('coldbox-api', 'coldbox-vm'))
        calls = self.calls_made()
        self.assertEqual((calls.count('coldbox api submit'), calls.count('coldbox vm up'), calls.count('coldbox vm down')), (1, 2, 2), calls)

    def without_a_repro(self):
        self.write('notes.txt', 'uncommitted, so the repro stops before it picks a backend\n')

    def assert_suite_ran_on(self, receipt, result, backend, isolation):
        output = result.stdout + result.stderr
        self.assertEqual((receipt['environment']['backend'], receipt['environment']['isolation']), (backend, isolation), output)
        self.assertEqual(receipt['run']['backend'], backend, output)

    def test_the_default_coldbox_vm_is_named_with_and_without_a_repro(self):
        for repro in (True, False):
            with self.subTest(repro=repro):
                if not repro:
                    self.without_a_repro()
                self.calls.unlink(missing_ok=True)
                result, receipt = self.verify('', GITHUB_REPOSITORY='example-org/native-tool',
                                               PYTHONPATH=synthetic_policy.shim_folder(self.addCleanup))
                self.assertEqual(receipt['repro'].get('backend'), 'coldbox' if repro else None, receipt['repro'])
                self.assert_suite_ran_on(receipt, result, 'coldbox', 'coldbox-vm')
                self.assertIn('coldbox vm up', self.calls_made())

    def test_a_coldbox_vm_on_cold_verify_host_over_ssh_is_named_without_a_repro(self):
        shims = self.repo.parent / 'shims'
        (shims / 'coldbox').unlink()
        (shims / 'ssh').write_text(COLDBOX_OVER_SSH)
        path = ':'.join(folder for folder in self.env['PATH'].split(':') if not (Path(folder) / 'coldbox').exists())
        self.without_a_repro()
        result, receipt = self.verify('', PATH=path, COLD_VERIFY_HOST='build-host')
        self.assertIsNone(receipt['repro'].get('backend'), receipt['repro'])
        self.assert_suite_ran_on(receipt, result, 'coldbox', 'coldbox-vm')
        self.assertIn('build-host coldbox vm up', self.calls_made())

    def test_a_vm_whose_suite_never_gave_a_verdict_is_not_named_as_where_it_ran(self):
        self.without_a_repro()
        for status in ('1', '3'):
            with self.subTest(status=status):
                result, receipt = self.verify('', GITHUB_REPOSITORY='example-org/native-tool', FAKE_SUITE_STATUS=status,
                                               PYTHONPATH=synthetic_policy.shim_folder(self.addCleanup))
                self.assertIn(f'the coldbox backend exited {status} without a verdict', result.stdout + result.stderr)
                self.assertEqual(receipt['checks']['tests'], ['INCOMPLETE', 'infrastructure aborted during suite execution'])
                self.assertNotIn('backend', receipt['run'])
                self.assertIn('coldbox vm up', self.calls_made())

    def test_an_explicit_local_backend_is_named_without_a_repro(self):
        self.without_a_repro()
        result, receipt = self.verify('local', GITHUB_REPOSITORY='example-org/sample')
        self.assertIsNone(receipt['repro'].get('backend'), receipt['repro'])
        self.assert_suite_ran_on(receipt, result, 'local', 'local')
        self.assertEqual(self.calls_made(), [])

    def test_ssh_backend_names_its_host_and_an_unreachable_host_is_incomplete(self):
        result, receipt = self.verify('ssh:build-box')
        self.assertNotEqual(receipt['result'], 'PASS', result.stdout + result.stderr)
        self.assertEqual((receipt['environment']['backend'], receipt['environment']['isolation']), ('ssh:build-box', 'ssh'))
        self.assertEqual(receipt['repro']['result'], 'INCOMPLETE')
        self.assertIn('could not reach build-box', receipt['repro']['error'])
        calls = self.calls_made()
        self.assertTrue(calls, calls)
        self.assertEqual(set(calls), {'ssh build-box'}, calls)

    def test_a_backend_that_cannot_start_is_incomplete_and_runs_nothing_here(self):
        result, receipt = self.verify('container:ubuntu:24.04', FAKE_BACKEND_DOWN='1')
        self.assertNotEqual(receipt['result'], 'PASS', result.stdout + result.stderr)
        self.assertEqual(receipt['checks']['repro'][0], 'INCOMPLETE')
        self.assertIn('the container backend gave no answer: backend is down', receipt['repro']['error'])
        self.assertNotEqual(receipt['checks']['tests'][0], 'PASS')
        self.assertNotIn('docker exec -i', self.calls_made())

    def test_an_unknown_backend_is_incomplete(self):
        result, receipt = self.verify('kubernetes')
        self.assertNotEqual(receipt['result'], 'PASS', result.stdout + result.stderr)
        self.assertEqual(receipt['checks']['repro'][0], 'INCOMPLETE')
        self.assertIn('COLD_VERIFY_BACKEND must be local, ssh:<host>, container:<image> or coldbox', receipt['repro']['error'])
        self.assertNotEqual(receipt['checks']['tests'][0], 'PASS')
        self.assertEqual(self.calls_made(), [])

    def test_required_gate_distinguishes_infrastructure_aborts_from_product_failures(self):
        self.git('branch', '-m', 'change/add')
        for backend, recipe, status, reason in (
            ('kubernetes', 'exit 2\n', 'INCOMPLETE', 'suite setup'),
            ('local', 'kill -TERM "$$"\n', 'INCOMPLETE', 'suite execution'),
            ('container:ubuntu:24.04', 'kill -TERM "$$"\n', 'INCOMPLETE', 'suite execution'),
            ('local', 'echo "INCOMPLETE: suite setup"; exit 2\n', 'FAIL', 'exit 1'),
            ('container:ubuntu:24.04', 'exit 2\n', 'FAIL', 'exit 1'),
        ):
            with self.subTest(backend=backend, recipe=recipe):
                self.write('.fleet/verify.sh', recipe)
                result, receipt = self.verify(backend, GITHUB_ACTIONS='true')
                self.assertEqual(receipt['result'], status, result.stdout + result.stderr)
                self.assertEqual(receipt['checks']['tests'][0], status)
                self.assertIn(reason, receipt['checks']['tests'][1])
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(result.stdout.splitlines()[-1], 'COLDVERIFY CI FAIL: tests')

    def test_a_recorded_product_failure_survives_a_later_execution_abort(self):
        self.git('branch', '-m', 'change/add')
        self.write('tests/test_other.py', 'import unittest\nclass Other(unittest.TestCase):\n'
                   '    def test_truth(self):\n        self.assertEqual(1, 2)\n')
        self.write('.fleet/verify.sh', 'python3 "$COLD_VERIFY_UNITTEST" discover -s tests -v\nkill -TERM "$$"\n')
        for backend in ('local', 'container:ubuntu:24.04'):
            with self.subTest(backend=backend):
                result, receipt = self.verify(backend, GITHUB_ACTIONS='true')
                self.assertEqual(receipt['checks']['tests'], ['FAIL', 'the JUnit XML records 1 failures and 0 errors'])
                self.assertEqual(receipt['run'].get('infrastructure_phase'), 'execution')
                self.assertEqual(receipt['result'], 'FAIL')
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout.splitlines()[-1], 'COLDVERIFY CI FAIL: tests')

    def test_local_backend_runs_a_runner_repository_on_this_machine(self):
        result, receipt = self.verify('local', GITHUB_REPOSITORY='example-org/sample')
        self.assert_python_replay_incomplete(result, receipt)
        self.assertEqual((receipt['environment']['backend'], receipt['environment']['isolation']), ('local', 'local'))
        self.assertEqual(self.calls_made(), [])

    def test_local_backend_still_needs_native_linux_for_a_runner_repository(self):
        for name, fake in (('wsl', 'platform.release = lambda: "6.6.87-microsoft-standard-WSL2"'),
                           ('macos', 'platform.system = lambda: "Darwin"')):
            with self.subTest(name):
                folder = self.repo.parent / name
                folder.mkdir()
                (folder / 'sitecustomize.py').write_text('import platform\n' + fake + '\n' + synthetic_policy.SHIM)
                result, receipt = self.verify('local', GITHUB_REPOSITORY='example-org/sample', PYTHONPATH=str(folder))
                self.assertNotEqual(receipt['result'], 'PASS', result.stdout + result.stderr)
                self.assertIn('native repro needs native Linux, not WSL or another OS', receipt['repro']['error'])
                self.assertNotEqual(receipt['checks']['tests'][0], 'PASS')

    def test_a_remote_backend_still_needs_native_linux_for_the_suite(self):
        (self.repo.parent / 'shims' / 'ssh').write_text(SSH_TO_FAKE_HOST)
        for name, fake in (('wsl', 'platform.release = lambda: "6.6.87-microsoft-standard-WSL2"'),
                           ('macos', 'platform.system = lambda: "Darwin"')):
            with self.subTest(name):
                folder = self.repo.parent / ('remote-' + name)
                folder.mkdir()
                (folder / 'sitecustomize.py').write_text('import platform\n' + fake + '\n')
                result, receipt = self.verify('ssh:build-box', FAKE_REMOTE=str(folder))
                self.assertNotEqual(receipt['result'], 'PASS', result.stdout + result.stderr)
                self.assertNotEqual(receipt['checks']['tests'][0], 'PASS', result.stdout + result.stderr)
                self.assertIn('native verification needs native Linux, not WSL or another OS', result.stdout + result.stderr)


class ColdboxBackendHost(unittest.TestCase):
    def setUp(self):
        synthetic_policy.use(self.addCleanup)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        shims = Path(folder.name)
        self.calls = shims / 'calls'
        for name, script in (('ssh', COLDBOX_OVER_SSH), ('coldbox', FAKE_BACKEND)):
            (shims / name).write_text(script)
            (shims / name).chmod(0o755)
        environment = patch.dict(os.environ, PATH=f'{shims}:{os.environ["PATH"]}', SHIM_CALLS=str(self.calls))
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop('COLD_VERIFY_HOST', None)
        os.environ.pop('FAKE_SSH_DOWN', None)
        os.environ.pop('FAKE_SSH_DOWN_AFTER_UP', None)
        os.environ.pop('FAKE_SSH_DOWN_REFUSED', None)
        os.environ.pop('FAKE_VM_DOWN_SECONDS', None)
        self.coldbox_on_path = None

    def calls_made(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def run_in_vm(self, command):
        with patch.object(verify_backend.shutil, 'which', return_value=self.coldbox_on_path), \
                verify_backend.shell('coldbox') as (prefix, node):
            return node, subprocess.run([*prefix, command], text=True, capture_output=True)

    def test_without_local_coldbox_the_vm_runs_on_vm_01_and_is_deleted_after_a_failed_suite(self):
        node, result = self.run_in_vm('echo "suite ran in $0"; exit 3')
        self.assertEqual((node, result.returncode, result.stdout), (platform.node(), 3, 'suite ran in sh\n'))
        calls = self.calls_made()
        self.assertEqual(calls[0], 'builder-a coldbox vm up')
        self.assertTrue(all(call.startswith('builder-a coldbox vm ') for call in calls), calls)
        self.assertEqual(calls[-1], 'builder-a coldbox vm down cbx-test')

    def test_cold_verify_host_picks_the_coldbox_host(self):
        os.environ['COLD_VERIFY_HOST'] = 'builder-b'
        self.run_in_vm('true')
        calls = self.calls_made()
        self.assertEqual((calls[0], calls[-1]), ('builder-b coldbox vm up', 'builder-b coldbox vm down cbx-test'))

    def test_a_stopped_suite_still_deletes_the_vm(self):
        os.environ['FAKE_VM_DOWN_SECONDS'] = '1'
        for number in verify_backend.STOP_SIGNALS:
            with self.subTest(signal=number.name):
                self.calls.unlink(missing_ok=True)
                child = subprocess.Popen([sys.executable, '-c', HOLDER, str(ROOT / 'ci')], stdout=subprocess.PIPE, text=True)
                self.assertTrue(child.stdout.readline().startswith('up '))
                child.send_signal(number)
                self.assertEqual(child.wait(timeout=30), 128 + number)
                child.stdout.close()
                self.assertEqual(self.calls_made(), ['coldbox vm up', 'coldbox vm exec', 'coldbox vm down', 'coldbox deleted'])

    def stop_traced_run(self, stop):
        os.environ['FAKE_VM_DOWN_SECONDS'] = '1'
        with tempfile.TemporaryDirectory() as folder:
            subprocess.run(['git', '-C', folder, 'init', '-q'], check=True)
            subprocess.run(['git', '-C', folder, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
                            '-c', 'commit.gpgsign=false', 'commit', '-q', '--allow-empty', '-m', 'base'], check=True)
            tracer = ('import sys\nsys.path.insert(0, sys.argv[1])\nimport verify_trace\n'
                      'verify_trace.run([sys.executable, "-c", sys.argv[3], sys.argv[1]], sys.argv[2], strict=False)\n')
            for number in verify_backend.STOP_SIGNALS:
                with self.subTest(signal=number.name):
                    self.calls.unlink(missing_ok=True)
                    traced = subprocess.Popen([sys.executable, '-c', tracer, str(ROOT / 'ci'), folder, HOLDER],
                                              stdout=subprocess.PIPE, text=True, start_new_session=True)
                    suite = int(traced.stdout.readline().split()[1])
                    stop(traced, number)
                    self.assertEqual(traced.wait(timeout=30), 128 + number)
                    traced.stdout.close()
                    self.assertEqual(self.calls_made(), ['coldbox vm up', 'coldbox vm exec', 'coldbox vm down', 'coldbox deleted'])
                    self.assertFalse(Path(f'/proc/{suite}').exists())

    def test_signalling_the_traced_run_deletes_the_vm_its_suite_holds_and_leaves_no_child(self):
        self.stop_traced_run(lambda traced, number: traced.send_signal(number))

    def test_signalling_the_traced_run_s_process_group_again_during_teardown_still_deletes_the_vm(self):
        def stop_twice(traced, number):
            os.killpg(traced.pid, number)
            deadline = time.monotonic() + 10
            while 'coldbox vm down' not in self.calls_made() and time.monotonic() < deadline:
                time.sleep(0.05)
            os.killpg(traced.pid, number)
        self.stop_traced_run(stop_twice)

    def test_the_previous_signal_handlers_are_restored_after_the_vm_is_deleted(self):
        before = {number: signal.getsignal(number) for number in verify_backend.STOP_SIGNALS}
        with patch.object(verify_backend.shutil, 'which', return_value=None), verify_backend.shell('coldbox'):
            during = {number: signal.getsignal(number) for number in before}
        self.assertTrue(all(during[number] is not before[number] for number in before), during)
        self.assertEqual({number: signal.getsignal(number) for number in before}, before)
        self.assertEqual(self.calls_made()[-1], 'builder-a coldbox vm down cbx-test')

    def test_an_interrupted_run_still_deletes_the_vm(self):
        with self.assertRaises(KeyboardInterrupt), \
                patch.object(verify_backend.shutil, 'which', return_value=None), \
                verify_backend.shell('coldbox'):
            raise KeyboardInterrupt
        self.assertEqual(self.calls_made()[-1], 'builder-a coldbox vm down cbx-test')

    def test_an_unreachable_coldbox_host_is_unavailable_and_named(self):
        os.environ['FAKE_SSH_DOWN'] = '1'
        with self.assertRaises(verify_backend.Unavailable) as raised:
            self.run_in_vm('true')
        self.assertEqual(str(raised.exception), 'the coldbox on builder-a backend gave no answer: '
                                                'ssh: connect to host builder-a port 22: Connection refused')
        self.assertEqual(self.calls_made(), ['builder-a coldbox vm up'])

    def test_a_vm_that_cannot_be_reached_after_start_is_named_by_its_host_and_deleted(self):
        os.environ['FAKE_SSH_DOWN_AFTER_UP'] = '1'
        with self.assertRaises(verify_backend.Unavailable) as raised:
            self.run_in_vm('true')
        self.assertEqual(str(raised.exception), 'the coldbox on builder-a backend gave no answer: '
                                                'ssh: connect to host builder-a port 22: Connection refused')
        self.assertEqual(self.calls_made()[-1], 'builder-a coldbox vm down cbx-test')

    def test_a_vm_that_cannot_be_deleted_is_reported_with_its_host(self):
        os.environ['FAKE_SSH_DOWN_REFUSED'] = '1'
        with patch('sys.stderr', new_callable=io.StringIO) as stderr:
            self.run_in_vm('true')
        self.assertIn('warning: the coldbox on builder-a backend could not delete cbx-test: vm busy', stderr.getvalue())

    def test_a_cold_verify_host_that_is_not_a_host_name_is_refused(self):
        os.environ['COLD_VERIFY_HOST'] = '-oProxyCommand=touch /tmp/owned'
        with self.assertRaises(verify_backend.Unavailable):
            self.run_in_vm('true')
        self.assertEqual(self.calls_made(), [])

    def test_a_local_coldbox_is_used_without_ssh(self):
        self.coldbox_on_path = '/usr/local/bin/coldbox'
        _, result = self.run_in_vm('exit 4')
        self.assertEqual(result.returncode, 4)
        self.assertEqual(self.calls_made(), ['coldbox vm up', 'coldbox vm exec', 'coldbox vm exec', 'coldbox vm down'])


class ColdboxApi(unittest.TestCase):
    def setUp(self):
        self.assertTrue(callable(getattr(verify_backend, 'submit', None)), 'verify_backend has no coldbox api client')
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        shims = Path(folder.name)
        self.calls, self.states = shims / 'calls', shims / 'states'
        (shims / 'coldbox').write_text(COLDBOX_API)
        (shims / 'coldbox').chmod(0o755)
        environment = patch.dict(os.environ, PATH=f'{shims}:{os.environ["PATH"]}', SHIM_CALLS=str(self.calls),
                                 FAKE_API_STATES=str(self.states), FAKE_SHA=SHA)
        environment.start()
        self.addCleanup(environment.stop)
        for name in ('FAKE_RECEIPT_EXIT', 'FAKE_RECEIPT_SHA', 'FAKE_RECEIPT_REPO', 'FAKE_RECEIPT_SCHEMA', 'FAKE_RECEIPT_VERDICT', 'FAKE_RECEIPT_ID', 'FAKE_STATUS_BYTES'):
            os.environ.pop(name, None)
        poll = patch.object(verify_backend, 'POLL_SECONDS', 0)
        poll.start()
        self.addCleanup(poll.stop)

    def submit(self, states):
        self.states.write_text(states + '\n')
        with patch('sys.stderr', new_callable=io.StringIO) as stderr:
            code = verify_backend.submit('me/app', SHA)
        return code, stderr.getvalue()

    def calls_made(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def test_a_submission_is_polled_through_queued_and_running_to_its_receipt(self):
        code, progress = self.submit('queued queued running done')
        self.assertEqual(code, 0)
        self.assertEqual(self.calls_made(), ['coldbox api submit', *['coldbox api status'] * 4, 'coldbox api receipt'])
        self.assertEqual(progress.splitlines(), ['cbx-1-abcdef-app on the coldbox api is queued',
                                                 'cbx-1-abcdef-app on the coldbox api is running'])

    def test_a_failing_receipt_is_a_fail(self):
        os.environ['FAKE_RECEIPT_EXIT'] = '1'
        self.assertEqual(self.submit('running done')[0], 1)

    def test_a_failed_submission_is_unavailable_with_its_error(self):
        os.environ['FAKE_RECEIPT_EXIT'] = '125'
        with self.assertRaisesRegex(verify_backend.ExecutionAborted,
                                    r'has no pass or fail receipt: exit 125, infra_error: fatal: could not read Username$'):
            self.submit('queued failed')

    def test_an_incomplete_receipt_cannot_put_a_line_break_or_control_character_in_the_reason(self):
        receipt = json.dumps({'cause': 'infra_error\n::error::forged', 'error': 'fatal\x1b[2K\r\nnext'})
        reason = verify_backend.unfinished(subprocess.CompletedProcess([], 125, receipt, ''))
        self.assertEqual(reason, 'exit 125, infra_error ::error::forged: fatal [2K next')

    def test_a_receipt_that_is_not_ready_is_unavailable(self):
        os.environ['FAKE_RECEIPT_EXIT'] = '75'
        with self.assertRaisesRegex(verify_backend.Unavailable, 'ask again later'):
            self.submit('done')

    @run_alone
    def test_polling_stops_at_its_time_limit(self):
        with patch.object(verify_backend, 'POLL_LIMIT', 1), patch.object(verify_backend, 'POLL_SECONDS', 0.2), \
                self.assertRaisesRegex(verify_backend.Unavailable, 'the coldbox api did not finish within 1 s'):
            started = time.monotonic()
            self.submit('running')
        self.assertLess(time.monotonic() - started, 5)
        calls = self.calls_made()
        self.assertEqual(calls[0], 'coldbox api submit')
        self.assertNotIn('coldbox api receipt', calls)
        self.assertGreater(calls.count('coldbox api status'), 1, calls)

    def test_a_receipt_that_does_not_match_the_submission_is_unavailable(self):
        for name, value in (('FAKE_RECEIPT_SHA', 'b' * 40), ('FAKE_RECEIPT_REPO', 'me/other'),
                            ('FAKE_RECEIPT_SCHEMA', 'coldbox.receipt/v2'), ('FAKE_RECEIPT_VERDICT', 'incomplete'),
                            ('FAKE_RECEIPT_ID', 'cbx-0-000000-older')):
            with self.subTest(name), patch.dict(os.environ, {name: value}), \
                    self.assertRaisesRegex(verify_backend.Unavailable, 'not for cbx-1-abcdef-app of me/app at ' + SHA):
                self.submit('done')

    def test_linux_verify_submits_a_clean_head_and_refuses_a_dirty_checkout(self):
        repo = Path(os.environ['FAKE_API_STATES']).parent / 'repo'
        (repo / '.fleet').mkdir(parents=True)
        (repo / '.fleet/verify.sh').write_text('true\n')
        env = {key: value for key, value in os.environ.items() if key not in GIT_LOCATION and not key.startswith('COLD_VERIFY_')}
        env.update(COLD_VERIFY_BACKEND='coldbox-api', GITHUB_REPOSITORY='me/app')
        for command in (['init', '-q'], ['add', '-A'], ['-c', 'user.name=t', '-c', 'user.email=t@localhost',
                                                          '-c', 'commit.gpgsign=false', 'commit', '-qm', 'base']):
            subprocess.run(['git', '-C', str(repo), *command], check=True, env=env)
        env['FAKE_SHA'] = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True, env=env).strip()
        self.states.write_text('running done\n')
        verify = [sys.executable, str(ROOT / 'ci/linux-verify.py'), '--generic', str(repo)]
        passed = subprocess.run(verify, text=True, capture_output=True, env=env)
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
        self.assertIn(f"PASS: coldbox checks on me/app at {env['FAKE_SHA']} through the coldbox api", passed.stdout)
        (repo / 'notes.txt').write_text('uncommitted\n')
        dirty = subprocess.run(verify, text=True, capture_output=True, env=env)
        self.assertEqual(dirty.returncode, 2, dirty.stdout + dirty.stderr)
        self.assertIn('INCOMPLETE: the coldbox-api backend runs only a clean checkout', dirty.stderr)
        self.assertEqual(self.calls_made().count('coldbox api submit'), 1)

    def test_a_status_that_is_not_one_json_object_is_unavailable(self):
        for name, output in (('invalid utf-8', '{"state": "done\\303'), ('truncated', '{"state": "do'),
                             ('duplicated', '{"state": "done"}\\n{"state": "done"}\\n'), ('not an object', '["done"]')):
            with self.subTest(name), patch.dict(os.environ, FAKE_STATUS_BYTES=output), \
                    self.assertRaisesRegex(verify_backend.Unavailable, 'the coldbox api gave no answer'):
                self.submit('done')

    def test_an_unknown_state_is_unavailable(self):
        with self.assertRaisesRegex(verify_backend.Unavailable, "unknown state 'lost'"):
            self.submit('lost')


class CachePerBackend(unittest.TestCase):
    def cache_dir(self, backend=None):
        env = {'COLD_VERIFY_BACKEND': backend} if backend else {}
        return verify_trace.cache_environment(env, Path('/repo'), None, False, False)['COLD_VERIFY_CACHE_DIR']

    def test_each_backend_keeps_its_own_step_cache(self):
        folders = [self.cache_dir(backend) for backend in ('local', 'container:ubuntu:24.04', 'ssh:build-box', 'coldbox')]
        self.assertEqual(len(set(folders)), 4, folders)
        self.assertNotIn(self.cache_dir(), folders)
        self.assertEqual(self.cache_dir(), '/repo/.cold/verify-cache')


class ReceiptEnvironment(unittest.TestCase):
    def test_older_evidence_without_a_backend_still_names_one(self):
        with patch.object(verify_receipt, 'coldbox_guest', return_value=None):
            self.assertEqual(verify_receipt.environment({'host': 'mac-elsewhere'})['backend'], 'ssh:mac-elsewhere')
            self.assertEqual(verify_receipt.environment(None)['backend'], 'local')
            self.assertEqual(verify_receipt.environment({'host': platform.node(), 'backend': 'container:img'})['isolation'], 'container')

    def test_the_backend_the_suite_ran_on_wins_over_the_repro_and_the_configuration(self):
        with patch.object(verify_receipt, 'coldbox_guest', return_value=None), \
                patch.dict(os.environ, {'COLD_VERIFY_BACKEND': 'local'}):
            found = verify_receipt.environment({'host': platform.node(), 'backend': 'local'}, {'backend': 'coldbox'})
        self.assertEqual((found['backend'], found['isolation']), ('coldbox', 'coldbox-vm'))

    def test_a_repro_that_stopped_before_routing_still_names_the_configured_backend(self):
        with patch.object(verify_receipt, 'coldbox_guest', return_value=None), \
                patch.dict(os.environ, {'COLD_VERIFY_BACKEND': 'container:ubuntu:24.04'}):
            found = verify_receipt.environment({'result': 'INCOMPLETE', 'error': 'base commit missing'})
        self.assertEqual((found['backend'], found['isolation']), ('container:ubuntu:24.04', 'container'))


if __name__ == '__main__':
    unittest.main()
