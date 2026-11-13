import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
import platform
import shlex
from unittest.mock import patch
import unittest

ROOT = Path(__file__).resolve().parents[1]
SIGN = (f'import os, sys; sys.path.insert(0, {str(ROOT / "ci")!r}); import junit_unittest; '
        'junit_unittest.sign(sys.argv[1], os.environ["COLD_VERIFY_JUNIT_KEY"])')
sys.path.insert(0, str(ROOT / 'ci'))
import verify_trace
import verify_context
import importlib.util
spec = importlib.util.spec_from_file_location("trace_linux_export", ROOT / "ci/linux-verify.py")
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)

SCRIPT = '''
import sys
print("cold job workspace=x")
print("::group::python venv")
print("venv ready")
print("::endgroup::")
print("::group::pip install -r requirements.txt")
print("installing")
print("::endgroup::")
print("::skip::cargo test")
print("python tests passed: 3")
'''
FAILING = '''
import sys
print("::group::pip install -r requirements.txt")
print("installing")
print("::endgroup::")
print("test_one ... ok")
print("pass" + "word=hunter2hunter2")
print("Authorization: Bear" + "er abcdef0123456789")
print("clone https://user:" + "s3cretpw@localhost/repo.git")
print("token ghp_" + "A" * 36, file=sys.stderr)
print("assertion failed: expected 1")
print("FAILED (1): python tests")
sys.exit(1)
'''


class VerifyTrace(unittest.TestCase):
    def run_traced(self, script):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.run(['git', '-C', folder, 'init', '-q'], check=True)
            (root / '.fleet').mkdir()
            (root / '.fleet/verify.sh').write_text('echo recipe\n')
            code = subprocess.run(
                [sys.executable, str(ROOT / 'ci/verify_trace.py'), '--root', folder, '--', sys.executable, '-c', script],
                text=True, capture_output=True)
            trace = (root / '.cold/verify-trace.jsonl').read_text().splitlines()
            self.assertEqual((root / '.cold/.gitignore').read_text(), '*\n')
            status = subprocess.run(['git', '-C', folder, 'status', '--porcelain'], text=True, capture_output=True).stdout
            self.assertNotIn('.cold', status)
            return code, [json.loads(line) for line in trace]

    def test_run_with_a_skipped_step_records_every_step_and_is_incomplete(self):
        code, records = self.run_traced(SCRIPT)
        self.assertEqual(code.returncode, 2)
        self.assertIn('TESTS INCOMPLETE: not run fresh (skipped or cached): cargo test', code.stdout)
        self.assertIn('::group::python venv\nvenv ready\n', code.stdout)
        self.assertIn('python tests passed: 3', code.stdout)
        self.assertEqual([(r['step'], r['status']) for r in records],
                         [('python venv', 'pass'), ('pip install -r requirements.txt', 'pass'),
                          ('cargo test', 'skip'), ('verify', 'pass')])
        self.assertEqual({r['schema_version'] for r in records}, {3})
        self.assertEqual(len({r['run_id'] for r in records}), 1)
        for record in records:
            self.assertNotIn('failing_excerpt', record)
            self.assertIsInstance(record['duration_ms'], int)
            self.assertRegex(record['inputs_hash'], r'^[0-9a-f]{64}$')
            self.assertEqual(record['head'], None)


    def test_host_and_platform_describe_the_launcher(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, HOME=home, USERPROFILE=home, COLDBOX_GUEST='0'):
            _, rows = self.run_traced('print("ok")')
            self.assertEqual(rows[0]['host'], platform.node())
            self.assertEqual(rows[0]['platform'], platform.system().lower())
            self.assertEqual(rows[0]['concurrent_runs'], 1)
            self.assertIsNone(rows[0]['snapshot_tree'])
            with verify_context.concurrent_run() as active:
                self.assertEqual(active, 1)
                _, overlapping = self.run_traced('print("ok")')
                self.assertEqual(overlapping[0]['concurrent_runs'], 2)

    def test_snapshot_digest_binds_export_not_later_source_edits(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'source'
            root.mkdir()
            subprocess.run(['git', '-C', str(root), 'init', '-q'], check=True)
            (root / 'file').write_bytes(b'committed')
            subprocess.run(['git', '-C', str(root), 'add', 'file'], check=True)
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=t', '-c', 'user.email=t@localhost',
                            'commit', '-qm', 'initial'], check=True)
            (root / 'file').write_bytes(b'dirty\r\n')
            (root / 'untracked').write_bytes(b'also tested')
            destination = Path(folder) / 'export'
            script = ('import sys; sys.path.insert(0, ' + repr(str(ROOT / 'ci')) + '); '
                      'import importlib.util; '
                      'spec=importlib.util.spec_from_file_location("exporter", ' + repr(str(ROOT / 'ci/linux-verify.py')) + '); '
                      'm=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); '
                      'from pathlib import Path; m.snapshot(Path(' + repr(str(root)) + '), Path(' + repr(str(destination)) + ')); '
                      'Path(' + repr(str(root / 'file')) + ').write_bytes(b"changed after export")')
            self.assertEqual(verify_trace.run([sys.executable, '-c', script], root), 2)
            row = next(verify_trace.read_records(root / verify_trace.TRACE_PATH))
            self.assertEqual(row['snapshot_tree'], verify_context.snapshot_tree(destination))
            self.assertNotEqual(row['snapshot_tree'], verify_context.snapshot_tree(root))
            self.assertEqual((destination / 'file').read_bytes(), b'dirty\r\n')
            self.assertEqual((destination / 'untracked').read_bytes(), b'also tested')
            self.assertRegex(row['snapshot_tree'], r'^[0-9a-f]{64}$')

    def test_digest_preserves_bytes_modes_targets_and_ignores_git_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'file').write_bytes(b'a\r\n')
            if os.name != 'nt':
                (root / 'file').chmod(0o644)
            first = verify_context.snapshot_tree(root)
            if os.name != 'nt':
                self.assertEqual(first, 'da1c1a58373b6ed76913ac2395cd397e994511d0b4f01e6046568e5a3430139f')
            (root / '.git').write_text('gitdir: elsewhere')
            self.assertEqual(verify_context.snapshot_tree(root), first)
            (root / '.git').unlink()
            (root / '.git').mkdir()
            (root / '.git/index').write_bytes(b'arbitrary metadata')
            self.assertEqual(verify_context.snapshot_tree(root), first)
            (root / 'file').write_bytes(b'a\n')
            self.assertNotEqual(verify_context.snapshot_tree(root), first)
            if os.name != 'nt':
                before = verify_context.snapshot_tree(root)
                (root / 'file').chmod(0o755)
                self.assertNotEqual(verify_context.snapshot_tree(root), before)
                (root / 'link').symlink_to('missing-one')
                before = verify_context.snapshot_tree(root)
                (root / 'link').unlink()
                (root / 'link').symlink_to('missing-two')
                self.assertNotEqual(verify_context.snapshot_tree(root), before)

    def test_independent_exports_have_identical_digest(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source'
            source.mkdir()
            (source / 'file').write_bytes(b'untracked')
            exporter.snapshot(source, root / 'one')
            exporter.snapshot(source, root / 'two')
            self.assertEqual(verify_context.snapshot_tree(root / 'one'), verify_context.snapshot_tree(root / 'two'))

    def test_concurrency_counts_overlap_and_recovers_stale_files(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'dead.run').write_bytes(b'0')
            with verify_context.concurrent_run(folder) as first:
                self.assertEqual(first, 1)
                with verify_context.concurrent_run(folder) as second:
                    self.assertEqual(second, 2)
                with verify_context.concurrent_run(folder) as after:
                    self.assertEqual(after, 2)
            with verify_context.concurrent_run(folder) as last:
                self.assertEqual(last, 1)

    def test_history_appends_runs_and_reader_accepts_v1(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            history = root / verify_trace.HISTORY_PATH
            history.parent.mkdir()
            old = dict(schema_version=1, run_id='old', head=None, step='verify', command='test', status='pass', duration_ms=0, inputs_hash=None)
            history.write_text(json.dumps(old) + '\n')
            for _ in range(2):
                self.assertEqual(verify_trace.run([sys.executable, '-c', 'print("ok")'], root), 2)
            rows = list(verify_trace.read_records(history))
            self.assertEqual([r['schema_version'] for r in rows], [1, 3, 3])
            for name in ('snapshot_tree', 'host', 'platform', 'concurrent_runs', 'waiting_ms', 'setup_ms'):
                self.assertIsNone(rows[0][name])
            self.assertEqual(len({r['run_id'] for r in rows}), 3)
            self.assertEqual(list(verify_trace.read_records(root / verify_trace.TRACE_PATH)), rows[-1:])

    def test_failing_run_marks_the_failed_step_with_an_excerpt(self):
        code, records = self.run_traced(FAILING)
        self.assertEqual(code.returncode, 1)
        self.assertEqual([(r['step'], r['status']) for r in records],
                         [('pip install -r requirements.txt', 'pass'), ('python tests', 'fail')])
        excerpt = records[1]['failing_excerpt']
        self.assertIn('assertion failed: expected 1', excerpt)
        self.assertTrue(excerpt.endswith('FAILED (1): python tests'))

    def test_excerpt_redacts_secrets(self):
        _, records = self.run_traced(FAILING)
        excerpt = records[1]['failing_excerpt']
        for leaked in ('hunter2hunter2', 'abcdef0123456789', 's3cretpw', 'ghp_' + 'A' * 36):
            self.assertNotIn(leaked, excerpt)
        self.assertIn('pass' 'word=[REDACTED]', excerpt)
        self.assertIn('Authorization: Bearer [REDACTED]', excerpt)
        self.assertIn('https://[REDACTED]@localhost', excerpt)

    def test_redact_hides_secret_environment_values_and_private_keys(self):
        text = 'saw plainvalue123 in output\n-----BEGIN PRIVATE ' 'KEY-----\nMIIB\n-----END PRIVATE KEY-----\nok'
        redacted = verify_trace.redact(text, {'SECRET_THING': 'plainvalue123', 'PATH': '/usr/bin/output'})
        self.assertEqual(redacted, 'saw [REDACTED] in output\n[REDACTED]\nok')

    def test_stderr_diagnostics_stay_with_their_step(self):
        script = ('import sys\nsys.stdout.reconfigure(line_buffering=True)\nprint("::group::build")\nprint("compiler exploded", file=sys.stderr)\n'
                  'print("::endgroup::")\nprint("FAILED (2): build")\nsys.exit(2)')
        _, records = self.run_traced(script)
        self.assertEqual([(r['step'], r['status']) for r in records], [('build', 'fail')])
        self.assertIn('compiler exploded', records[0]['failing_excerpt'])

    def test_head_ignores_inherited_git_variables(self):
        with tempfile.TemporaryDirectory() as other, tempfile.TemporaryDirectory() as folder:
            for repo in (other, folder):
                subprocess.run(['git', '-C', repo, 'init', '-q'], check=True)
                subprocess.run(['git', '-C', repo, '-c', 'user.name=t', '-c', 'user.email=t@localhost',
                                'commit', '-q', '--allow-empty', '-m', 'x'], check=True)
            expected = subprocess.run(['git', '-C', folder, 'rev-parse', 'HEAD'], text=True,
                                      capture_output=True).stdout.strip()
            saved = os.environ.get('GIT_DIR')
            os.environ['GIT_DIR'] = str(Path(other, '.git'))
            try:
                self.assertEqual(verify_trace.head_sha(folder), expected)
            finally:
                os.environ.pop('GIT_DIR') if saved is None else os.environ.update(GIT_DIR=saved)

    def test_nested_groups_stay_inside_the_outer_step(self):
        script = ('print("::group::.fleet/verify.ps1")\nprint("::group::fixture one")\nprint("ok")\nprint("::endgroup::")\n'
                  'print("fixture two failed")\nprint("ran 11, failed 2")\nprint("::endgroup::")\n'
                  'print("FAILED (1): .fleet/verify.ps1")\nraise SystemExit(1)')
        _, records = self.run_traced(script)
        self.assertEqual([(r['step'], r['status']) for r in records], [('.fleet/verify.ps1', 'fail')])
        self.assertIn('fixture two failed', records[0]['failing_excerpt'])
        self.assertIn('ran 11, failed 2', records[0]['failing_excerpt'])

    def test_identity_is_captured_before_the_command_runs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.run(['git', '-C', folder, 'init', '-q'], check=True)
            (root / '.fleet').mkdir()
            (root / '.fleet/verify.sh').write_bytes(b'before\n')
            subprocess.run(['git', '-C', folder, '-c', 'user.name=t', '-c', 'user.email=t@localhost',
                            'commit', '-q', '--allow-empty', '-m', 'first'], check=True)
            first = subprocess.run(['git', '-C', folder, 'rev-parse', 'HEAD'], text=True, capture_output=True).stdout.strip()
            script = ('import pathlib, subprocess\n'
                      'pathlib.Path(".fleet/verify.sh").write_text("after\\n")\n'
                      'subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@localhost", '
                      '"commit", "-q", "--allow-empty", "-m", "second"], check=True)')
            subprocess.run([sys.executable, str(ROOT / 'ci/verify_trace.py'), '--root', folder, '--',
                            sys.executable, '-c', script], cwd=folder, check=False, capture_output=True)
            record = json.loads((root / '.cold/verify-trace.jsonl').read_text().splitlines()[0])
            self.assertEqual(record['head'], first)
            self.assertEqual(record['inputs_hash'], verify_trace.inputs_hash({'.fleet/verify.sh': b'before\n'}, 'verify'))

    def test_unmarked_failure_is_one_failed_verify_step(self):
        _, records = self.run_traced('print("boom")\nraise SystemExit(3)')
        self.assertEqual([(r['step'], r['status']) for r in records], [('verify', 'fail')])
        self.assertEqual(records[0]['failing_excerpt'], 'boom')


class VerdictRequiresProof(unittest.TestCase):
    def verify(self, script):
        with tempfile.TemporaryDirectory() as folder:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = verify_trace.run(['bash', '-c', script], Path(folder))
            return code, output.getvalue()

    def test_a_fresh_run_with_a_real_test_count_passes(self):
        with tempfile.TemporaryDirectory() as suite:
            (Path(suite) / 'test_ok.py').write_text('import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n')
            code, output = self.verify(f'{sys.executable} "$COLD_VERIFY_UNITTEST" discover -s {suite} -p "test_*.py"')
        self.assertEqual(code, 0, output)
        self.assertIn('TESTS PASS: 1 tests ran fresh and passed', output)

    def test_a_unittest_run_whose_only_subtest_skipped_is_incomplete(self):
        with tempfile.TemporaryDirectory() as suite:
            (Path(suite) / 'test_external.py').write_text(
                'import unittest\nclass External(unittest.TestCase):\n    def test_external(self):\n'
                '        with self.subTest(case=1):\n            self.skipTest("dependency unavailable")\n')
            code, output, saved = self.verify_saved(f'{sys.executable} "$COLD_VERIFY_UNITTEST" discover -s {suite}')
        self.assertIn('OK (skipped=1)', output)
        self.assertEqual(code, 2, output)
        self.assertIn('TESTS INCOMPLETE: the JUnit XML records no test that ran', output)
        self.assertEqual((saved['result'], saved['tests_ran'], saved['tests_source']), ('INCOMPLETE', 0, 'junit'))

    def verify_saved(self, script):
        with tempfile.TemporaryDirectory() as folder:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = verify_trace.run(['bash', '-c', script], Path(folder))
            return code, output.getvalue(), json.loads((Path(folder) / '.cold/verify-result.json').read_text())

    def test_an_echoed_pytest_summary_is_incomplete(self):
        code, output, saved = self.verify_saved('echo "==== 5 passed in 0.01s ===="')
        self.assertEqual(code, 2, output)
        self.assertIn('TESTS INCOMPLETE: 5 tests counted only from output text, which does not prove they ran; '
                      'write JUnit XML to $COLD_VERIFY_JUNIT_DIR', output)
        self.assertEqual((saved['tests_ran'], saved['tests_source']), (5, 'output'))

    def test_a_plain_unittest_run_without_junit_is_unbound_and_incomplete(self):
        with tempfile.TemporaryDirectory() as suite:
            (Path(suite) / 'test_ok.py').write_text('import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n')
            code, output, saved = self.verify_saved(f'{sys.executable} -m unittest discover -s {suite} -p "test_*.py"')
        self.assertEqual(code, 2, output)
        self.assertIn('TESTS INCOMPLETE: 1 tests counted only from output text', output)
        self.assertEqual((saved['result'], saved['tests_ran'], saved['tests_source']), ('INCOMPLETE', 1, 'output'))

    def test_passing_recipe_owns_failure_markers_outside_its_test_step(self):
        with tempfile.TemporaryDirectory() as suite:
            test = Path(suite) / 'test_fixture.py'
            test.write_text(
                'import unittest\nclass T(unittest.TestCase):\n'
                '    def test_fixture(self):\n'
                '        print("FAILED: python tests")\n'
                '        print("FAILED (1): tests")\n')
            command = shlex.quote(f'{sys.executable} "{ROOT / "ci/junit_unittest.py"}" discover -s {suite}')
            script = f'{sys.executable} {ROOT / "ci/verify_parallel.py"} --step tests {command}'
            code, output, saved = self.verify_saved(script)
        self.assertIn('FAILED: python tests', output)
        self.assertEqual(code, 0, output)
        self.assertEqual(saved['result'], 'PASS')
        self.assertEqual(saved['tests_ran'], 1)
        self.assertEqual([(s['step'], s['status']) for s in saved['steps']], [('tests', 'pass')])

    def test_nested_runs_inside_the_suite_do_not_add_to_the_junit_count(self):
        runner = ROOT / 'ci/junit_unittest.py'
        with tempfile.TemporaryDirectory() as suite:
            (Path(suite) / 'inner').mkdir()
            (Path(suite) / 'inner/test_inner.py').write_text(
                'import unittest\nclass I(unittest.TestCase):\n    def test_i(self):\n        pass\n')
            (Path(suite) / 'test_outer.py').write_text(f"""import subprocess, sys, unittest
class O(unittest.TestCase):
    def test_spawns_plain_unittest(self):
        subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', {str(Path(suite) / 'inner')!r}], check=True)
    def test_spawns_the_junit_runner(self):
        done = subprocess.run([sys.executable, {str(runner)!r}, 'discover', '-s', {str(Path(suite) / 'inner')!r}],
                              capture_output=True, text=True, check=True)
        print(done.stderr, flush=True)
""")
            code, output, saved = self.verify_saved(f'{sys.executable} "$COLD_VERIFY_UNITTEST" discover -s {suite} -p "test_outer.py"')
        self.assertGreaterEqual(output.count('Ran 1 test in'), 2, output)
        self.assertEqual(code, 0, output)
        self.assertIn('TESTS PASS: 2 tests ran fresh and passed', output)
        self.assertEqual((saved['tests_ran'], saved['tests_source']), (2, 'junit'))

    def junit(self, *documents, sign=True):
        sign_with = (f'{shlex.quote(sys.executable)} -c {shlex.quote(SIGN)} ' if sign else 'true ')
        writes = '; '.join(f"printf '%s' {shlex.quote(document)} > \"$COLD_VERIFY_JUNIT_DIR/r{index}.xml\"; "
                           f'{sign_with}"$COLD_VERIFY_JUNIT_DIR/r{index}.xml"'
                           for index, document in enumerate(documents))
        return self.verify_saved(writes + '; echo "Ran 99 tests in 0.1s"')

    def test_junit_xml_the_adapter_did_not_sign_never_counts(self):
        good = '<testsuite><testcase name="a"/></testsuite>'
        code, output, saved = self.junit(good, sign=False)
        self.assertEqual(code, 2, output)
        self.assertIn("TESTS INCOMPLETE: JUnit XML not written by coldverify's test adapter, so it does not prove a test ran: r0.xml", output)
        wrong_key = (f"printf '%s' {shlex.quote(good)} > \"$COLD_VERIFY_JUNIT_DIR/r.xml\"; "
                     f'COLD_VERIFY_JUNIT_KEY=guessed {shlex.quote(sys.executable)} -c {shlex.quote(SIGN)} "$COLD_VERIFY_JUNIT_DIR/r.xml"')
        code, output, saved = self.verify_saved(wrong_key)
        self.assertEqual(code, 2, output)
        self.assertIn("JUnit XML not written by coldverify's test adapter, so it does not prove a test ran: r.xml", output)
        self.assertEqual(saved['tests_ran'], 0)

    def test_junit_counts_every_testcase_across_files_minus_skips(self):
        code, output, saved = self.junit(
            '<testsuites><testsuite tests="9"><testcase name="a"/><testcase name="b"><skipped/></testcase></testsuite>'
            '<testsuite><testcase name="c"/></testsuite></testsuites>',
            '<testsuite><testcase name="d"/><testcase name="e"><system-out>Ran 1 test in 0.0s</system-out></testcase></testsuite>')
        self.assertEqual(code, 0, output)
        self.assertIn('TESTS PASS: 4 tests ran fresh and passed', output)
        self.assertEqual((saved['tests_ran'], saved['tests_source']), (4, 'junit'))

    def test_failures_or_errors_in_the_junit_xml_fail_even_when_the_command_exits_0(self):
        for kind in ('failure', 'error'):
            with self.subTest(kind=kind):
                code, output, saved = self.junit(f'<testsuite><testcase name="a"/><testcase name="b"><{kind} message="x"/></testcase></testsuite>')
                self.assertEqual(code, 1, output)
                self.assertEqual(saved['result'], 'FAIL')
                self.assertIn('TESTS FAIL: the JUnit XML records', output)

    def test_unreadable_junit_xml_never_passes(self):
        good = '<testsuite><testcase name="a"/></testsuite>'
        for bad in ('<testsuite><testcase name="a">', 'not xml at all', '<coverage><testcase name="a"/></coverage>',
                    '<?xml version="1.0" encoding="bogus"?><testsuite/>', '<?xml version="1.0" encoding="utf-32"?><testsuite/>'):
            with self.subTest(bad=bad):
                code, output, saved = self.junit(good, bad)
                self.assertEqual(code, 2, output)
                self.assertIn('TESTS INCOMPLETE: unreadable JUnit XML: r1.xml', output)

    def test_junit_xml_with_no_test_that_ran_is_incomplete(self):
        for document in ('<testsuite tests="5"/>', '<testsuites/>', '<testsuite><testcase name="a"><skipped/></testcase></testsuite>'):
            with self.subTest(document=document):
                code, output, saved = self.junit(document)
                self.assertEqual(code, 2, output)
                self.assertIn('TESTS INCOMPLETE: the JUnit XML records no test that ran', output)
                self.assertEqual((saved['tests_ran'], saved['tests_source']), (0, 'junit'))

    def test_a_failing_command_stays_fail_whatever_the_junit_xml_says(self):
        code, output, saved = self.verify_saved(
            """printf '<testsuite><testcase name="a"/></testsuite>' > "$COLD_VERIFY_JUNIT_DIR/r.xml"; exit 3""")
        self.assertEqual(code, 3, output)
        self.assertIn('TESTS FAIL: exit 3', output)

    def test_the_run_result_is_saved_for_the_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            with contextlib.redirect_stdout(io.StringIO()):
                code = verify_trace.run(['bash', '-c', 'echo "Ran 3 tests in 0.1s"; exit 7'], Path(folder))
            saved_path = Path(folder) / '.cold/verify-result.json'
            self.assertTrue(saved_path.is_file())
            saved = json.loads(saved_path.read_text())
        self.assertEqual(code, 7)
        self.assertEqual((saved['result'], saved['reason'], saved['exit_code'], saved['tests_ran']), ('FAIL', 'exit 7', 7, 3))
        self.assertEqual(saved['command'], ['bash', '-c', 'echo "Ran 3 tests in 0.1s"; exit 7'])
        self.assertEqual([(step['step'], step['status']) for step in saved['steps']], [('verify', 'fail')])

    def test_a_successful_command_that_ran_no_test_is_incomplete(self):
        for script in ('true', 'echo all good', 'echo "Ran 0 tests in 0.000s"'):
            with self.subTest(script=script):
                code, output = self.verify(script)
                self.assertEqual(code, 2)
                self.assertIn('TESTS INCOMPLETE: no test count found', output)

    def test_cached_or_skipped_steps_are_incomplete_even_with_tests(self):
        code, output = self.verify('echo "::skip::lint"; echo "Ran 4 tests in 0.1s"')
        self.assertEqual(code, 2)
        self.assertIn('TESTS INCOMPLETE: not run fresh (skipped or cached): lint', output)

    def test_a_partial_selection_is_incomplete(self):
        code, output = self.verify('echo "::group::tests (1 of 2)"; echo "Ran 1 test in 0.1s"; echo "::endgroup::"')
        self.assertEqual(code, 2)
        self.assertIn('only part of a suite was selected', output)

    def test_a_failure_is_fail_with_its_exit_code(self):
        code, output = self.verify('echo "Ran 3 tests in 0.1s"; exit 7')
        self.assertEqual(code, 7)
        self.assertIn('TESTS FAIL: exit 7', output)

    def test_counts_from_common_runners_are_recognized(self):
        lines = ['Ran 2 tests in 0.010s', 'GENERIC_EXECUTED_TESTS=3', '===== 1 failed, 4 passed in 0.50s =====',
                 'test result: ok. 5 passed; 0 failed', '# pass 6', 'Tests:       7 passed, 7 total']
        steps = [{'step': 'verify', 'status': 'pass', 'lines': lines}]
        self.assertEqual(verify_trace.tests_ran(steps), 27)

    def test_unittest_tests_that_were_all_skipped_are_not_counted(self):
        code, output = self.verify('echo "Ran 3 tests in 0.001s"; echo; echo "OK (skipped=3)"')
        self.assertEqual(code, 2)
        self.assertIn('no test count found', output)
        steps = [{'step': 'verify', 'status': 'pass', 'lines': ['Ran 3 tests in 0.1s', 'OK (skipped=1)']}]
        self.assertEqual(verify_trace.tests_ran(steps), 2)

    def test_reader_rejects_an_invented_status(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'trace.jsonl'
            path.write_text(json.dumps(dict(schema_version=3, status='mostly-pass')) + '\n')
            with self.assertRaisesRegex(ValueError, 'unknown verify trace status'):
                list(verify_trace.read_records(path))


class TracerTiming(unittest.TestCase):
    def trace(self, script, *, lock_wait=0.0):
        @contextmanager
        def slow_lock():
            time.sleep(lock_wait)
            yield 1
        with tempfile.TemporaryDirectory() as folder, patch.object(verify_trace, 'concurrent_run', slow_lock):
            root = Path(folder)
            subprocess.run(['git', '-C', folder, 'init', '-q'], check=True)
            with contextlib.redirect_stdout(io.StringIO()):
                verify_trace.run([sys.executable, '-u', '-c', script], root)
            return [json.loads(line) for line in (root / verify_trace.TRACE_PATH).read_text().splitlines()]

    def test_time_blocked_on_the_run_registry_is_waiting(self):
        row, = self.trace('print("ok")', lock_wait=0.3)
        self.assertGreaterEqual(row['waiting_ms'], 300)
        self.assertEqual(row['setup_ms'], 0)
        self.assertEqual(row['schema_version'], 3)
        self.assertLessEqual(row['waiting_ms'] + row['setup_ms'], row['duration_ms'])

    def test_time_until_the_snapshot_is_ready_is_setup_and_is_not_a_context_field(self):
        script = ('import json, os, time\n'
                  'time.sleep(0.3)\n'
                  'open(os.environ["COLD_VERIFY_SNAPSHOT_RECEIPT"], "w").write(json.dumps({"snapshot_tree": "ab", "snapshot_ready_at": time.time()}))\n'
                  'time.sleep(0.1)\n')
        entry_times = iter([time.monotonic()] * 3)
        with patch.object(verify_trace, 'time', wraps=time) as clock:
            clock.monotonic.side_effect = lambda: next(entry_times, time.monotonic())
            row, = self.trace(script)
        self.assertGreaterEqual(row['setup_ms'], 300)
        self.assertEqual(row['waiting_ms'], 0)
        self.assertGreaterEqual(row['duration_ms'] - row['setup_ms'], 100)
        self.assertEqual(row['snapshot_tree'], 'ab')
        self.assertNotIn('snapshot_ready_at', row)

    def test_the_run_records_only_a_valid_backend_from_the_snapshot_receipt(self):
        for backend, recorded in (('coldbox', 'coldbox'), ('ssh:builder-a', 'ssh:builder-a'), ('ssh:-oProxyCommand=x', None), (None, None)):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                subprocess.run(['git', '-C', folder, 'init', '-q'], check=True)
                script = ('import json, os\n'
                          f'open(os.environ["COLD_VERIFY_SNAPSHOT_RECEIPT"], "w").write(json.dumps({{"snapshot_tree": "ab", "backend": {backend!r}}}))\n')
                with contextlib.redirect_stdout(io.StringIO()):
                    verify_trace.run([sys.executable, '-c', script], root)
                run = json.loads((root / verify_trace.RESULT_PATH).read_text())
                self.assertEqual(run.get('backend'), recorded)
                row, = [json.loads(line) for line in (root / verify_trace.TRACE_PATH).read_text().splitlines()]
                self.assertNotIn('backend', row)

    def test_phases_belong_to_the_step_that_was_open_while_they_happened(self):
        script = ('import json, os, time\n'
                  'print("::group::export")\n'
                  'time.sleep(0.3)\n'
                  'open(os.environ["COLD_VERIFY_SNAPSHOT_RECEIPT"], "w").write(json.dumps({"snapshot_tree": "ab", "snapshot_ready_at": time.time()}))\n'
                  'print("::endgroup::")\n'
                  'print("::group::tests")\n'
                  'time.sleep(0.1)\n'
                  'print("::endgroup::")\n')
        export, tests = self.trace(script)
        self.assertGreaterEqual(export['setup_ms'], 250)
        self.assertEqual(tests['setup_ms'], 0)
        self.assertEqual(tests['waiting_ms'], 0)


if __name__ == '__main__':
    unittest.main()
