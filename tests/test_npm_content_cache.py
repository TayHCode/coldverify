import base64
from contextlib import contextmanager
import hashlib
import http.server
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ci'))
import npm_content_cache as cache
import verify_context
import verify_policy

spec = importlib.util.spec_from_file_location('npm_linux_verify', ROOT / 'ci/linux-verify.py')
linux = importlib.util.module_from_spec(spec)
spec.loader.exec_module(linux)


def object_path(home, data):
    digest = hashlib.sha512(data).hexdigest()
    return home / '.npm/_cacache/content-v2/sha512' / digest[:2] / digest[2:4] / digest[4:]


def put(home, data):
    path = object_path(home, data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@contextmanager
def job_context(home):
    marker = home / 'guest-host'
    marker.write_text('fixture-host')
    store = cache.content_store

    def private_store(path, create=False):
        return store(home if path == '/home/runner' else path, create)

    with patch.dict(os.environ, HOME='/home/runner', GITHUB_ACTIONS='true',
                    GITHUB_JOB='fixture', COLDBOX_GUEST='1'), \
            patch.object(verify_context, 'COLDBOX_HOST_FILE', marker), \
            patch.object(verify_policy, 'verification_policy', return_value={'linux_hosts': ['fixture-host']}), \
            patch.object(cache, 'content_store', side_effect=private_store):
        yield


class ContentTransfer(unittest.TestCase):
    def test_disabled_cache_preserves_recipe_os_errors(self):
        for home in (None, '', 'relative'):
            with self.subTest(home=home), self.assertRaisesRegex(FileNotFoundError, 'recipe executable'):
                with cache.content_store(home):
                    raise FileNotFoundError('recipe executable')

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.source, self.target = self.root / 'source', self.root / 'target'
        self.source.mkdir()
        self.target.mkdir()

    def transfer(self):
        with cache.content_store(self.source) as src, cache.content_store(self.target, True) as dst:
            cache.copy_content(src, dst)

    def test_nonlinux_import_and_no_cache_for_invalid_home(self):
        program = """import os, runpy, sys
for name in ('O_DIRECTORY', 'O_NOFOLLOW'):
    if hasattr(os, name):
        delattr(os, name)
sys.path.insert(0, 'ci')
runpy.run_path('ci/linux-verify.py', run_name='import_probe')
import npm_content_cache
sys.platform = 'win32'
with npm_content_cache.content_store('/unused', True) as store:
    assert store is None
print('cross-platform import: OK')
"""
        result = subprocess.run([sys.executable, '-c', program], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for home in ('', 'relative'):
            with cache.content_store(home, True) as store:
                self.assertIsNone(store)

    def test_only_valid_content_and_no_shared_inode(self):
        original = put(self.source, b'package')
        corrupt = put(self.source, b'corrupt')
        corrupt.write_bytes(b'wrong digest')
        for name in ('.npmrc', '.npm/_logs/private', '.npm/_cacache/index-v5/private'):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('private')
        self.transfer()
        restored = object_path(self.target, b'package')
        self.assertEqual(restored.read_bytes(), b'package')
        self.assertNotEqual(original.stat().st_ino, restored.stat().st_ino)
        self.assertEqual(restored.stat().st_nlink, 1)
        self.assertEqual([p for p in self.target.rglob('*') if p.is_file()], [restored])

    def test_existing_object_and_hardlink_are_untouched(self):
        put(self.source, b'package')
        target = put(self.target, b'package')
        target.write_bytes(b'existing')
        outside = self.root / 'outside'
        os.link(target, outside)
        self.transfer()
        self.assertEqual(outside.read_bytes(), b'existing')
        self.assertEqual(target.stat().st_ino, outside.stat().st_ino)

    def test_source_symlink_fifo_and_hardlink_cannot_be_read(self):
        for kind in ('symlink', 'fifo', 'hardlink'):
            with self.subTest(kind=kind):
                path = put(self.source, kind.encode())
                path.unlink()
                outside = self.root / kind
                outside.write_bytes(kind.encode())
                if kind == 'symlink':
                    path.symlink_to(outside)
                elif kind == 'fifo':
                    os.mkfifo(path)
                else:
                    os.link(outside, path)
                self.transfer()
                self.assertFalse(object_path(self.target, kind.encode()).exists())
                self.assertEqual(outside.read_bytes(), kind.encode())

    def test_symlink_ancestors_in_both_directions(self):
        put(self.source, b'package')
        outside = self.root / 'outside'
        outside.mkdir()
        (self.target / '.npm').symlink_to(outside, target_is_directory=True)
        self.transfer()
        self.assertEqual(list(outside.iterdir()), [])
        alias = self.root / 'alias'
        alias.symlink_to(self.source, target_is_directory=True)
        with cache.content_store(alias) as descriptor:
            self.assertIsNone(descriptor)

    def test_destination_swap_and_entry_symlink_cannot_modify_outside(self):
        put(self.source, b'package')
        outside = self.root / 'outside'
        outside.mkdir()
        with cache.content_store(self.source) as src, cache.content_store(self.target, True) as dst:
            store = self.target / '.npm/_cacache/content-v2'
            store.rename(store.with_name('held'))
            store.symlink_to(outside, target_is_directory=True)
            cache.copy_content(src, dst)
        self.assertEqual(list(outside.iterdir()), [])
        victim = outside / 'victim'
        victim.write_bytes(b'untouched')
        target = object_path(self.target, b'other')
        target.parent.mkdir(parents=True)
        target.symlink_to(victim)
        put(self.source, b'other')
        self.transfer()
        self.assertEqual(victim.read_bytes(), b'untouched')

    def test_publication_uses_unnamed_validated_file_descriptor(self):
        put(self.source, b'package')
        outside = self.root / 'outside'
        outside.write_bytes(b'outside')
        real_link = os.link
        seen = []

        def publish(source, destination, **kwargs):
            self.assertTrue(source.startswith('/proc/self/fd/'))
            self.assertEqual(Path(source).read_bytes(), b'package')
            metadata = Path(source).stat()
            self.assertEqual(metadata.st_nlink, 0)
            seen.append(source)
            return real_link(source, destination, **kwargs)

        with patch.object(cache.os, 'link', side_effect=publish):
            self.transfer()
        self.assertEqual(len(seen), 1)
        result = object_path(self.target, b'package')
        self.assertFalse(result.is_symlink())
        self.assertEqual(result.read_bytes(), b'package')
        self.assertEqual(result.stat().st_nlink, 1)
        self.assertEqual(outside.read_bytes(), b'outside')

    def test_source_directory_swap_cannot_read_outside(self):
        original = put(self.source, b'package')
        outside = self.root / 'outside'
        outside.mkdir()
        victim = outside / original.name
        victim.write_bytes(b'package')
        real_open = os.open

        def swap(name, flags, *args, **kwargs):
            if name == original.parent.name and flags == cache.directory_flags():
                original.parent.rename(original.parent.with_name('held'))
                original.parent.symlink_to(outside, target_is_directory=True)
            return real_open(name, flags, *args, **kwargs)

        with patch.object(cache.os, 'open', side_effect=swap):
            self.transfer()
        self.assertFalse(object_path(self.target, b'package').exists())
        self.assertEqual(victim.read_bytes(), b'package')

    def test_native_or_dispatched_account_never_opens_cache(self):
        repo = self.root / 'repo'
        (repo / '.fleet').mkdir(parents=True)
        (repo / '.fleet/verify.py').write_text("import os; assert os.environ['npm_config_cache'] == 'unchanged'\n")
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        for guest, actions, home, job in ((None, 'true', '/home/runner', 'job'),
                                          ('fixture-host', 'false', '/home/runner', 'job'),
                                          ('fixture-host', 'true', str(self.source), 'job'),
                                          ('fixture-host', 'true', '/home/runner', '')):
            with self.subTest(guest=guest, actions=actions, home=home, job=job), \
                    patch.object(linux, 'coldbox_guest', return_value=guest), \
                    patch.dict(os.environ, HOME=home, GITHUB_ACTIONS=actions, GITHUB_JOB=job,
                               RUNNER_TEMP=str(self.root), npm_config_cache='unchanged'), \
                    patch.object(cache, 'copy_content', wraps=cache.copy_content) as transfer:
                linux.verify(repo, local=True)
                self.assertTrue(all(call.args[:2] == (None, None) for call in transfer.call_args_list))
        self.assertEqual(list(self.source.iterdir()), [])

    def test_recipe_mutation_is_private_and_existing_content_is_preserved(self):
        original = put(self.source, b'package')
        repo = self.root / 'repo'
        (repo / '.fleet').mkdir(parents=True)
        relative = original.relative_to(self.source)
        (repo / '.fleet/verify.py').write_text(f"""import os, pathlib
private = pathlib.Path(os.environ['HOME']) / {str(relative)!r}
private.write_bytes(b'child mutation')
assert pathlib.Path({str(original)!r}).read_bytes() == b'package'
""")
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        with patch.dict(os.environ, RUNNER_TEMP=str(self.root)), job_context(self.source):
            linux.verify(repo, local=True)
        self.assertEqual(original.read_bytes(), b'package')


class NpmRoundtrip(unittest.TestCase):
    def test_actual_verify_cold_fill_then_offline_fresh_install(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home, repo = root / 'caller', root / 'repo'
            home.mkdir()
            (repo / '.fleet').mkdir(parents=True)
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode='w:gz') as tar:
                for name, body in {'package.json': b'{"name":"cache-proof","version":"1.0.0","main":"index.js"}',
                                   'index.js': b'module.exports = 42;\n'}.items():
                    info = tarfile.TarInfo('package/' + name)
                    info.size = len(body)
                    tar.addfile(info, io.BytesIO(body))
            payload = archive.getvalue()
            requests = []

            class Handler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    requests.append(self.path)
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

                def log_message(self, *args):
                    pass

            server = http.server.ThreadingHTTPServer(('localhost', 0), Handler)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            self.addCleanup(server.server_close)
            url = f'http://localhost:{server.server_port}/package.tgz'
            manifest = {'name': 'proof', 'version': '1.0.0', 'dependencies': {'cache-proof': url}}
            (repo / 'package.json').write_text(json.dumps(manifest))
            lock = {'name': 'proof', 'version': '1.0.0', 'lockfileVersion': 3, 'requires': True,
                    'packages': {'': manifest, 'node_modules/cache-proof': {
                        'version': '1.0.0', 'resolved': url,
                        'integrity': 'sha512-' + base64.b64encode(hashlib.sha512(payload).digest()).decode()}}}
            (repo / 'package-lock.json').write_text(json.dumps(lock))
            (home / '.npmrc').write_text('registry=http://unreachable.invalid\n')
            report = root / 'runs.jsonl'
            content = object_path(home, payload)
            recipe = repo / '.fleet/verify.py'
            recipe.write_text(f'''import json, os, pathlib, subprocess
home = pathlib.Path(os.environ['HOME'])
assert not (home / '.npmrc').exists()
assert not pathlib.Path('node_modules').exists()
subprocess.run(['npm', 'ci', '--ignore-scripts', '--no-audit', '--no-fund', *(['--offline'] if os.environ.get('PROOF_OFFLINE') else [])], check=True)
subprocess.run(['node', '-e', "if (require('cache-proof') !== 42) process.exit(1); console.log('behavior: 42')"], check=True)
with open({str(report)!r}, 'a') as out:
    out.write(json.dumps({{'home': str(home), 'caller_has_content': pathlib.Path({str(content)!r}).exists(), 'cache_env': os.environ.get('npm_config_cache'), 'aliases': [k for k in os.environ if k.casefold() == 'npm_config_cache']}}) + '\\n')
if os.environ.get('PROOF_FAIL'):
    raise SystemExit(7)
''')
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            env = dict(HOME=str(home), NPM_CONFIG_CACHE=str(root / 'wrong'),
                       NpM_cOnFiG_cAcHe=str(root / 'also-wrong'), RUNNER_TEMP=str(root))
            try:
                with patch.dict(os.environ, {**env, 'PROOF_FAIL': '1'}), job_context(home):
                    with self.assertRaises(subprocess.CalledProcessError):
                        linux.verify(repo, local=True)
                self.assertFalse(content.exists())
                with patch.dict(os.environ, env), job_context(home):
                    linux.verify(repo, local=True)
            finally:
                server.shutdown()
                thread.join()
            count = len(requests)
            self.assertEqual(count, 2)
            fixed = content.exists()
            with patch.dict(os.environ, {**env, 'PROOF_OFFLINE': '1'}), job_context(home):
                linux.verify(repo, local=True)
            self.assertTrue(fixed)
            self.assertEqual(content.read_bytes(), payload)
            self.assertEqual(len(requests), count)
            runs = [json.loads(line) for line in report.read_text().splitlines()]
            self.assertEqual(len({run['home'] for run in runs}), 3)
            self.assertTrue(all(run['cache_env'] == str(Path(run['home']) / '.npm') for run in runs))
            self.assertTrue(all(run['aliases'] == ['npm_config_cache'] for run in runs))
            self.assertEqual([run['caller_has_content'] for run in runs], [False, False, True])
            files = [p.relative_to(home).as_posix() for p in home.rglob('*') if p.is_file()]
            self.assertEqual(sorted(files), sorted(['.npmrc', 'guest-host', content.relative_to(home).as_posix()]))
            print(f'PROOF npm: cold requests={count}, offline requests=0, behavior=42, fresh homes=3', flush=True)


if __name__ == '__main__':
    unittest.main()
