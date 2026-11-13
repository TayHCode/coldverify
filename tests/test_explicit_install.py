import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from test_verify_explicit import needs_bwrap
from test_verify_receipt import schema_errors

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ci'))
VENV = 'python3 -m venv --without-pip .venv'
PURELIB = '"$(.venv/bin/python -c \'import sysconfig; print(sysconfig.get_paths()["purelib"])\')"'
PROBE = '''import os, socket, sys
from pathlib import Path
port, service, targets = int(sys.argv[1]), sys.argv[2], sys.argv[3:]
try:
    socket.create_connection(('localhost', port), timeout=2).close()
    print('NET open')
except OSError:
    print('NET closed')
try:
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(service)
    print('UNIX open')
except OSError:
    print('UNIX closed')
sockets = 0
for top in ('/run', '/var'):
    for found in Path(top).rglob('*'):
        try:
            sockets += found.is_socket()
        except OSError:
            pass
print('HOST-SOCKETS', sockets)
print('ENV', 'seen' if 'PLANTED_SECRET' in os.environ else 'absent')
for target in targets:
    label, path = target.split('=', 1)
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'a') as handle:
            handle.write('tampered\\n')
        print('WROTE', label)
    except OSError:
        print('BLOCKED', label)
'''


def unittest_file(body):
    return 'import unittest\nfrom pathlib import Path\n\n\nclass Case(unittest.TestCase):\n    def test_case(self):\n' + body


class ExplicitInstall(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.repo = self.folder / 'repo'
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.env = {key: value for key, value in os.environ.items() if not key.startswith(('COLD_', 'GIT_', 'GITHUB_'))}
        self.env.update(COLDBOX_GUEST='0', COLD_VERIFY_BACKEND='local', PLANTED_SECRET='planted')
        listener = socket.create_server(('localhost', 0))
        self.addCleanup(listener.close)
        listener.listen(16)
        service = socket.socket(socket.AF_UNIX)
        self.addCleanup(service.close)
        service.bind(str(self.folder / 'service.sock'))
        service.listen(16)
        self.probe = f'python3 -c {shlex.quote(PROBE)} {listener.getsockname()[1]} {self.folder}/service.sock'

    def commit(self, message, files):
        for name, text in files.items():
            (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / name).write_text(text)
        subprocess.run(['git', '-C', str(self.repo), 'add', '-A'], check=True)
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
                        'commit', '-q', '-m', message], check=True)
        return subprocess.check_output(['git', '-C', str(self.repo), 'rev-parse', 'HEAD'], text=True).strip()

    def coldverify(self, command, install=None):
        extra = ['--install-command', install] if install is not None else []
        return subprocess.run([sys.executable, str(ROOT / 'coldverify'), '--claim', 'the fix works', '--base', self.base,
                               '--head', self.head, '--test-command', command, *extra],
                              cwd=self.repo, env=self.env, text=True, capture_output=True)

    def receipt(self, result):
        path = self.repo / '.cold/verify-receipt.json'
        self.assertTrue(path.is_file(), result.stdout + result.stderr)
        receipt = json.loads(path.read_text())
        self.assertEqual(schema_errors(receipt), [])
        return receipt

    def outputs(self, receipt):
        test = receipt['repro']['tests'][0]
        return test['base_output'] + '\n' + test['head_output']

    def probe_targets(self):
        tmp = Path('/tmp') / ('cv172-' + self.folder.name)
        self.addCleanup(tmp.unlink, missing_ok=True)
        return ' '.join([f'repo={self.repo}/value.py', f'git-config={self.repo}/.git/config',
                         f'git-hook={self.repo}/.git/hooks/post-checkout', f'objects={self.repo}/.git/objects/tamper',
                         'other-side=$OTHER/node_modules/dep/index.js', f'outside={self.folder}/outside',
                         f'tmp={tmp}'])

    def assert_caller_untouched(self, before):
        status = subprocess.check_output(['git', '-C', str(self.repo), 'status', '--porcelain', '--ignored'], text=True)
        self.assertEqual([line for line in status.split('\n') if line and not line.endswith(' .cold/')], [])
        self.assertEqual((self.repo / '.git/config').read_text(), before)
        for path in (self.repo / '.git/hooks/post-checkout', self.repo / '.git/objects/tamper', self.folder / 'outside'):
            self.assertFalse(path.exists(), path)

    @needs_bwrap
    def test_r1_each_side_uses_the_venv_node_modules_and_go_cache_its_own_install_built(self):
        test = unittest_file(
            "        import os\n        here = Path.cwd()\n"
            "        self.assertEqual((here / 'node_modules/dep/index.js').read_text(), (here / 'vendor/dep.js').read_text())\n"
            "        self.assertEqual(Path(os.environ['HOME'], 'go/pkg/mod/cache/marker').read_text(), str(here))\n"
            "        import value\n        self.assertEqual(value.value(), 2)\n")
        self.base = self.commit('base', {'value.py': 'def value():\n    return 1\n', 'vendor/dep.js': 'exports.side = "base"\n',
                                         'tests/__init__.py': ''})
        self.head = self.commit('fix', {'value.py': 'def value():\n    return 2\n', 'vendor/dep.js': 'exports.side = "head"\n',
                                        'tests/test_deps.py': test})
        install = (f'{VENV} && mkdir -p node_modules/dep "$HOME/go/pkg/mod/cache" && cp vendor/dep.js node_modules/dep/index.js'
                   ' && printf %s "$PWD" > "$HOME/go/pkg/mod/cache/marker"')
        receipt = self.receipt(self.coldverify('.venv/bin/python -m unittest tests.test_deps', install))
        self.assertEqual(receipt['checks']['repro'][0], 'PASS', receipt['checks']['repro'])
        self.assertIn('AssertionError: 1 != 2', receipt['repro']['tests'][0]['base_output'])
        self.assertFalse((self.repo / 'node_modules').exists() or (self.repo / '.venv').exists())

    @needs_bwrap
    def test_r2_r3_an_editable_install_makes_each_side_import_its_own_tree_for_every_layout(self):
        layouts = (('src', 'src/pkg/__init__.py'), ('lib', None), ('.', 'pkg/__init__.py'))
        for source, init in layouts:
            with self.subTest(layout=source):
                self.setUp()
                module = f'{source}/pkg/value.py'.removeprefix('./')
                files = {module: 'def value():\n    return 1\n', 'tests/__init__.py': ''}
                if init:
                    files[init] = ''
                self.base = self.commit('base', files)
                self.head = self.commit('fix', {module: 'def value():\n    return 2\n', 'tests/test_value.py': unittest_file(
                    "        import pkg.value as m\n"
                    "        self.assertTrue(Path(m.__file__).resolve().is_relative_to(Path.cwd().resolve()), m.__file__)\n"
                    "        self.assertEqual(m.value(), 2)\n")})
                install = f'{VENV} && printf "%s\\n" "$PWD/{source}" > {PURELIB}/_editable.pth'
                receipt = self.receipt(self.coldverify('.venv/bin/python -m unittest tests.test_value', install))
                self.assertEqual(receipt['checks']['repro'][0], 'PASS', receipt['repro']['tests'])
                self.assertIn('AssertionError: 1 != 2', receipt['repro']['tests'][0]['base_output'])

    @needs_bwrap
    def test_r4_the_test_gets_no_network_no_planted_variable_and_cannot_write_outside_its_side(self):
        self.base = self.commit('base', {'value.py': 'def value():\n    return 1\n', 'tests/__init__.py': ''})
        self.head = self.commit('fix', {'value.py': 'def value():\n    return 2\n', 'tests/test_value.py': unittest_file(
            "        import value\n        self.assertEqual(value.value(), 2)\n")})
        before = (self.repo / '.git/config').read_text()
        other = 'case "$PWD" in */base) OTHER=../head;; *) OTHER=../base;; esac; '
        result = self.coldverify(f'{other}{self.probe} {self.probe_targets()}; python3 -m unittest tests.test_value')
        receipt = self.receipt(result)
        output = self.outputs(receipt)
        self.assertEqual(output.count('NET closed'), 2, output)
        self.assertEqual(output.count('ENV absent'), 2, output)
        self.assertEqual(output.count('UNIX closed'), 2, output)
        self.assertEqual(output.count('HOST-SOCKETS 0'), 2, output)
        self.assertNotIn('WROTE', output)
        self.assertEqual(output.count('BLOCKED'), 14, output)
        self.assertEqual(receipt['checks']['repro'][0], 'PASS', output)
        self.assert_caller_untouched(before)

    @needs_bwrap
    def test_r4_the_install_gets_network_and_the_test_does_not(self):
        self.base = self.commit('base', {'value.py': 'def value():\n    return 1\n', 'tests/__init__.py': ''})
        self.head = self.commit('fix', {'value.py': 'def value():\n    return 2\n', 'tests/test_value.py': unittest_file(
            "        import value\n        self.assertEqual(Path('net.txt').read_text(), 'NET open\\n')\n"
            "        self.assertEqual(value.value(), 2)\n")})
        install = f'{self.probe} | grep NET > net.txt'
        receipt = self.receipt(self.coldverify(f'{self.probe}; python3 -m unittest tests.test_value', install))
        output = self.outputs(receipt)
        self.assertEqual(receipt['checks']['repro'][0], 'PASS', output)
        self.assertEqual(output.count('NET closed'), 2, output)

    @needs_bwrap
    def test_r5a_the_head_run_executes_the_artifact_head_built(self):
        self.base = self.commit('base', {'src/ext.c': 'broken\n', 'tests/__init__.py': ''})
        self.head = self.commit('fix', {'src/ext.c': 'fixed\n', 'tests/test_ext.py': unittest_file(
            "        self.assertEqual(Path('build/ext.so').read_text(), 'fixed\\n')\n")})
        receipt = self.receipt(self.coldverify('python3 -m unittest tests.test_ext', 'mkdir -p build && cp src/ext.c build/ext.so'))
        self.assertEqual(receipt['checks']['repro'][0], 'PASS', receipt['repro']['tests'])

    @needs_bwrap
    def test_r5b_a_base_test_that_rewrites_dependencies_cannot_make_a_bad_fix_pass_or_reach_anything_shared(self):
        self.base = self.commit('base', {'vendor/dep.js': 'exports.ok = false\n', 'value.py': 'OK = False\n', 'tests/__init__.py': ''})
        self.head = self.commit('bad fix', {'value.py': 'OK = False  # claims a fix\n', 'tests/test_dep.py': unittest_file(
            "        self.assertEqual(Path('node_modules/dep/index.js').read_text(), 'exports.ok = true\\n')\n")})
        before = (self.repo / '.git/config').read_text()
        install = 'mkdir -p node_modules/dep && cp vendor/dep.js node_modules/dep/index.js'
        lurker = ('(i=0; while [ $i -lt 300 ]; do for f in ../head/node_modules/dep/index.js; do '
                  '[ -f "$f" ] && echo "exports.ok = true" > "$f"; done; i=$((i + 1)); sleep 0.02; done) > /dev/null 2>&1 &')
        tamper = (f'if git rev-parse HEAD | grep -q {self.base}; then echo "exports.ok = true" > node_modules/dep/index.js; '
                  f'{lurker} OTHER=../head {self.probe} {self.probe_targets()} '
                  f'caller-deps={self.repo}/node_modules/dep/index.js; fi; ')
        receipt = self.receipt(self.coldverify(tamper + 'python3 -m unittest tests.test_dep', install))
        test = receipt['repro']['tests'][0]
        self.assertNotIn('WROTE', test['base_output'])
        self.assertIn('UNIX closed', test['base_output'])
        self.assertEqual(test['base_output'].count('BLOCKED'), 8, test['base_output'])
        self.assertEqual(test['head_exit'], 1, test['head_output'])
        self.assertEqual(receipt['checks']['repro'][0], 'FAIL', receipt['checks']['repro'])
        self.assert_caller_untouched(before)
        self.assertFalse((self.repo / 'node_modules').exists())

    @needs_bwrap
    def test_an_install_that_fails_on_either_side_is_incomplete_with_the_reason(self):
        self.base = self.commit('base', {'value.py': 'def value():\n    return 1\n', 'tests/__init__.py': ''})
        self.head = self.commit('fix', {'value.py': 'def value():\n    return 2\n', 'tests/test_value.py': unittest_file(
            "        import value\n        self.assertEqual(value.value(), 2)\n")})
        cases = (('base', 'exit 7', 'install on base failed with exit 7'),
                 ('head', f'git rev-parse HEAD | grep -q {self.head} && exit 5; true', 'install on head failed with exit 5'))
        for side, install, reason in cases:
            with self.subTest(side=side):
                result = self.coldverify('python3 -m unittest tests.test_value', install)
                receipt = self.receipt(result)
                self.assertEqual(receipt['repro']['tests'], [])
                self.assertEqual(receipt['checks']['repro'][0], 'INCOMPLETE')
                self.assertIn(reason, receipt['checks']['repro'][1])
                self.assertEqual(result.returncode, 2)

    @needs_bwrap
    def test_an_install_that_times_out_is_incomplete_and_names_the_limit(self):
        import verify_explicit
        import verify_repro
        self.base = self.commit('base', {'value.py': 'def value():\n    return 1\n', 'tests/__init__.py': ''})
        self.head = self.commit('fix', {'value.py': 'def value():\n    return 2\n', 'tests/test_value.py': 'raise SystemExit(0)\n'})
        (self.repo / '.cold').mkdir()
        with mock.patch.dict(os.environ, COLDBOX_GUEST='0', COLD_VERIFY_BACKEND='local'), \
                mock.patch.object(verify_repro, 'CASE_TIMEOUT', 1):
            code = verify_explicit.run(self.repo, self.base, self.head, 'true', 'sleep 5')
        repro = json.loads((self.repo / '.cold/verify-repro.json').read_text())
        self.assertEqual((code, repro['result'], repro['tests']), (2, 'INCOMPLETE', []))
        self.assertIn('install on base timed out after 1 s', repro['error'])

    @needs_bwrap
    def test_a_test_that_fails_on_both_sides_for_lack_of_network_says_so(self):
        self.base = self.commit('base', {'value.py': 'def value():\n    return 1\n', 'tests/__init__.py': ''})
        self.head = self.commit('fix', {'value.py': 'def value():\n    return 2\n', 'tests/test_value.py': 'x = 1\n'})
        result = self.coldverify('python3 -c "import socket; socket.getaddrinfo(\'example.com\', 443)" || exit 1')
        receipt = self.receipt(result)
        self.assertEqual(receipt['checks']['repro'][0], 'FAIL', receipt['checks']['repro'])
        self.assertIn('the test may need network rather than the fix being wrong', receipt['checks']['repro'][1])
        self.assertEqual(result.returncode, 1)

    def test_a_sandbox_that_fails_to_start_is_incomplete_not_a_failing_test(self):
        import verify_explicit
        self.base = self.commit('base', {'value.py': 'def value():\n    return 1\n', 'tests/__init__.py': ''})
        self.head = self.commit('fix', {'tests/test_value.py': 'x = 1\n'})
        (self.repo / '.cold').mkdir()
        refuses = ['sh', '-c', '[ "$1" = true ] && exit 0; echo "bwrap: setting up uid map: Permission denied"; exit 1', '--']
        with mock.patch.dict(os.environ, COLDBOX_GUEST='0', COLD_VERIFY_BACKEND='local'), \
                mock.patch.object(verify_explicit, 'sandbox', return_value=refuses):
            code = verify_explicit.run(self.repo, self.base, self.head, 'exit 0')
        repro = json.loads((self.repo / '.cold/verify-repro.json').read_text())
        self.assertEqual((code, repro['result'], repro['tests']), (2, 'INCOMPLETE', []))
        self.assertIn('the sandbox did not start (exit 1)', repro['error'])

    def test_no_bubblewrap_means_nothing_runs(self):
        import verify_explicit
        self.base = self.commit('base', {'value.py': 'def value():\n    return 1\n', 'tests/__init__.py': ''})
        self.head = self.commit('fix', {'tests/test_value.py': 'x = 1\n'})
        (self.repo / '.cold').mkdir()
        ran = self.folder / 'ran'
        with mock.patch.dict(os.environ, COLDBOX_GUEST='0', COLD_VERIFY_BACKEND='local'), \
                mock.patch.object(verify_explicit.shutil, 'which', return_value=None):
            code = verify_explicit.run(self.repo, self.base, self.head, f'touch {ran}', f'touch {ran}')
        repro = json.loads((self.repo / '.cold/verify-repro.json').read_text())
        self.assertEqual((code, repro['result']), (2, 'INCOMPLETE'))
        self.assertIn('bubblewrap (bwrap) is not installed', repro['error'])
        self.assertFalse(ran.exists())

    def test_install_command_needs_the_explicit_flags_and_text(self):
        self.base = self.commit('base', {'value.py': '', 'tests/__init__.py': ''})
        self.head = self.base
        tool = [sys.executable, str(ROOT / 'coldverify')]
        cases = ((['--install-command', 'true'], 'needs --base, --head and --test-command'),
                 (['--base', self.base, '--head', self.head, '--test-command', 'true', '--install-command', ' '], 'cannot be empty'))
        for args, message in cases:
            with self.subTest(args=args):
                result = subprocess.run([*tool, *args], cwd=self.repo, env=self.env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(message, result.stderr)


if __name__ == '__main__':
    unittest.main()
