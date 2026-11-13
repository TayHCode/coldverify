import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ci'))
import synthetic_policy
import verify_context
from run_alone import run_alone

spec = importlib.util.spec_from_file_location('linux_verify', ROOT / 'ci/linux-verify.py')
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


@unittest.skipUnless(os.name == 'posix', 'native Linux boundary')
class RealLinuxVerification(unittest.TestCase):
    def setUp(self):
        synthetic_policy.use(self.addCleanup)
        for isolation in (patch.object(verify_context, 'COLDBOX_HOST_FILE', Path('/nonexistent/coldbox/host')),
                          patch.dict(os.environ, COLDBOX_GUEST='0')):
            isolation.start()
            self.addCleanup(isolation.stop)

    def test_standard_github_remote_forms_resolve_native_platform(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            subprocess.run(['git', 'init', '-q', folder], check=True)
            subprocess.run(['git', '-C', folder, 'remote', 'add', 'origin', 'unused'], check=True)
            for prefix in ('https://github.com/', 'git@github.com:', 'ssh://git@github.com/', 'ssh://git@github.com:22/'):
                with self.subTest(prefix=prefix):
                    subprocess.run(['git', '-C', folder, 'remote', 'set-url', 'origin', prefix + 'example-org/native-tool.git'], check=True)
                    self.assertEqual(verify.repository(Path(folder)), 'example-org/native-tool')

    @run_alone
    def test_recipe_output_streams_while_the_recipe_runs(self):
        script = "import sys,time; print('first', flush=True); time.sleep(2); print('second', flush=True)"
        with tempfile.TemporaryDirectory() as folder:
            runner = f"import runpy,sys; m=runpy.run_path({str(ROOT / 'ci/linux-verify.py')!r}, run_name='lv'); m['command']([sys.executable,'-c',{script!r}], {folder!r}, None)"
            process = subprocess.Popen([sys.executable, '-c', runner], cwd=ROOT / 'ci', stdout=subprocess.PIPE, text=True)
            started = time.monotonic()
            self.assertEqual(process.stdout.readline(), 'first\n')
            self.assertLess(time.monotonic() - started, 1.5)
            process.communicate()

    def test_custom_suite_runs_and_failure_propagates_without_credentials(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(verify, 'native_host', return_value='control-a'):
            root = Path(folder)
            subprocess.run(['git', 'init', '-q', folder], check=True)
            (root / '.fleet').mkdir()
            (root / '.gitignore').write_text('node_modules/\n')
            (root / 'node_modules').mkdir()
            (root / 'node_modules/local-edit').write_text('preserve')
            script = root / '.fleet/verify.sh'
            script.write_text('set -eu\ntest -z "${GH_TOKEN:-}"\ntest -z "${GIT_INDEX_FILE:-}"\ntest "$HOME" != "' + str(Path.home()) + '"\ntest ! -d node_modules\nprintf ran > observed\n')
            with patch.dict(os.environ, GH_TOKEN='never-pass-to-suite', GIT_INDEX_FILE=str(root / '.git/index')):
                verify.verify(root)
            self.assertFalse((root / 'observed').exists())
            self.assertEqual((root / 'node_modules/local-edit').read_text(), 'preserve')
            script.write_text('exit 7\n')
            with self.assertRaises(subprocess.CalledProcessError) as error:
                verify.verify(root)
            self.assertEqual(error.exception.returncode, 7)

    def test_child_python_can_generate_bytecode_for_contract_tests(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(verify, 'native_host', return_value='control-a'):
            root = Path(folder)
            subprocess.run(['git', 'init', '-q', folder], check=True)
            (root / '.fleet').mkdir()
            (root / 'fixture_module.py').write_text('answer = 42\n')
            (root / '.fleet/verify.sh').write_text('python3 -c \'import fixture_module, glob; count = len(glob.glob("__pycache__/fixture_module*.pyc")); print("bytecode-count=" + str(count)); assert count == 1\'\n')
            observed = io.StringIO()
            with redirect_stdout(observed):
                verify.verify(root)
            self.assertIn('bytecode-count=1', observed.getvalue())
            self.assertFalse((root / '__pycache__').exists())

    def test_repository_without_suite_cannot_pass(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(verify, 'native_host', return_value='control-a'):
            subprocess.run(['git', 'init', '-q', folder], check=True)
            with self.assertRaisesRegex(RuntimeError, 'real verification recipe'):
                verify.verify(Path(folder))

    def test_unapproved_host_cannot_run(self):
        with patch.object(verify.platform, 'system', return_value='Linux'), \
             patch.object(verify.platform, 'node', return_value='unapproved'), \
             self.assertRaisesRegex(RuntimeError, 'approved native Linux'):
            verify.native_host()

    def test_wsl_cannot_run_the_suite_even_with_the_runner_hostname(self):
        with patch.object(verify.platform, 'system', return_value='Linux'), \
             patch.object(verify.platform, 'node', return_value='runner-a'), \
             patch.object(verify.platform, 'release', return_value='6.6.87-microsoft-standard-WSL2'), \
             self.assertRaisesRegex(RuntimeError, 'approved native Linux'):
            verify.native_host()

    def test_approved_linux_hosts_can_run(self):
        for host in ('runner-a',):
            with self.subTest(host=host), patch.object(verify.platform, 'system', return_value='Linux'), patch.object(verify.platform, 'node', return_value=host):
                self.assertEqual(verify.native_host(), host)

    def test_uncommitted_deletion_is_absent_from_the_export(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.run(['git', 'init', '-q', folder], check=True)
            (root / 'deleted.py').write_text('obsolete = True\n')
            subprocess.run(['git', '-C', folder, 'add', 'deleted.py'], check=True)
            (root / 'deleted.py').unlink()
            (root / 'new.py').write_text('replacement = True\n')
            exported = root / 'export'
            verify.snapshot(root, exported)
            self.assertFalse((exported / 'deleted.py').exists())
            self.assertEqual((exported / 'new.py').read_text(), 'replacement = True\n')

    @run_alone
    def test_dispatched_verifies_run_at_once_in_their_own_directories(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / 'sitecustomize.py').write_text("import os, platform\nplatform.node = lambda: os.environ.get('FIXTURE_NODE', 'fixture-controller')\n"
                                                  + synthetic_policy.SHIM)
            shim = ('#!/usr/bin/env bash\nexport FIXTURE_NODE=runner-a\nexport PYTHONPATH="{}"\nwhile [[ $1 == -* ]]; do shift 2; done\nshift\n'
                    '[[ $1 == *mkdir* ]] && {{ printf %s "$1" > "{}/command.$$"; bash -c "${{1//COLD_VERIFY_HOST=runner-a/COLD_VERIFY_HOST={}}}" < <(tee "{}/wire.$$"); exit $?; }}\n'
                    'exec bash -c "${{1//COLD_VERIFY_HOST=runner-a/COLD_VERIFY_HOST={}}}"\n').format(base, base, 'runner-a', base, 'runner-a')
            (base / 'bin').mkdir()
            (base / 'bin/ssh').write_text(shim)
            (base / 'bin/ssh').chmod(0o755)
            roots = []
            for index in range(6):
                root = base / f'repo{index}'
                subprocess.run(['git', 'init', '-q', str(root)], check=True)
                (root / '.fleet').mkdir()
                (root / '.fleet/verify.sh').write_text(f'sleep 2\necho "ran-{index} $PWD" >> "{base}/seen"\n')
                roots.append(root)
            env = {**os.environ, 'GITHUB_REPOSITORY': 'example-org/native-tool', 'PATH': f'{base / "bin"}:{os.environ["PATH"]}',
                   'COLD_VERIFY_BACKEND': 'ssh:runner-a', 'PYTHONPATH': str(base)}
            started = time.monotonic()
            processes = [subprocess.Popen([sys.executable, str(ROOT / 'ci/linux-verify.py'), str(root)], env=env,
                                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) for root in roots]
            outputs = [process.communicate()[0] for process in processes]
            elapsed = time.monotonic() - started
            self.assertEqual([process.returncode for process in processes], [0] * 6, outputs)
            self.assertLess(elapsed, 9, 'six two-second verifies ran one after another')
            seen = (base / 'seen').read_text().splitlines()
            self.assertEqual(sorted(line.split()[0] for line in seen), [f'ran-{i}' for i in range(6)])
            self.assertEqual(len({line.split()[1] for line in seen}), 6)
            commands = list(base.glob('command.*'))
            self.assertEqual(len(commands), 6)
            self.assertTrue(all('COLD_VERIFY_HOST=runner-a' in command.read_text() for command in commands),
                            'the remote verify was not told which host it is')
            wires = list(base.glob('wire.*'))
            self.assertEqual(len(wires), 6)
            self.assertTrue(all(wire.read_bytes()[:2] == b'\x1f\x8b' for wire in wires), 'checkout was sent uncompressed')

    def test_dispatch_transports_change_manifest_and_verifier_context(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / 'bin').mkdir()
            (base / 'sitecustomize.py').write_text("import os, platform\nplatform.node = lambda: os.environ.get('FIXTURE_NODE', 'fixture-controller')\n"
                                                  + synthetic_policy.SHIM)
            ssh = base / 'bin/ssh'
            ssh.write_text(f'''#!/usr/bin/env bash
export FIXTURE_NODE=runner-a
export PYTHONPATH="{base}"
while [[ $1 == -* ]]; do shift 2; done
shift
exec bash -c "${{1//COLD_VERIFY_HOST=runner-a/COLD_VERIFY_HOST=runner-a}}"
''')
            ssh.chmod(0o755)
            root = base / 'repo'
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / '.fleet').mkdir()
            (root / '.fleet/verify.sh').write_text('''set -eu
test "$(cat "$COLD_VERIFY_CHANGES")" = 'manifest content'
test -f "$COLD_VERIFY_FILTER"
test -f "$COLD_VERIFY_PARALLEL"
test -f "$COLD_VERIFY_CACHE"
test "$COLD_VERIFY_HEAD" = abc123
test "$COLD_VERIFY_TREE" = def456
test "$COLD_VERIFY_GATE" = 1
test "$COLD_VERIFY_NO_CACHE" = 1
test "$COLD_VERIFY_UNTRUSTED" = 1
test -f "$COLD_VERIFY_UNITTEST"
case "$COLD_VERIFY_JUNIT_DIR" in /tmp/cold-verify-*/junit) ;; *) exit 9 ;; esac
printf '<testsuite><testcase name="remote"/></testsuite>' > "$COLD_VERIFY_JUNIT_DIR/remote.xml"
test "$COLD_VERIFY_JUNIT_KEY" = per-run-key
printf signature > "$COLD_VERIFY_JUNIT_DIR/remote.xml.sig"
echo context-received
''')
            changes = base / 'changes.json'
            changes.write_text('manifest content')
            env = {**os.environ, 'GITHUB_REPOSITORY': 'example-org/native-tool',
                   'PATH': f'{base / "bin"}:{os.environ["PATH"]}', 'COLD_VERIFY_BACKEND': 'ssh:runner-a', 'PYTHONPATH': str(base),
                   'COLD_VERIFY_CHANGES': str(changes), 'COLD_VERIFY_FILTER': '/local/filter.py',
                   'COLD_VERIFY_PARALLEL': '/local/parallel.py', 'COLD_VERIFY_CACHE': '/local/cache.py',
                   'COLD_VERIFY_CACHE_DIR': str(base / 'cache'), 'COLD_VERIFY_HEAD': 'abc123',
                   'COLD_VERIFY_TREE': 'def456', 'COLD_VERIFY_GATE': '1', 'COLD_VERIFY_NO_CACHE': '1',
                   'COLD_VERIFY_UNTRUSTED': '1', 'COLD_VERIFY_UNITTEST': '/local/junit_unittest.py',
                   'COLD_VERIFY_JUNIT_DIR': str(base / 'junit'), 'COLD_VERIFY_JUNIT_KEY': 'per-run-key'}
            result = subprocess.run([sys.executable, str(ROOT / 'ci/linux-verify.py'), str(root)],
                                    env=env, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('context-received', result.stdout)
            self.assertEqual((base / 'junit/remote.xml').read_text(), '<testsuite><testcase name="remote"/></testsuite>')
            self.assertEqual((base / 'junit/remote.xml.sig').read_text(), 'signature')

    def test_dispatched_step_cache_survives_between_runs(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / 'bin').mkdir()
            (base / 'sitecustomize.py').write_text("import os, platform\nplatform.node = lambda: os.environ.get('FIXTURE_NODE', 'fixture-controller')\n"
                                                  + synthetic_policy.SHIM)
            ssh = base / 'bin/ssh'
            ssh.write_text(f'''#!/usr/bin/env bash
export FIXTURE_NODE=runner-a
export PYTHONPATH="{base}"
while [[ $1 == -* ]]; do shift 2; done
shift
exec bash -c "${{1//COLD_VERIFY_HOST=runner-a/COLD_VERIFY_HOST=runner-a}}"
''')
            ssh.chmod(0o755)
            root = base / 'repo'
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / '.fleet').mkdir()
            (root / '.fleet/verify.sh').write_text(
                f'''python3 "$COLD_VERIFY_CACHE" step sample -- bash -c 'echo ran >> "{base}/seen"'\n''')
            subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
                            'commit', '-qm', 'fixture'], check=True)
            head = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
            env = {**os.environ, 'GITHUB_REPOSITORY': 'example-org/native-tool',
                   'PATH': f'{base / "bin"}:{os.environ["PATH"]}', 'COLD_VERIFY_BACKEND': 'ssh:runner-a', 'PYTHONPATH': str(base),
                   'COLD_VERIFY_CACHE': '/local/cache.py', 'COLD_VERIFY_CACHE_DIR': str(base / 'cache'),
                   'COLD_VERIFY_HEAD': head}
            env.pop('COLD_VERIFY_GATE', None)
            env.pop('COLD_VERIFY_NO_CACHE', None)
            for _ in range(2):
                result = subprocess.run([sys.executable, str(ROOT / 'ci/linux-verify.py'), str(root)],
                                        env=env, capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((base / 'seen').read_text(), 'ran\n')
            self.assertIn('::skip::sample', result.stdout)
            self.assertTrue(list((base / 'cache').glob('*.json')))
            result = subprocess.run([sys.executable, str(ROOT / 'ci/linux-verify.py'), str(root)],
                                    env={**env, 'COLD_VERIFY_NO_CACHE': '1'}, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((base / 'seen').read_text(), 'ran\nran\n')

    def test_dropped_connection_is_retried_and_a_failed_verify_is_not(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / 'bin').mkdir()
            (base / 'sitecustomize.py').write_text("import os, platform\nplatform.node = lambda: os.environ.get('FIXTURE_NODE', 'fixture-controller')\n"
                                                  + synthetic_policy.SHIM)
            (base / 'badbin').mkdir()
            (base / 'badbin/tar').write_text('#!/bin/sh\ncat >/dev/null\nexit 1\n')
            (base / 'badbin/tar').chmod(0o755)
            shim = ('#!/usr/bin/env bash\nexport FIXTURE_NODE=runner-a\nexport PYTHONPATH="{}"\nwhile [[ $1 == -* ]]; do shift 2; done\nshift\n'
                    'if [[ $1 == *mkdir* ]]; then\n  echo x >> "{}/tries"\n'
                    '  [ $(wc -l < "{}/tries") -le $DROPS ] && {{ cat >/dev/null; exit 255; }}\n'
                    '  [ -n "$BAD_TAR" ] && export PATH="{}/badbin:$PATH"\nfi\n'
                    'exec bash -c "${{1//COLD_VERIFY_HOST=runner-a/COLD_VERIFY_HOST={}}}"\n').format(base, base, base, base, 'runner-a')
            (base / 'bin/ssh').write_text(shim)
            (base / 'bin/ssh').chmod(0o755)
            root = base / 'repo'
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / '.fleet').mkdir()
            (root / '.fleet/verify.sh').write_text(f'echo ran-once >> "{base}/ran"\n[ -z "$FAIL_VERIFY" ]\n')
            env = {**os.environ, 'GITHUB_REPOSITORY': 'example-org/native-tool', 'PATH': f'{base / "bin"}:{os.environ["PATH"]}',
                   'COLD_VERIFY_BACKEND': 'ssh:runner-a', 'PYTHONPATH': str(base)}

            def run(**extra):
                (base / 'tries').unlink(missing_ok=True)
                (base / 'ran').unlink(missing_ok=True)
                return subprocess.run([sys.executable, str(ROOT / 'ci/linux-verify.py'), str(root)],
                                      env={**env, 'DROPS': '0', **extra}, capture_output=True, text=True, check=False)

            dropped = run(DROPS='2')
            self.assertEqual(dropped.returncode, 0, dropped.stdout + dropped.stderr)
            self.assertEqual(len((base / 'tries').read_text().splitlines()), 3)
            self.assertEqual((base / 'ran').read_text(), 'ran-once\n')
            unstageable = run(BAD_TAR='1')
            self.assertEqual(unstageable.returncode, 2, unstageable.stdout + unstageable.stderr)
            self.assertEqual(len((base / 'tries').read_text().splitlines()), 1)
            self.assertFalse((base / 'ran').exists())
            failing = run(FAIL_VERIFY='1')
            self.assertEqual(failing.returncode, 1, failing.stdout + failing.stderr)
            self.assertEqual(len((base / 'tries').read_text().splitlines()), 1)

    def test_export_keeps_the_real_commit_and_applies_working_changes_in_its_own_git_index(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'source'
            subprocess.run(['git', 'init', '-q', str(source)], check=True)
            (source / 'file.txt').write_text('committed\n')
            subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(source), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
                            '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture'], check=True)
            head = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
            (source / 'file.txt').write_text('working change\n')
            exported = Path(folder) / 'export'
            verify.snapshot(source, exported)
            self.assertEqual(subprocess.check_output(['git', '-C', str(exported), 'rev-parse', 'HEAD'], text=True).strip(), head)
            self.assertEqual((exported / 'file.txt').read_text(), 'working change\n')
            subprocess.run(['git', '-C', str(exported), 'add', '.'], check=True)
            self.assertEqual(subprocess.check_output(['git', '-C', str(source), 'diff', '--cached'], text=True), '')

    def test_external_symlink_is_rejected_before_suite_can_change_user_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source'
            subprocess.run(['git', 'init', '-q', str(source)], check=True)
            owned = root / 'user-file'
            owned.write_text('preserve')
            (source / 'outside').symlink_to(owned)
            with self.assertRaisesRegex(ValueError, 'symlink escapes'):
                verify.snapshot(source, root / 'export')
            self.assertEqual(owned.read_text(), 'preserve')

    def test_absolute_internal_symlink_is_redirected_into_the_export(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source'
            subprocess.run(['git', 'init', '-q', str(source)], check=True)
            original = source / 'original'
            original.write_text('preserve')
            (source / 'link').symlink_to(original)
            exported = root / 'export'
            verify.snapshot(source, exported)
            (exported / 'link').write_text('isolated write')
            self.assertEqual(original.read_text(), 'preserve')
            self.assertEqual((exported / 'original').read_text(), 'isolated write')

    def test_missing_recipe_cannot_silently_pass_a_mixed_pytest_and_unittest_suite(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(verify, 'native_host', return_value='control-a'):
            root = Path(folder)
            subprocess.run(['git', 'init', '-q', folder], check=True)
            (root / 'conftest.py').write_text('# project fixtures\n')
            (root / 'test_hybrid.py').write_text('import unittest\nclass Smoke(unittest.TestCase):\n def test_smoke(self): pass\ndef test_function(): assert False\n')
            with self.assertRaisesRegex(RuntimeError, 'real verification recipe'):
                verify.verify(root)

    def test_snapshot_preserves_staged_changes_with_a_callers_index_override(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'source'
            subprocess.run(['git', 'init', '-q', str(source)], check=True)
            file = source / 'file.txt'
            file.write_text('committed\n')
            subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(source), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
                            '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture'], check=True)
            file.write_text('staged change\n')
            subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
            before = subprocess.check_output(['git', '-C', str(source), 'diff', '--cached'], text=True)
            exported = Path(folder) / 'export'
            with patch.dict(os.environ, GIT_INDEX_FILE=str(source / '.git/index')):
                verify.snapshot(source, exported)
            self.assertEqual((exported / 'file.txt').read_text(), 'staged change\n')
            self.assertEqual(subprocess.check_output(['git', '-C', str(source), 'diff', '--cached'], text=True), before)


@unittest.skipUnless(os.name == 'posix', 'native Linux boundary')
class ColdboxGuestVerification(unittest.TestCase):
    def setUp(self):
        synthetic_policy.use(self.addCleanup)
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        base = Path(self.folder.name)
        self.root = base / 'repo'
        self.root.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        (self.root / '.fleet').mkdir()
        (self.root / '.fleet/verify.sh').write_text('echo guest-recipe-ran\n')
        self.marker = base / 'host'

    def run_main(self, marker_text):
        if marker_text is not None:
            self.marker.write_text(marker_text)
        output = io.StringIO()
        with patch.object(verify_context, 'COLDBOX_HOST_FILE', self.marker), \
             patch.object(verify.platform, 'node', return_value='coldbox-job-7'), \
             patch.object(verify.platform, 'system', return_value='Linux'), \
             patch.object(verify.platform, 'release', return_value='6.8.0-generic'), \
             patch.dict(os.environ, {'GITHUB_REPOSITORY': 'example-org/homelab'}), \
             patch.object(sys, 'argv', ['linux-verify.py', str(self.root)]), \
             patch.object(verify, 'dispatch', return_value=0) as dispatch, \
             redirect_stdout(output):
            code = verify.main()
        return code, output.getvalue(), dispatch

    def test_guest_marker_runs_the_recipe_locally_without_ssh(self):
        code, output, dispatch = self.run_main('runner-a\n')
        self.assertEqual(code, 0)
        self.assertIn('guest-recipe-ran', output)
        dispatch.assert_not_called()

    def test_every_listed_host_runs_locally_and_reports_itself(self):
        self.assertEqual(verify.verify_policy.verification_policy()['linux_hosts'], ['runner-a', 'ops-a', 'builder-b', 'builder-a'])
        for host in ('runner-a', 'ops-a', 'builder-b', 'builder-a'):
            with self.subTest(host=host):
                code, output, dispatch = self.run_main(host + '\n')
                self.assertEqual(code, 0)
                self.assertIn('guest-recipe-ran', output)
                self.assertIn(f'PASS: real Linux verification on {host}\n', output)
                self.assertEqual(output.count('runner-a'), host == 'runner-a')
                dispatch.assert_not_called()

    def test_missing_marker_dispatches_to_a_coldbox_vm(self):
        code, output, dispatch = self.run_main(None)
        self.assertEqual(code, 0)
        self.assertNotIn('guest-recipe-ran', output)
        dispatch.assert_called_once()
        self.assertEqual(dispatch.call_args.args[1], 'coldbox')

    def test_marker_naming_another_host_still_dispatches(self):
        for marker in ('control-a\n', 'windows-a\n', 'build-host\n'):
            with self.subTest(marker=marker):
                _, output, dispatch = self.run_main(marker)
                self.assertNotIn('guest-recipe-ran', output)
                dispatch.assert_called_once()
                self.assertEqual(dispatch.call_args.args[1], 'coldbox')

    def test_guest_marker_does_not_lift_the_wsl_refusal(self):
        self.marker.write_text('runner-a\n')
        with patch.object(verify_context, 'COLDBOX_HOST_FILE', self.marker), \
             patch.object(verify.platform, 'system', return_value='Linux'), \
             patch.object(verify.platform, 'release', return_value='6.6.87-microsoft-standard-WSL2'), \
             self.assertRaisesRegex(RuntimeError, 'approved native Linux'):
            verify.native_host()

    def test_guest_without_marker_is_not_an_approved_host(self):
        with patch.object(verify_context, 'COLDBOX_HOST_FILE', self.marker), \
             patch.object(verify.platform, 'system', return_value='Linux'), \
             patch.object(verify.platform, 'node', return_value='coldbox-job-7'), \
             patch.object(verify.platform, 'release', return_value='6.8.0-generic'), \
             self.assertRaisesRegex(RuntimeError, 'approved native Linux'):
            verify.native_host()
