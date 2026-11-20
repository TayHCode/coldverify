#!/usr/bin/env python3
"""Replay changed tests on the PR base and head to evaluate proof."""
import ast
import contextlib
import importlib
import importlib.util
import inspect
import io
import json
import logging
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from functools import partial
from pathlib import Path
from urllib.parse import unquote, urlsplit

CASE_TIMEOUT = 300
OUTPUT_LIMIT = 1000
NO_TEST_CASES = 'no new or changed test cases found, so nothing reproduces the claim'
BASH_COMMAND = ('bash', '--')
TEST_PATH = re.compile(r'(^|/)(tests?|__tests__)/|(^|/)(?:test[_-]|.*[._-](?:test|spec)\.)', re.IGNORECASE)

LOG = logging.getLogger(__name__)
GIT_VARS = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR',
            'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES')


SECRET = re.compile(r'gh[pousr]_\w{20,}|github_pat_\w{20,}|sk-[\w-]{20,}|xox[abprs]-[\w-]{10,}|AKIA[0-9A-Z]{16}'
                    r'|[^/\s:@]+:[^/\s@]+(?=@)|(?i:bearer\s+\S+|\b(?:token|password|passwd|secret|api[_-]?key)[=:]\S+)')


class NoRepro(RuntimeError):
    pass


def excerpt(text, limit=500):
    text = SECRET.sub('[redacted]', ' '.join(str(text).split()))
    return text if len(text) <= limit else text[:limit // 2] + ' … ' + text[-limit // 2:]


def identity_error(root, base, head, checkout, parents):
    def commit(value):
        return value if isinstance(value, str) and re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value) else '<invalid>'
    return 'PR head differs from the checked-out commit: ' + json.dumps({
        'event_applies': pr_event_applies(root), 'base': commit(base), 'head': commit(head),
        'checkout': commit(checkout), 'parents': [commit(parent) for parent in parents]}, sort_keys=True)


def clean_environment():
    return {key: value for key, value in os.environ.items() if key not in GIT_VARS}


def default_base(root):
    try:
        return git(root, 'merge-base', 'HEAD', 'origin/main').decode().strip()
    except subprocess.CalledProcessError:
        raise NoRepro('no PR base, and HEAD has no merge base with origin/main') from None


def commit_parents(root, commit):
    headers = git(root, '--no-replace-objects', 'cat-file', 'commit', commit).partition(b'\n\n')[0]
    return [line.split()[1].decode('ascii') for line in headers.splitlines() if line.startswith(b'parent ')]


def merge_checkout_base(root, base, head, checkout):
    parents = commit_parents(root, checkout)
    error = identity_error(root, base, head, checkout, parents)
    if (not isinstance(base, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', base)
            or not pr_event_applies(root) or len(parents) != 2 or parents[1] != head):
        raise RuntimeError(error)
    if parents[0] == base:
        return base
    branch = os.environ.get('GITHUB_BASE_REF', '')
    if not branch:
        raise RuntimeError(error)
    try:
        ref = 'refs/remotes/origin/' + branch
        git(root, 'check-ref-format', ref)
        shallow = git(root, 'rev-parse', '--is-shallow-repository').decode().strip() == 'true'
        exists = subprocess.run(['git', '-C', str(root), 'show-ref', '--verify', '--quiet', ref],
                                env=clean_environment()).returncode == 0
        if shallow or not exists:
            git(root, 'fetch', '--no-tags', *(['--unshallow'] if shallow else []),
                'origin', 'refs/heads/' + branch + ':' + ref)
        git(root, 'merge-base', '--is-ancestor', base, parents[0])
        git(root, 'merge-base', '--is-ancestor', parents[0], ref)
    except subprocess.CalledProcessError as cause:
        raise RuntimeError(error) from cause
    return parents[0]


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True, env=clean_environment()).stdout


def setup_git(root, *args):
    try:
        return git(root, *args)
    except subprocess.CalledProcessError as error:
        detail = excerpt(error.stderr.decode(errors='replace')) or f'exit {error.returncode}'
        raise NoRepro(f'git {" ".join(args[:2])} failed: {detail}') from error


def methods(source):
    return {name: ast.dump(node, include_attributes=False) for name, node in test_functions(source).items()}


def test_functions(source):
    tree = ast.parse(source)
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    test_bases = {'unittest.TestCase', 'unittest.IsolatedAsyncioTestCase', 'TestCase', 'IsolatedAsyncioTestCase'}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name == 'unittest':
                    test_bases.update({(item.asname or item.name) + '.TestCase', (item.asname or item.name) + '.IsolatedAsyncioTestCase'})
        elif isinstance(node, ast.ImportFrom) and node.module == 'unittest':
            test_bases.update(item.asname or item.name for item in node.names if item.name in ('TestCase', 'IsolatedAsyncioTestCase'))

    def base_name(node):
        return node.id if isinstance(node, ast.Name) else ast.unparse(node) if isinstance(node, ast.Attribute) else ''

    def mro(name, seen):
        if name not in classes:
            return [name]
        if name in seen:
            raise ValueError('cyclic test inheritance')
        bases = [base_name(base) for base in classes[name].bases if base_name(base)]
        sequences = [mro(base, seen | {name}) for base in bases] + [bases[:]]
        order = [name]
        while any(sequences):
            sequences = [seq for seq in sequences if seq]
            candidate = next((seq[0] for seq in sequences if not any(seq[0] in other[1:] for other in sequences)), None)
            if candidate is None:
                raise ValueError('inconsistent test inheritance')
            order.append(candidate)
            for seq in sequences:
                if seq[0] == candidate:
                    seq.pop(0)
        return order

    result = {}
    for name in classes:
        order = mro(name, set())
        external = [base for base in order if base not in classes]
        if not any(base in test_bases or base not in ('object', 'ABC', 'abc.ABC', 'Protocol', 'typing.Protocol') for base in external):
            continue
        cases = {}
        for ancestor in order:
            if ancestor in classes:
                for node in classes[ancestor].body:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test'):
                        cases.setdefault(node.name, node)
        result.update({f'{name}.{case}': value for case, value in cases.items()})
    result.update({node.name: node for node in tree.body
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test')})
    return result


def load_test_module(path):
    test_path = Path(path).resolve()
    package = []
    parent = test_path.parent
    while (parent / '__init__.py').is_file():
        package.insert(0, parent.name)
        parent = parent.parent
    name_parts = package + [test_path.stem]
    module_name = '.'.join(name_parts) if all(part.isidentifier() for part in name_parts) else '_cold_repro_test'
    sys.path.insert(0, str(parent))
    sys.path.insert(0, str(Path.cwd()))
    sys.path.insert(0, str(test_path.parent))
    if package and module_name != '_cold_repro_test':
        importlib.import_module('.'.join(package))
    spec = importlib.util.spec_from_file_location(module_name, test_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def list_cases(path, output):
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            module = load_test_module(path)
        names = []
        for cls in vars(module).values():
            if isinstance(cls, type) and issubclass(cls, unittest.TestCase) and cls is not unittest.TestCase and cls.__module__ == module.__name__:
                names.extend(f'{cls.__name__}.{name}' for name in unittest.defaultTestLoader.getTestCaseNames(cls))
        names.extend(name for name, value in vars(module).items()
                     if name.startswith('test') and inspect.isfunction(value) and value.__module__ == module.__name__)
        Path(output).write_text(json.dumps(sorted(set(names))), encoding='utf-8')
        return 0
    except (Exception, SystemExit):
        LOG.exception('Could not list cases in %s', path)
        return 2


def missing_name(error):
    while error.__cause__ is not None and not isinstance(error, (ImportError, AttributeError)):
        error = error.__cause__
    if isinstance(error, ModuleNotFoundError):
        return error.name and [error.name]
    if isinstance(error, ImportError):
        found = re.match(r"cannot import name '(\w+)'", str(error))
        return found and error.name and [f'{error.name}.{found[1]}']
    if not isinstance(error, AttributeError) or not error.name:
        return None
    owner = getattr(error, 'obj', None)
    if inspect.ismodule(owner):
        return [f'{owner.__name__}.{error.name}']
    owner = owner if isinstance(owner, type) else type(owner)
    return [f'{cls.__module__}.{cls.__qualname__}.{error.name}' for cls in owner.__mro__]


def missing_on_base(errors):
    names = [missing_name(error) for error in errors]
    if not names or not all(names):
        return 2
    report = os.environ.get('COLD_REPRO_MISSING')
    if report:
        Path(report).write_text(json.dumps(names), encoding='utf-8')
    return 3


def defined_names(source, module):
    names = set()
    pending = [(ast.parse(source), module)]
    while pending:
        node, scope = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(f'{scope}.{node.name}')
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(f'{scope}.{node.id}')
        elif isinstance(node, ast.alias):
            names.add(f"{scope}.{(node.asname or node.name).split('.')[0]}")
        if isinstance(node, ast.ClassDef):
            pending.extend((child, f'{scope}.{node.name}') for child in node.body)
        elif not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
                                   ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            pending.extend((child, scope) for child in ast.iter_child_nodes(node))
    return names


def added_names(root, fork, head, paths):
    names = set()
    tree = set(git(root, 'ls-tree', '-r', '--name-only', '-z', head).decode('utf-8', 'replace').split('\0'))
    for path, old_path in paths.items():
        if not path.endswith('.py'):
            continue
        parts = [] if Path(path).stem == '__init__' else [Path(path).stem]
        package = Path(path).parent
        while package != Path('.') and (package / '__init__.py').as_posix() in tree:
            parts.insert(0, package.name)
            package = package.parent
        module = '.'.join(parts)
        old = subprocess.run(['git', '-C', str(root), 'show', f'{fork}:{old_path}'], capture_output=True, env=clean_environment(), check=False)
        try:
            names |= defined_names(git(root, 'show', f'{head}:{path}'), module) - (
                defined_names(old.stdout, module) if old.returncode == 0 else set())
        except (SyntaxError, ValueError):
            continue
        if old.returncode or old_path != path:
            names.add(module)
    return names


def run_pytest_function(path, name):
    import pytest
    phases = []
    raised = []

    class Recorder:
        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_makereport(self, item, call):
            outcome = yield
            phases.append((call.when, call.excinfo, outcome.get_result()))

        def pytest_exception_interact(self, node, call, report):
            raised.append(call.excinfo.value)

    def red(excinfo, report):
        return report.failed and excinfo is not None and isinstance(excinfo.value, (AssertionError, pytest.fail.Exception))

    sys.path.insert(0, os.getcwd())
    code = pytest.main(['-q', '-p', 'no:cacheprovider', f'{path}::{name}'], plugins=[Recorder()])
    calls = [(excinfo, report) for when, excinfo, report in phases if when == 'call']
    if any(hasattr(report, 'wasxfail') for _, _, report in phases):
        return 2
    if (code not in (0, 1) or not calls
            or any(when != 'call' and not report.passed for when, _, report in phases)
            or not all(report.passed or red(excinfo, report) for excinfo, report in calls)):
        return missing_on_base(raised)
    return 1 if any(report.failed for _, report in calls) else 0


class KeptErrors(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.raised = []

    def addError(self, test, err):
        super().addError(test, err)
        self.raised.append(err[1])


def run_case(path, name):
    try:
        if '.' not in name:
            return run_pytest_function(path, name)
        module = load_test_module(path)
        suite = unittest.defaultTestLoader.loadTestsFromName(name, module)
        result = unittest.TextTestRunner(verbosity=1, resultclass=KeptErrors).run(suite)
        if result.errors and not (result.failures or result.skipped or result.expectedFailures or result.unexpectedSuccesses):
            return missing_on_base(result.raised)
        if result.errors or result.testsRun != 1 or result.skipped or result.expectedFailures or result.unexpectedSuccesses:
            return 2
        return 1 if result.failures else 0 if result.wasSuccessful() else 2
    except (Exception, SystemExit) as error:
        LOG.exception('Could not run %s', name)
        return missing_on_base([error])


def changed_lines(root, fork, head, paths):
    changes = {}
    for path, old_path in paths.items():
        diff = git(root, 'diff', '-U0', '--no-ext-diff', '--find-renames', fork, head, '--', old_path, path).decode('utf-8', 'replace')
        if path.endswith('.go'):
            if lines := go_changed_lines(root, fork, head, old_path, path, diff):
                changes[path] = lines
            continue
        for match in re.finditer(r'(?m)^@@ -\S+ \+(\d+)(?:,(\d+))? @@', diff):
            start, count = int(match[1]), int(match[2] or 1)
            changes.setdefault(path, []).extend(range(start, start + count) if count else (start, start + 1))
    return changes


SITECUSTOMIZE = """import importlib.util, json, os
_trace = os.environ.get('COLD_REPRO_TRACE')
if _trace:
    _module, _changes, _hits, _root = json.loads(_trace)
    _spec = importlib.util.spec_from_file_location('_cold_repro_trace', _module)
    _repro = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_repro)
    _repro.trace_changes(_changes, _hits, _root)
import sys
_here = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [item for item in sys.path if os.path.abspath(item or '.') != _here]
del sys.modules['sitecustomize']
try:
    import sitecustomize
except ImportError:
    pass
"""


def trace_changes(changes_file, hits_file, root):
    import threading
    changes = {rel: set(lines) for rel, lines in json.loads(Path(changes_file).read_text(encoding='utf-8')).items()}
    root = Path(root).resolve()
    known = {}
    hit = False

    def changed(filename):
        if filename not in known:
            resolved = Path(filename).resolve()
            rel = resolved.relative_to(root).as_posix() if resolved.is_relative_to(root) else None
            if rel is None and '/site-packages/' in resolved.as_posix():
                tail = resolved.as_posix().split('/site-packages/', 1)[1]
                rel = next((item for item in changes if (item == tail or item.endswith('/' + tail))
                            and (root / item).is_file() and (root / item).read_bytes() == resolved.read_bytes()), None)
            known[filename] = rel if rel in changes else None
        return known[filename]

    def importing(frame):
        while frame is not None:
            if frame.f_code.co_name == '<module>' and frame.f_globals.get('__name__') != '__main__':
                return True
            frame = frame.f_back
        return False

    def local(frame, event, arg):
        nonlocal hit
        source = changed(frame.f_code.co_filename)
        line = frame.f_lineno
        if not hit and event in ('call', 'line') and line in changes[source] and not importing(frame):
            hit = True
            Path(hits_file).write_text(json.dumps([source, line]), encoding='utf-8')
        return None if hit else local

    def tracer(frame, event, arg):
        if hit or not frame.f_code.co_flags & inspect.CO_OPTIMIZED or not changed(frame.f_code.co_filename):
            return None
        return local(frame, event, arg)

    threading.settrace(tracer)
    sys.settrace(tracer)


def run_traced_case(path, name, changes_file, hits_file):
    hooks = Path(hits_file).with_suffix('.hooks')
    hooks.mkdir()
    (hooks / 'sitecustomize.py').write_text(SITECUSTOMIZE, encoding='utf-8')
    os.environ['COLD_REPRO_TRACE'] = json.dumps([str(Path(__file__).resolve()), str(changes_file), str(hits_file), str(Path.cwd().resolve())])
    os.environ['PYTHONPATH'] = os.pathsep.join(filter(None, [str(hooks), os.environ.get('PYTHONPATH')]))
    trace_changes(changes_file, hits_file, Path.cwd())
    try:
        return run_case(path, name)
    finally:
        sys.settrace(None)


NODE_TEST_SUFFIXES = {'.js', '.mjs', '.cjs'}
TYPESCRIPT_SUFFIXES = {'.ts', '.tsx', '.mts', '.cts'}
JS_TEST_SUFFIXES = NODE_TEST_SUFFIXES | {'.jsx'} | TYPESCRIPT_SUFFIXES
NODE_CASE = r'''(?:test|it)\(\s*(?P<quote>['"])(?P<name>(?:(?!(?P=quote))[^\\\n])+)(?P=quote)'''
DECLARATIONS = {
    'go': re.compile(r'(?m)^func (?P<name>Test\w*)\(\s*\w+\s+\*testing\.T\s*\)'),
    'cargo': re.compile(r'(?m)^#\[(?:\w+::)*test\]\s*(?:#\[[^\]\n]*\]\s*)*(?:pub\s+)?(?:async\s+)?fn\s+(?P<name>\w+)'),
    'node': re.compile(r'(?m)^' + NODE_CASE),
}
UNSELECTABLE = {
    'go': re.compile(r'(?m)^.*\bfunc\s.*\bTest\w*\s*\(.*$'),
    'cargo': re.compile(r'(?m)^.*#\[(?:\w+::)*test\b.*$'),
    'node': re.compile(r'(?m)^.*\b(?:test|it)(?:\.\w+)*\s*\(.*$'),
}
BODY_END = re.compile(r'(?m)^\}.*$')
VITEST_DECLARATION = re.compile(r'(?m)^(?P<indent>[ \t]*)' + NODE_CASE)


def nearest(exists, path, marker):
    return next((parent for parent in Path(path).parents if exists((parent / marker).as_posix())), None)


def foreign_runner(root, head, tree, path, *, strict=False):
    file = Path(path)
    if strict and file.suffix == '.sh':
        lines = git(root, 'show', f'{head}:{path}').split(b'\n', 1)
        if re.fullmatch(rb'#![ \t]*(?:(?:/usr)?/bin/bash|/usr/bin/env[ \t]+bash)[ \t]*', lines[0]):
            return 'bash'
    if file.name.endswith('_test.go'):
        return 'go' if nearest(tree.__contains__, path, 'go.mod') else None
    if file.suffix == '.rs' and 'tests' in file.parts[:-1]:
        return 'cargo' if nearest(tree.__contains__, path, 'Cargo.toml') else None
    package = nearest(tree.__contains__, path, 'package.json')
    if package is None or file.suffix not in JS_TEST_SUFFIXES:
        return None
    manifest = json.loads(git(root, 'show', f'{head}:{(package / "package.json").as_posix()}'))
    sections = [manifest.get(key, {}) for key in ('dependencies', 'devDependencies')] if isinstance(manifest, dict) else [None]
    if not all(isinstance(section, dict) for section in sections):
        raise ValueError('package.json and its dependencies must be JSON objects: ' + path)
    if 'jest' in manifest or any('jest' in section for section in sections):
        return 'jest'
    if any('vitest' in section for section in sections):
        return 'vitest'
    return 'node' if file.suffix in NODE_TEST_SUFFIXES else None


def declared_tests(runner, path, source):
    if runner == 'bash':
        return {'bash': source}, ''
    runner = 'node' if runner == 'jest' else runner
    declaration = VITEST_DECLARATION if runner == 'vitest' else DECLARATIONS[runner]
    syntax = UNSELECTABLE['node' if runner == 'vitest' else runner]
    found = [] if runner == 'cargo' and Path(path).parent.name != 'tests' else declaration.finditer(source)
    chunks = {}
    rest = []
    end = 0
    for match in found:
        if match.start() < end:
            continue
        line_end = source.find('\n', match.end())
        line_end = len(source) if line_end < 0 else line_end
        terminator = r'\);?\s*$' if runner == 'vitest' else r'\}\)?;?\s*$'
        one_line = re.search(terminator, source[match.end():line_end])
        if one_line and syntax.search(source[match.end():line_end]):
            continue
        body_end = re.compile(r'(?m)^' + re.escape(match['indent']) + r'\}\s*\).*$') if runner == 'vitest' else BODY_END
        close = None if one_line else body_end.search(source, line_end)
        stop = line_end if one_line else close.end() if close else len(source)
        rest.append(source[end:match.start()])
        chunks[match['name']] = chunks.get(match['name'], '') + source[match.start():stop]
        end = stop
    rest = ''.join(rest + [source[end:]])
    return chunks, rest if syntax.search(rest) else ''


def node_dependencies(package, env, label, run):
    manifest = json.loads((package / 'package.json').read_text(encoding='utf-8'))
    if (package / 'node_modules').exists() or not any(manifest.get(key) for key in ('dependencies', 'devDependencies')):
        return
    install = 'ci' if (package / 'package-lock.json').is_file() else 'install'
    if run(['npm', install, '--ignore-scripts', '--no-audit', '--no-fund'], package, env):
        raise RuntimeError('could not install npm dependencies for ' + label)


def vitest_dependencies(root, base, head, paths):
    packages = set()
    for path in paths:
        package = nearest(lambda marker: (head / marker).is_file(), path, 'package.json')
        if package in packages:
            continue
        if not shutil.which('node'):
            raise NoRepro(f'Node.js is required to reproduce Vitest test {path}')
        modules = root / package / 'node_modules'
        binary = modules / '.bin/vitest'
        if (modules.is_symlink() or not modules.resolve().is_relative_to(root) or not binary.is_file()
                or not binary.resolve().is_relative_to(modules.resolve())):
            raise NoRepro(f'Vitest dependencies are missing for {path}; run the repository setup before Coldverify')
        projects = []
        for checkout in (base, head):
            project = checkout / package
            manifest = project / 'package.json'
            if not project.resolve().is_relative_to(checkout.resolve()) or not manifest.is_file() or manifest.is_symlink():
                raise NoRepro(f'Vitest project {package} is missing for {path}')
            projects.append(project)
        inputs = ('dependencies', 'devDependencies', 'optionalDependencies', 'peerDependencies', 'overrides', 'resolutions')
        manifests = [json.loads((project / 'package.json').read_text(encoding='utf-8')) for project in projects]
        if any(manifests[0].get(key) != manifests[1].get(key) for key in inputs):
            raise NoRepro(f'Vitest dependencies changed for {path}; the setup must match both revisions')
        for lock in ('package-lock.json', 'pnpm-lock.yaml', 'yarn.lock', 'bun.lock', 'bun.lockb'):
            base_lock, head_lock = (project / lock for project in projects)
            if (base_lock.is_symlink() or head_lock.is_symlink() or base_lock.is_file() != head_lock.is_file()
                    or (base_lock.is_file() and base_lock.read_bytes() != head_lock.read_bytes())):
                raise NoRepro(f'Vitest dependency lock changed for {path}; the setup must match both revisions')
        try:
            for project in projects:
                shutil.copytree(modules, project / 'node_modules', symlinks=True)
        except OSError as error:
            raise NoRepro(f'could not copy prepared Vitest dependencies for {path}: {excerpt(error)}') from error
        packages.add(package)


def vitest_result(name, report, path, output, code):
    if report.is_symlink() or re.search(r'(?m)^[⎯─ ]*Unhandled (?:Errors?|Rejection|Exception)[⎯─ ]*$', output):
        return 2
    try:
        data = json.loads(report.read_text(encoding='utf-8'))
        suite, = data['testResults']
        if Path(suite['name']) != path or suite.get('message'):
            return 2
        executed = [case for case in suite['assertionResults'] if case['status'] in ('passed', 'failed')]
        if name is None:
            return 0 if code == 0 and data['success'] and not executed else 2
        result, = executed
        if result['title'] != name:
            return 2
        if code == 0 and data['success'] and result['status'] == 'passed':
            return 0
        failures = result['failureMessages']
        if (code == 1 and not data['success'] and result['status'] == 'failed' and failures
                and all(re.match(r'AssertionError(?: \[ERR_ASSERTION\])?: ', failure) for failure in failures)):
            return 1
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return 2


def istanbul_changed_line(report, control, checkout, changes):
    if any(path.is_symlink() or path.parent.is_symlink() for path in (report, control)):
        return None
    try:
        coverage = json.loads(report.read_text(encoding='utf-8'))
        imported = json.loads(control.read_text(encoding='utf-8'))
        for path, changed in changes.items():
            absolute = str(checkout / path)
            file = coverage.get(absolute)
            if not file:
                continue
            baseline = imported.get(absolute, {})
            functions = [entry['loc'] for key, entry in file['fnMap'].items() if file['f'][key] > baseline.get('f', {}).get(key, 0)]
            for key, statement in file['statementMap'].items():
                line = statement['start']['line']
                if (file['s'][key] > baseline.get('s', {}).get(key, 0) and line in changed
                        and any(fn['start']['line'] <= line <= fn['end']['line'] for fn in functions)):
                    return path, line
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def vitest_exercised(report, control, checkout, changes):
    return istanbul_changed_line(report, control, checkout, changes) is not None


NODE_LOCATIONS = """const { Session } = require('node:inspector');
const { writeFileSync } = require('node:fs');
const { join } = require('node:path');
const { fileURLToPath } = require('node:url');
const { threadId } = require('node:worker_threads');
const sources = new Set(JSON.parse(process.env.COLD_REPRO_JS_SOURCES));
// node --test can initialize the inspector after preloads (nodejs/node#48467).
process.on('exit', () => {
    const session = new Session();
    try {
        session.connect();
    } catch {
        return;
    }
    const scripts = [];
    session.on('Debugger.scriptParsed', ({ params }) => {
        if (params.url.startsWith('file:') && !params.hasSourceURL && sources.has(fileURLToPath(params.url))) {
            scripts.push(params);
        }
    });
    session.post('Debugger.enable');
    const locations = {};
    for (const { scriptId, url } of scripts) {
        session.post('Debugger.getPossibleBreakpoints', { start: { scriptId, lineNumber: 0, columnNumber: 0 } },
            (error, result) => {
                if (!error) locations[url] = result.locations.map(({ lineNumber, columnNumber }) => [lineNumber + 1, columnNumber]);
            });
    }
    if (scripts.length) {
        writeFileSync(join(process.env.NODE_V8_COVERAGE, `locations-${process.pid}-${threadId}.json`), JSON.stringify(locations));
    }
});
"""


def file_url(url):
    parsed = urlsplit(url)
    if parsed.scheme == 'file' and not (parsed.query or parsed.fragment):
        return parsed.netloc, unquote(parsed.path)
    return None


def node_coverage(directory):
    if directory.is_symlink():
        raise ValueError('symlinked coverage directory')
    reports = list(directory.glob('coverage-*.json'))
    if not reports:
        raise ValueError('no Node coverage reports')
    scripts = {}
    for report in reports:
        for script in read_case_json(report)['result']:
            location = file_url(script['url'])
            if location is not None:
                scripts.setdefault(location, []).append(script['functions'])
    return scripts


def node_ranges(scripts):
    for functions in scripts:
        ranges = []
        for index, function in enumerate(functions):
            entries = function['ranges']
            if index == 0 and function['functionName'] == '' and entries[0]['startOffset'] == 0:
                continue
            for entry in entries:
                ranges.append((entry['startOffset'], entry['endOffset'], entry['count']))
        yield sorted(ranges, key=lambda item: item[1] - item[0])


def node_changed_line(report, control, checkout, changes):
    non_executable = re.compile(r'(?:[\s{}()[\];,]|/\*(?:(?!\*/).)*\*/)*(?://.*|/\*(?!.*\*/).*)?')

    def count(profiles, offset):
        return sum(next((count for start, end, count in ranges if start <= offset < end), 0) for ranges in profiles)

    try:
        actual, imported = node_coverage(report), node_coverage(control)
        locations = {}
        for report_file in report.glob('locations-*.json'):
            for url, points in read_case_json(report_file).items():
                source_locations = locations.setdefault(file_url(url), {})
                for row, column in points:
                    source_locations.setdefault(row, set()).add(column)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, NoRepro):
        return None
    for path, changed in changes.items():
        file = checkout / path
        url = file_url(file.as_uri())
        if url not in actual or url not in locations:
            continue
        try:
            before = list(node_ranges(imported.get(url, ())))
            after = list(node_ranges(actual[url]))
            offset = 0
            with file.open(encoding='utf-8', newline='') as source:
                lines = re.split(r'(\r\n|[\r\n\u2028\u2029])', source.read())
            for line, (content, ending) in enumerate(zip(lines[::2], lines[1::2] + ['']), 1):
                if line in changed and not non_executable.fullmatch(content):
                    points = (offset + column for column in locations[url].get(line, ()))
                    if any(count(after, point) > count(before, point) for point in points):
                        return path, line
                offset += len((content + ending).encode('utf-16-le')) // 2
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            continue
    return None


GO_COVER_BLOCK = re.compile(r'(.+):(\d+)\.(\d+),(\d+)\.(\d+) \d+ (\d+)')
GO_COMMENT_OR_LITERAL = re.compile(r"""//[^\n]*|/\*[\s\S]*?\*/|`[^`]*`|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'""")


def go_code_lines(root, revision, path):
    source = git(root, 'show', f'{revision}:{path}').decode('utf-8', 'replace')

    def without_comment(match):
        literal = match[0]
        return '\n' * literal.count('\n') if literal.startswith('/') else literal.replace('\n', '`\n`')

    return [line.strip() for line in GO_COMMENT_OR_LITERAL.sub(without_comment, source).split('\n')]


def go_changed_lines(root, fork, head, old_path, path, diff):
    hunks = list(re.finditer(r'(?m)^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', diff))
    if not hunks:
        return []
    before = go_code_lines(root, fork, old_path) if any(match[2] != '0' for match in hunks) else []
    after = go_code_lines(root, head, path)
    changed = []
    for match in hunks:
        old_start, old_count, start, count = (int(value or 1) for value in match.groups())
        removed = before[old_start - 1:old_start - 1 + old_count] if old_count else ()
        added = after[start - 1:start - 1 + count] if count else ()
        if list(filter(None, removed)) == list(filter(None, added)):
            continue
        end = start + count if count else start + 2
        changed.extend(line for line in range(max(1, start), min(end, len(after) + 1)) if after[line - 1])
    return changed


def go_coverage(profile):
    if profile.is_symlink() or not profile.is_file():
        raise ValueError('missing Go coverage profile')
    with profile.open(encoding='utf-8') as stream:
        if stream.readline().strip() != 'mode: count':
            raise ValueError('expected Go count coverage')
        blocks = {}
        for line in stream:
            match = GO_COVER_BLOCK.fullmatch(line.rstrip('\n'))
            if not match:
                raise ValueError('invalid Go coverage block')
            source, start, start_column, end, end_column, count = match.groups()
            key = (source, int(start), int(start_column), int(end), int(end_column))
            blocks[key] = blocks.get(key, 0) + int(count)
    return blocks


def go_exercised(profile, control, sources):
    try:
        collected = go_coverage(control)
        for block, count in go_coverage(profile).items():
            source, start, _, end, end_column = block
            if block in collected and count > collected[block] and any(
                    start <= line <= end - (end_column == 1) for line in sources.get(source, ())):
                return True
    except (OSError, ValueError):
        return False
    return False


def cargo_coverage_tools(project, env, scratch, run):
    paths = [env.get('PATH')]
    if shutil.which('rustc'):
        with tempfile.TemporaryFile(dir=scratch) as output:
            if run(['rustc', '--print', 'target-libdir'], project, env, output=output) == 0:
                output.seek(0)
                bin_dir = Path(os.fsdecode(output.read()).strip()).parent / 'bin'
                if bin_dir.is_absolute():
                    paths.insert(0, str(bin_dir))
    for path in paths:
        profdata = shutil.which('llvm-profdata', path=path)
        cov = shutil.which('llvm-cov', path=path)
        if profdata and cov:
            return profdata, cov
    return None


def cargo_test_binary(output, test_path, target_dir, *, require_fresh):
    test_path = test_path.resolve()
    binaries = set()
    for line in output.splitlines():
        if not line.startswith('{'):
            continue
        artifact = json.loads(line)
        if (artifact.get('reason') == 'compiler-artifact' and artifact.get('executable')
                and (not require_fresh or artifact.get('fresh') is True)):
            target = artifact['target']
            if target['kind'] == ['test'] and Path(target['src_path']).resolve() == test_path:
                binary = Path(artifact['executable'])
                if binary.resolve().is_relative_to(target_dir) and binary.is_file() and not binary.is_symlink():
                    binaries.add(binary)
    return next(iter(binaries)) if len(binaries) == 1 else None


def cargo_line_counts(output, checkout, project, changes):
    checkout = checkout.resolve()
    path = None
    counts = {}
    for line in output:
        if line.startswith(b'SF:'):
            source = (project / os.fsdecode(line[3:].rstrip(b'\r\n'))).resolve()
            path = source.relative_to(checkout).as_posix() if source.is_relative_to(checkout) else None
        elif line.strip() == b'end_of_record':
            path = None
        elif path in changes and line.startswith(b'DA:'):
            number, count, *_ = line[3:].split(b',')
            number = int(number)
            if number in changes[path]:
                counts[path, number] = int(count)
    return counts


def cargo_exercised(run, command, checkout, project, test_path, name, env, scratch, changes):
    changes = {path: set(lines) for path, lines in changes.items() if path.endswith('.rs')}
    if not changes:
        return False, 'no changed Rust lines'
    tools = cargo_coverage_tools(project, env, scratch, run)
    if tools is None:
        return False, 'LLVM coverage tools are unavailable (install Rust llvm-tools-preview)'
    profdata, cov = tools
    with tempfile.TemporaryDirectory(dir=scratch, prefix='cargo-coverage-') as folder:
        coverage = Path(folder).resolve()
        target_dir = coverage / 'target'
        build_profile = coverage / 'build' / '%m-%p.profraw'
        env = dict(env, CARGO_TARGET_DIR=str(target_dir), RUSTFLAGS='-C instrument-coverage',
                   LLVM_PROFILE_FILE=str(build_profile))
        reports = []
        binary = None
        try:
            for label, selected in (('control', ''), ('test', name)):
                profile = coverage / f'{label}-%m-%p.profraw'
                runner = "target.'cfg(all())'.runner=" + json.dumps(['env', f'LLVM_PROFILE_FILE={profile}'])
                instrumented = [*command[:2], '--message-format=json', '--config', runner, *command[2:-1], selected]
                with tempfile.TemporaryFile(dir=scratch) as output:
                    code = run(instrumented, project, env, output=output)
                    output.seek(0)
                    test_output = output.read().decode(errors='replace')
                if selected:
                    if foreign_result('cargo', selected, test_output, None, code) != 0:
                        return False, 'the instrumented test did not pass'
                elif code != 0 or re.findall(r'(?m)^running (\d+) tests?$', test_output) != ['0']:
                    return False, 'the coverage control did not run with zero tests'
                executable = cargo_test_binary(test_output, test_path, target_dir, require_fresh=bool(selected))
                profiles = sorted(p for p in coverage.glob(f'{label}-*.profraw') if p.is_file() and not p.is_symlink())
                if executable is None or not profiles or (binary is not None and binary != executable):
                    return False, 'the control and selected test did not produce coverage from one test binary'
                binary = executable
                indexed = coverage / f'{label}.profdata'
                if run([profdata, 'merge', '-sparse', '-o', str(indexed), *map(str, profiles)], project, env):
                    return False, 'llvm-profdata could not merge the test profiles'
                with tempfile.TemporaryFile(dir=scratch) as output:
                    if run([cov, 'export', '--format=lcov', '--instr-profile=' + str(indexed), str(binary)],
                           project, env, output=output):
                        return False, 'llvm-cov could not export the test coverage'
                    output.seek(0)
                    reports.append(cargo_line_counts(output, checkout, project, changes))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return False, 'the instrumented test coverage could not be read'
    control, selected = reports
    for (path, line), count in selected.items():
        if count > control.get((path, line), 0):
            return True, f'ran {path}:{line}'
    return False, 'no changed Rust line ran in the selected test'


def foreign_result(runner, name, output, report, code):
    result = parsed_result(runner, name, output, report)
    return result if (result, code) in ((0, 0), (1, 1), (1, 101)) else 2


def parsed_result(runner, name, output, report):
    if runner == 'go':
        events = [json.loads(line) for line in output.splitlines() if line.startswith('{')]
        actions = [event.get('Action') for event in events if event.get('Test') == name and event.get('Action') in ('pass', 'fail', 'skip')]
        return {('pass',): 0, ('fail',): 1}.get(tuple(actions), 2)
    if runner == 'cargo':
        results = re.findall(rf'(?m)^test {re.escape(name)} \.\.\. (\w+)$', output)
        return {('ok',): 0, ('FAILED',): 1}.get(tuple(results), 2)
    if runner == 'node':
        results = re.findall(rf'(?m)^(not )?ok \d+ - {re.escape(name)}( +#.*)?\n((?:[ \t]+.*\n|\n)*)', output + '\n')
        if len(results) != 1 or results[0][1]:
            return 2
        failed, _, details = results[0]
        return 0 if not failed else 1 if re.search(r"(?m)^  code: 'ERR_ASSERTION'$", details) else 2
    try:
        results = [result for suite in json.loads(Path(report).read_text(encoding='utf-8'))['testResults']
                   for result in suite['assertionResults'] if result['status'] in ('passed', 'failed')]
    except (OSError, ValueError, KeyError, TypeError):
        return 2
    if len(results) != 1 or results[0].get('title') != name:
        return 2
    details = results[0].get('failureDetails') or []
    if results[0]['status'] == 'passed':
        return 0
    asserted = details and all(isinstance(item, dict) and ('matcherResult' in item or item.get('code') == 'ERR_ASSERTION') for item in details)
    return 1 if asserted else 2


def replay_foreign(run, runner, name, command, project, env, report):
    with tempfile.TemporaryFile(dir=report.parent.parent) as output:
        code = run(command, project, env, output=output)
        output.seek(0)
        raw = output.read()
    return foreign_result(runner, name, raw.decode('utf-8', errors='replace'), report, code), raw


def javascript_hit(run, runner, name, command, project, checkout, env, report, changes):
    checkout = checkout.resolve()
    changes = {path: lines for path, lines in changes.items()
               if Path(path).suffix in NODE_TEST_SUFFIXES and (checkout / path).resolve() == checkout / path}
    if not changes:
        return None
    coverage = report.with_suffix('.coverage')
    control = report.with_suffix('.import-coverage')
    command = list(command)
    control_env = env
    if runner == 'node':
        locations = report.with_suffix('.cjs')
        locations.write_text(NODE_LOCATIONS, encoding='utf-8')
        coverage.mkdir()
        control.mkdir()
        command[1:1] = ['--require', str(locations)]
        env = dict(env, COLD_REPRO_JS_SOURCES=json.dumps([str(checkout / path) for path in changes]))
        control_command = [*command[:-2], '--test-name-pattern=(?!)', command[-1]]
        control_env = dict(env, NODE_V8_COVERAGE=str(control))
        env = dict(env, NODE_V8_COVERAGE=str(coverage))
    else:
        out = command.index('--outputFile') + 1
        report = report.with_suffix('.coverage.json')
        command[out] = str(report)
        command += ['--coverage', '--coverageProvider=babel', '--coverageReporters=json']
        control_command = [*command, '--coverageDirectory=' + str(control)]
        control_command[command.index('-t') + 1] = '(?!)'
        control_command[out] = str(report.with_suffix('.import.json'))
        command += ['--coverageDirectory=' + str(coverage)]
    if run(control_command, project, control_env):
        return None
    result, _ = replay_foreign(run, runner, name, command, project, env, report)
    if result != 0:
        return None
    if runner == 'node':
        return node_changed_line(coverage, control, checkout, changes)
    return istanbul_changed_line(coverage / 'coverage-final.json', control / 'coverage-final.json', checkout, changes)


def run_foreign(runner, checkout, path, name, env, scratch, label, outputs, sandbox_root=None, changes=None):
    if runner == 'bash':
        if shutil.which('bash', path=env.get('PATH')) is None:
            raise NoRepro(f'Bash is required to replay {path}')
        if sandbox_root is None:
            raise NoRepro(f'a Linux sandbox is required to replay {path}')
        command = [*BASH_COMMAND, path]
        code = isolated_test(sandbox_root, (checkout, scratch), command, checkout, env, outputs)
        return code, command, False
    project = nearest(lambda marker: (checkout / marker).is_file(), path, {'go': 'go.mod', 'cargo': 'Cargo.toml'}.get(runner, 'package.json'))
    if project is None:
        outputs.append(f'no {runner} project contains {path}')
        return 2, [], False
    project = checkout / project
    relative = (checkout / path).relative_to(project).as_posix()
    pattern = '^' + re.escape(name) + '$'
    report = scratch / f'{label}-jest.json'
    env = dict(env, GOCACHE=str(scratch / 'go-build'), GOMODCACHE=str(scratch / 'go-mod'), GOTOOLCHAIN='local',
               CARGO_HOME=str(scratch / 'cargo-home'), npm_config_cache=str(scratch / 'npm-cache'))
    rustup = os.environ.get('RUSTUP_HOME') or (Path.home() / '.rustup' if (Path.home() / '.rustup').is_dir() else None)
    if rustup:
        env['RUSTUP_HOME'] = str(rustup)
    run = run_test
    install = run_test
    if sandbox_root is not None:
        tools = {'go': ('go',), 'cargo': ('cargo', 'rustc', 'llvm-profdata', 'llvm-cov', 'env'),
                 'vitest': ('node',)}.get(runner, ('node', 'npm'))
        readable = []
        for tool in tools:
            executable = shutil.which(tool)
            if executable:
                executable_path = Path(executable)
                real = executable_path.resolve()
                readable.extend((executable_path.parent, real.parent))
                if tool in ('go', 'npm') and real.parent.name == 'bin':
                    readable.append(real.parent.parent)
        if runner == 'cargo' and rustup:
            readable.append(Path(rustup))
        run = partial(isolated_test, sandbox_root, (checkout, scratch), readable=readable)
        install = partial(run, network=True)
        if runner in ('go', 'cargo'):
            fetch = ['go', 'mod', 'download'] if runner == 'go' else ['cargo', 'fetch']
            setup_output = []
            if install(fetch, project, env, capture=setup_output):
                raise NoRepro(f'could not fetch {runner} dependencies for {label}: {setup_output[0]}')
    if runner in ('node', 'jest'):
        node_dependencies(project, env, label, install)
    if runner == 'jest' and not (project / 'node_modules/.bin/jest').is_file():
        outputs.append('jest is not installed')
        return 2, [], False
    coverage = None
    if runner == 'vitest':
        report = scratch / 'vitest.json'
        command = [str(project / 'node_modules/.bin/vitest'), 'run', './' + relative,
                   '--no-color', '--reporter=json']
        control = scratch / 'import-coverage'
        control_ok = False
        if changes:
            provider = next((item for item in ('v8', 'istanbul')
                             if (project / f'node_modules/@vitest/coverage-{item}/package.json').is_file()), None)
            if provider:
                coverage = scratch / 'coverage'
                command += ['--coverage.enabled', '--coverage.provider=' + provider, '--coverage.reporter=json']
                control_report = scratch / 'import-vitest.json'
                control_command = [*command, '--outputFile.json=' + str(control_report),
                                   '--coverage.reportsDirectory=' + str(control), '-t', '(?!)']
                control_output = []
                control_code = run(control_command, project, env, capture=control_output)
                control_ok = vitest_result(None, control_report, checkout / path, control_output[0], control_code) == 0
                command += ['--coverage.reportsDirectory=' + str(coverage)]
        command += ['--reporter=default', '--outputFile.json=' + str(report), '-t', r'(?:^| )' + re.escape(name) + '$']
        if sandbox_root is None:
            run = partial(run_test, errors=subprocess.STDOUT)
        with tempfile.TemporaryFile(dir=scratch.parent) as output:
            code = run(command, project, env, output=output)
            output.seek(0)
            raw = output.read()
        outputs.append(output_excerpt(raw, b'', False))
        result = vitest_result(name, report, checkout / path, raw.decode(errors='replace'), code)
        hits = (result == 0 and control_ok
                and vitest_exercised(coverage / 'coverage-final.json', control / 'coverage-final.json', checkout, changes))
        if result == 0 and changes and not hits:
            explanation = ('no changed line ran beyond test collection in the Vitest coverage report' if coverage
                           else 'the setup did not provide @vitest/coverage-v8 or @vitest/coverage-istanbul')
            outputs[-1] = excerpt(f'Vitest trace: {explanation}. {outputs[-1]}', OUTPUT_LIMIT)
        return result, command, hits
    command = {
        'go': ['go', 'test', '-json', '-count=1', '-run', pattern, './' + str(Path(relative).parent)],
        'cargo': ['cargo', 'test', '--test', Path(path).stem, '--', '--exact', name],
        'node': ['node', '--test', '--test-reporter=tap', '--test-name-pattern=' + pattern, relative],
        'jest': [str(project / 'node_modules/.bin/jest'), '--ci', '--runTestsByPath', relative, '-t', pattern, '--json', '--outputFile', str(report)],
    }[runner]
    if runner == 'go' and changes:
        target = command[-1]
        module = []
        if not run(['go', 'list', '-f', '{{with .Module}}{{.Path}}{{end}}', target], project, env, capture=module):
            prefix = module[0].strip()
            sources = {f'{prefix}/{source.relative_to(project).as_posix()}': lines
                       for file, lines in changes.items() if file.endswith('.go')
                       and (source := checkout / file).is_relative_to(project)}
            if prefix and sources:
                profile = scratch / f'{label}-go.cover'
                control = scratch / f'{label}-go-control.cover'
                flags = ['-covermode=count', '-coverpkg=./...']
                control_command = [*command[:4], *flags, f'-coverprofile={control}', '-run', '^$', target]
                if not run(control_command, project, env):
                    coverage = profile
                    command[4:4] = [*flags, f'-coverprofile={profile}']
    result, raw = replay_foreign(run, runner, name, command, project, env, report)
    outputs.append(output_excerpt(raw, b'', False))
    hits = result == 0 and coverage is not None and go_exercised(coverage, control, sources)
    if runner == 'cargo' and result == 0 and changes is not None:
        hits, detail = cargo_exercised(run, command, checkout, project, checkout / path, name, env, scratch, changes)
        outputs[-1] = excerpt(f'Cargo trace: {detail}. {outputs[-1]}', OUTPUT_LIMIT)
    elif result == 0 and changes and runner in ('node', 'jest'):
        hit = javascript_hit(run, runner, name, command, project, checkout, env, report, changes)
        if hit:
            hits = True
            outputs[-1] = excerpt(f'JavaScript trace: executed changed line {hit[0]}:{hit[1]}. {outputs[-1]}', OUTPUT_LIMIT)
    return result, command, hits


def run_test(command, directory, env, capture=None, limit=1 << 20, output=subprocess.DEVNULL, errors=subprocess.DEVNULL):
    options = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    piped = capture is not None
    process = subprocess.Popen(command, cwd=directory, env=env, stdout=subprocess.PIPE if piped else output,
                               stderr=subprocess.STDOUT if piped else errors, **options)
    head, tail, seen, key = bytearray(), bytearray(), [0], [b'', False]

    def drain():
        with process.stdout as stream:
            for block in iter(lambda: os.read(stream.fileno(), 65536), b''):
                seen[0] += len(block)
                markers = re.findall(rb'-----(BEGIN|END) [A-Z ]*PRIVATE KEY-----', key[0] + block)
                key[:] = [(key[0] + block)[-64:], markers[-1] == b'BEGIN' if markers else key[1]]
                room = max(0, limit - len(head))
                head.extend(block[:room])
                tail.extend(block[room:])
                del tail[:-limit]

    if piped:
        import threading
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
    try:
        code = process.wait(timeout=CASE_TIMEOUT)
    except subprocess.TimeoutExpired:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()
        print('Repro test timed out', flush=True)
        code = 2
    if os.name != 'nt':
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    if piped:
        reader.join(5)
        capture.append(output_excerpt(bytes(head), bytes(tail), seen[0] > len(head) + len(tail), key[1]))
    return code


def sandbox(root, writable, network, readable=(), *, optional_readable=()):
    bwrap = shutil.which('bwrap')
    if bwrap is None:
        raise NoRepro('bubblewrap (bwrap) is not installed, and the test command never runs unsandboxed')
    root = Path(root).resolve()
    common = Path(git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir').decode().strip())
    hidden = ['/run', '/tmp', '/var', '/home', '/root', '/srv', '/mnt', '/media', str(root)]
    command = [bwrap, '--die-with-parent', '--new-session', '--unshare-all', *(['--share-net'] if network else []),
               '--ro-bind', '/', '/', '--dev', '/dev', '--proc', '/proc']
    for path in hidden:
        command += ['--tmpfs', path]
    resolver = os.path.realpath('/etc/resolv.conf')
    command += ['--ro-bind-try', resolver, resolver, '--ro-bind', str(common), str(common)]
    for path in readable:
        command += ['--ro-bind', str(path), str(path)]
    for path in dict.fromkeys(map(Path, optional_readable)):
        if any(path.is_relative_to(folder) for folder in hidden):
            command += ['--ro-bind-try', str(path), str(path)]
    for path in writable:
        command += ['--bind', str(path), str(path)]
    for path in reversed(hidden):
        command += ['--remount-ro', path]
    return command + ['--']


def run_sandboxed(prefix, command, checkout, env, capture=None, output=None):
    marker = Path(env['HOME']) / '.cold-sandbox-started'
    marker.unlink(missing_ok=True)
    argv = [*prefix, '/bin/sh', '-c', ': > "$0" && exec "$@" </dev/null', str(marker), *command]
    captured = [] if capture is None else capture
    try:
        if output is None:
            code = run_test(argv, checkout, env, capture=captured)
        else:
            code = run_test(argv, checkout, env, output=output, errors=subprocess.STDOUT)
    except OSError as error:
        raise NoRepro('the sandbox could not start: ' + excerpt(error)) from error
    if not marker.is_file():
        if output is not None:
            output.seek(0)
            captured.append(excerpt(output.read(4096).decode('utf-8', errors='replace')))
        detail = captured[-1] if captured else ''
        raise NoRepro(f'the sandbox did not start (exit {code}): {detail}'.rstrip())
    return code


def isolated_test(root, writable, command, checkout, env, capture=None, output=None, *, readable=(), network=False):
    prefix = sandbox(root, writable, network, optional_readable=readable)
    return run_sandboxed(prefix, command, checkout, env, capture, output)


def output_excerpt(head, tail, gap, open_key=False):
    from verify_context import redact
    head, tail = head.decode(errors='replace'), tail.decode(errors='replace')
    if gap:
        head = (head.rsplit(None, 1) or [''])[0]
        tail = re.sub(r'\A(?:(?!-----BEGIN).)*?-----END [A-Z ]*PRIVATE KEY-----', '[REDACTED]',
                      (tail.split(None, 1) or [''])[-1], flags=re.DOTALL)
        if open_key and not re.search(r'-----BEGIN [A-Z ]*PRIVATE KEY-----', tail):
            tail = '[REDACTED]'
        return excerpt(redact(head) + ' … ' + redact(tail), OUTPUT_LIMIT)
    return excerpt(redact(head + tail), OUTPUT_LIMIT)


def case_environment(home, pythonpath=()):
    allowed = {'PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'SYSTEMROOT', 'WINDIR', 'PATHEXT', 'CI'}
    env = {key: value for key, value in clean_environment().items() if key in allowed}
    env.update(HOME=str(home), USERPROFILE=str(home), TEMP=str(home), TMP=str(home), TMPDIR=str(home),
               XDG_CONFIG_HOME=str(home), XDG_DATA_HOME=str(home), XDG_STATE_HOME=str(home),
               XDG_CACHE_HOME=str(home), PYTHONDONTWRITEBYTECODE='1')
    if pythonpath:
        env['PYTHONPATH'] = os.pathsep.join(pythonpath)
    return env


def repro_setup(checkout):
    pyproject = Path(checkout) / 'pyproject.toml'
    if not pyproject.is_file():
        return False, ()
    text = pyproject.read_text(encoding='utf-8')
    try:
        import tomllib
    except ImportError as exc:
        if 'repro' in text:
            raise RuntimeError('[tool.cold.repro] needs Python 3.11 or newer') from exc
        return False, ()
    table = tomllib.loads(text).get('tool', {}).get('cold', {}).get('repro')
    if table is None:
        return False, ()
    no_deps = table.get('no-deps', False)
    pythonpath = table.get('pythonpath', [])
    if type(no_deps) is not bool:
        raise RuntimeError('[tool.cold.repro] no-deps must be true or false')
    if not isinstance(pythonpath, list) or not all(isinstance(item, str) for item in pythonpath):
        raise RuntimeError('[tool.cold.repro] pythonpath must be a list of absolute paths')
    for item in pythonpath:
        if not Path(item).is_absolute():
            raise RuntimeError('[tool.cold.repro] pythonpath entries must be absolute paths: ' + item)
    return no_deps, tuple(pythonpath)


def project_python(checkout, scratch, label, setup=(False, ()), pytest=False, sandbox_root=None):
    no_deps, pythonpath = setup
    requirements = [] if no_deps else sorted(checkout.glob('requirements*.txt'))
    package = (checkout / 'pyproject.toml').is_file() or (checkout / 'setup.py').is_file()
    home = scratch / (label + '-home')
    home.mkdir()
    env = case_environment(home, pythonpath)
    venv = scratch / (label + '-venv')
    run = run_test
    if sandbox_root is not None:
        venv = home / 'venv'
        run = partial(isolated_test, sandbox_root, (checkout, home), readable=python_inputs(pythonpath), network=True)

    def lacks_pytest(python):
        return pytest and run([python, '-c', 'import pytest'], checkout, env) != 0

    if not requirements and not package and not lacks_pytest(sys.executable):
        return sys.executable
    venv_command = [sys.executable, '-m', 'venv'] + (['--system-site-packages'] if no_deps else []) + [str(venv)]
    if run(venv_command, checkout, env):
        raise RuntimeError('could not create Python environment for ' + label)
    python = str(venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python'))
    for item in requirements:
        if run([python, '-m', 'pip', 'install', '-q', '-r', str(item)], checkout, env):
            raise RuntimeError('could not install Python requirements for ' + label)
    install = ['--no-deps'] if no_deps else []
    if package and run([python, '-m', 'pip', 'install', '-q', *install, '.'], checkout, env):
        raise RuntimeError('could not install Python project for ' + label)
    if lacks_pytest(python) and run([python, '-m', 'pip', 'install', '-q', 'pytest'], checkout, env):
        raise RuntimeError('could not install pytest for ' + label)
    return python


def python_inputs(pythonpath):
    return (Path(__file__).resolve(), sys.prefix, sys.base_prefix, *pythonpath)


def discover(root, checkout, python, path, scratch, label, pythonpath, filename):
    with tempfile.TemporaryDirectory(prefix=f'{label}-discover-', dir=scratch) as home:
        listing = Path(home) / 'cases.json' if root is not None else scratch / filename
        env = case_environment(home if root is not None else scratch, pythonpath)
        command = [python, str(Path(__file__).resolve()), '--list', path, str(listing)]
        if root is None:
            code = run_test(command, checkout, env)
        else:
            code = isolated_test(root, (checkout, home), command, checkout, env,
                                 readable=(*python_inputs(pythonpath), scratch / f'{label}-home'))
        if code:
            raise RuntimeError('could not discover unittest cases in ' + path)
        return read_case_json(listing)


def read_case_json(path):
    if path.is_symlink() or not path.is_file():
        raise NoRepro('test did not produce a regular results file')
    return json.loads(path.read_text(encoding='utf-8'))


def pr_event_applies(root):
    if not os.environ.get('GITHUB_EVENT_NAME', '').startswith('pull_request'):
        return False
    workspace = os.environ.get('GITHUB_WORKSPACE')
    return not workspace or Path(workspace).resolve() == Path(root).resolve()


def metadata(root):
    event = os.environ.get('GITHUB_EVENT_PATH')
    if event and pr_event_applies(root):
        pull = json.loads(Path(event).read_text(encoding='utf-8'))['pull_request']
        return pull['title'], pull.get('body') or '', pull['base']['sha'], pull['head']['sha']
    branch = git(root, 'branch', '--show-current').decode().strip() or (os.environ.get('GITHUB_HEAD_REF') if pr_event_applies(root) else None) or os.environ.get('GITHUB_REF_NAME')
    if not branch:
        print('PR metadata unavailable; the repro uses origin/main as its base.', file=sys.stderr)
        return '', '', '', ''
    try:
        query = subprocess.run(['gh', 'pr', 'view', branch, '--json', 'title,body,baseRefOid,headRefOid'], cwd=root, text=True, capture_output=True, env=clean_environment(), check=False)
    except FileNotFoundError:
        query = None
    if query is None or query.returncode:
        if query is None or 'no pull requests found' not in query.stderr.lower():
            print('PR metadata unavailable; the repro uses origin/main as its base.', file=sys.stderr)
        return '', '', '', ''
    pr = json.loads(query.stdout)
    return pr['title'], pr.get('body') or '', pr['baseRefOid'], pr['headRefOid']


def is_bug_fix(title, branch, body):
    prefix = r'^fix(?:\b|[/_:-])'
    declaration = r'(?im)^\s*(?:bug[ -]?fix\s*:\s*(?:yes|true)|type\s*:\s*bug(?:[ -]?fix)?)\s*$'
    statement = r'(?i)\bfix(?:es|ed)?\s+(?:a\s+|the\s+)?bug\b'
    return bool(re.match(prefix, title, re.IGNORECASE) or re.match(prefix, branch, re.IGNORECASE) or
                re.search(declaration, body) or re.search(statement, body))


def unsupported_test(path):
    file = Path(path)
    assets = {'.json', '.txt', '.csv', '.yaml', '.yml', '.toml', '.xml', '.md', '.rst',
              '.bin', '.png', '.jpg', '.jpeg', '.gif', '.pdf', '.html', '.snap'}
    if file.suffix.lower() in assets or file.suffix.lower() == '.py':
        return False
    parts = set(file.parts[:-1])
    if {'fixtures', 'helpers', 'data', 'testdata', 'resources'} & parts:
        return False
    entry = re.search(r'(?i)^(?:test[_.-])|[._-](?:test|spec)\.', file.name)
    pascal = re.search(r'(?:Test|Tests|Spec)\.[^.]+$', file.name)
    jest = '__tests__' in parts
    rust = file.suffix == '.rs' and 'tests' in parts
    java = file.suffix == '.java' and 'test' in parts
    return bool(entry or pascal or jest or rust or java)


def gate(root, force=False, reason=None, details=None, exercised=False, *, local=True):
    from verify_context import host_name
    root = Path(root).resolve()
    linux = platform.system() == 'Linux'
    isolated = local and exercised and linux
    sandbox_root = root if isolated else None
    evidence = root / '.cold/verify-repro.json'
    report = {'result': 'FAIL', 'tests': [], 'base': None, 'head': None, 'host': host_name(), 'platform': platform.system().lower(), 'opt_out_reason': None}
    try:
        is_repo = subprocess.run(['git', '-C', str(root), 'rev-parse', '--is-inside-work-tree'], capture_output=True, env=clean_environment(), check=False).returncode == 0
        if not is_repo:
            if force or reason or pr_event_applies(root):
                raise NoRepro('a Git checkout is required to reproduce a change')
            return 0
        title, body, base, pr_head = details if details is not None else metadata(root)
        branch = (os.environ.get('GITHUB_HEAD_REF') if pr_event_applies(root) else None) or git(root, 'branch', '--show-current').decode().strip() or os.environ.get('GITHUB_REF_NAME', '')
        fix = force or is_bug_fix(title, branch, body)
        if not fix:
            if reason:
                raise RuntimeError('an opt-out requires a bug-fix PR or --bug-fix')
            return 0
        head = git(root, 'rev-parse', 'HEAD').decode().strip()
        report.update(base=base or None, head=head)
        if pr_head and pr_head != head:
            base = merge_checkout_base(root, base, pr_head, head)
            report['base'] = base
            head = pr_head
            report['head'] = head
        if git(root, 'status', '--porcelain', '--untracked-files=all').strip():
            raise NoRepro('commit all changes before running the repro gate')
        if reason:
            if not reason.strip():
                raise RuntimeError('the opt-out reason cannot be empty')
            report.update(result='INCOMPLETE', opt_out_reason=reason.strip())
            print('INCOMPLETE: repro opted out: ' + reason.strip(), file=sys.stderr)
            return 2
        if not base:
            base = default_base(root)
            report['base'] = base
        for commit in (base, head):
            try:
                git(root, 'cat-file', '-e', commit + '^{commit}')
            except subprocess.CalledProcessError:
                setup_git(root, 'fetch', '--depth=1', 'origin', commit)
        try:
            fork = git(root, 'merge-base', base, head).decode().strip()
        except subprocess.CalledProcessError:
            if git(root, 'rev-parse', '--is-shallow-repository').decode().strip() != 'true':
                raise
            setup_git(root, 'fetch', '--unshallow', 'origin', base, head)
            fork = git(root, 'merge-base', base, head).decode().strip()
        changes = git(root, 'diff', '--name-status', '--find-renames', '--diff-filter=AMR', '-z', fork, head).decode().split('\0')
        paths = {}
        added = set()
        index = 0
        while index < len(changes) and changes[index]:
            status = changes[index]
            index += 1
            old_path = changes[index]
            index += 1
            path = changes[index] if status.startswith('R') else old_path
            index += status.startswith('R')
            paths[path] = old_path
            if status == 'A':
                added.add(path)
        test_files = [p for p in paths if p and TEST_PATH.search(p)]
        files = [p for p in test_files if p.endswith('.py') and (Path(p).name.startswith('test_') or Path(p).name.endswith('_test.py'))]
        tree = set(git(root, 'ls-tree', '-r', '--name-only', '-z', head).decode('utf-8', 'replace').split('\0'))
        candidates = [p for p in paths if p not in files and unsupported_test(p)]
        foreign = {p: runner for p in candidates if (runner := foreign_runner(root, head, tree, p, strict=exercised))}
        unsupported = [p for p in candidates if p not in foreign]
        foreign_selected = []
        for path, runner in foreign.items():
            old = subprocess.run(['git', '-C', str(root), 'show', f'{fork}:{paths[path]}'], capture_output=True, env=clean_environment(), check=False)
            before, before_rest = declared_tests(runner, paths[path], old.stdout.decode('utf-8', 'replace')) if old.returncode == 0 else ({}, '')
            cases, rest = declared_tests(runner, path, git(root, 'show', f'{head}:{path}').decode('utf-8', 'replace'))
            foreign_selected.extend((path, name) for name, chunk in cases.items() if before.get(name) != chunk)
            if rest != before_rest:
                unsupported.append(path)
        roots = {'test', 'tests', '__tests__'}
        test_roots = {Path(*Path(path).parts[:i + 1]) for path in [*files, *foreign]
                      for i, part in enumerate(Path(path).parts[:-1]) if part in roots}
        directories = {Path(p).parent for p in files}
        support = [p for p in paths if p not in files and p not in foreign and
                   (any(Path(p).is_relative_to(root) for root in test_roots) or
                    (p.endswith('.py') and p in added and Path(p).parent in directories and Path(p).parent != Path('.')))]
        cannot_judge = None
        if unsupported:
            kinds = ', '.join(sorted({Path(p).suffix or Path(p).name for p in unsupported}))
            cannot_judge = f'coldverify cannot reproduce {kinds} tests yet'
            if any(Path(path).suffix == '.sh' for path in unsupported):
                cannot_judge += ('. Next: where strict repro can trace the change, replace the unsupported test with '
                                 'a supported test that fails on base, passes on head and hits a changed production line. '
                                 'Strict Linux Bash replay requires a Bash shebang and a 1/0 replay remains unproven '
                                 'for shell behavior')
            else:
                cannot_judge += '; pass --no-repro-reason'
            cannot_judge += f' ({", ".join(unsupported)})'
            if any(path not in foreign and Path(path).suffix in TYPESCRIPT_SUFFIXES for path in unsupported):
                cannot_judge += '. TypeScript repro requires a package that declares Jest or Vitest.'
            if not files and not foreign:
                raise NoRepro(cannot_judge)
        selected = []
        unrunnable = []
        bash_unavailable = []
        unexercised = vitest_untraced = bash_untraced = cargo_untraced = False
        python_unobserved = False
        new_files = set()
        pytest = False
        for path in files:
            source = git(root, 'show', f'{head}:{path}').decode('utf-8')
            old = subprocess.run(['git', '-C', str(root), 'show', f'{fork}:{paths[path]}'], capture_output=True, env=clean_environment(), check=False)
            if old.returncode:
                new_files.add(path)
            before = methods(old.stdout.decode('utf-8')) if old.returncode == 0 else {}
            cases = methods(source)
            pytest = pytest or any('.' not in name for name in cases)
            selected.extend((path, name) for name, node in cases.items() if before.get(name) != node)
        with tempfile.TemporaryDirectory(prefix='cold-repro-', dir=os.environ.get('RUNNER_TEMP') or None) as temporary:
            import evidence_export
            evidence_export.write_marker(temporary, 'coldverify repro', evidence_export.REMOTE_DAYS)
            base_dir = Path(temporary) / 'base'
            head_dir = Path(temporary) / 'head'
            fork_dir = Path(temporary) / 'fork' if fork != base else base_dir
            setup_git(root, 'worktree', 'add', '--detach', str(base_dir), base)
            head_added = False
            fork_added = False
            try:
                setup_git(root, 'worktree', 'add', '--detach', str(head_dir), head)
                head_added = True
                if fork != base:
                    setup_git(root, 'worktree', 'add', '--detach', str(fork_dir), fork)
                    fork_added = True
                scratch = Path(temporary)
                vitest_dependencies(root, base_dir, head_dir,
                                    (path for path, _ in foreign_selected if foreign[path] == 'vitest'))
                setup = repro_setup(head_dir)
                pythonpath = setup[1]
                base_python = project_python(base_dir, scratch, 'base', setup, pytest, sandbox_root) if files else None
                head_python = project_python(head_dir, scratch, 'head', setup, pytest, sandbox_root) if files else None
                fork_python = project_python(fork_dir, scratch, 'fork', setup, pytest, sandbox_root) if fork_added and files else base_python
                runnable = set()
                for index, path in enumerate(files):
                    names = discover(sandbox_root, head_dir, head_python, path, scratch, 'head', pythonpath, f'cases-{index}.json')
                    runnable.update((path, name) for name in names)
                    before = []
                    if path not in new_files:
                        before = discover(sandbox_root, fork_dir, fork_python, paths[path], scratch,
                                          'fork' if fork_added else 'base', pythonpath, f'old-cases-{index}.json')
                    selected.extend((path, name) for name in names if name not in before)
                selected = sorted(set(selected) & runnable) + sorted(set(foreign_selected))
                production = {p: paths[p] for p in paths if p not in test_files and p not in support}
                new_names = None
                changes = changed_lines(root, fork, head, production) if exercised and foreign_selected else {}
                if not selected:
                    raise NoRepro(cannot_judge or NO_TEST_CASES)
                for path in [*files, *foreign, *support]:
                    destination = base_dir / path
                    for parent in destination.relative_to(base_dir).parents:
                        if (base_dir / parent).is_symlink():
                            raise RuntimeError('test file has a symlinked parent: ' + path)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if destination.is_symlink():
                        destination.unlink()
                    mode = git(root, 'ls-tree', head, '--', path).split(b' ', 1)[0]
                    contents = git(root, 'show', f'{head}:{path}')
                    if mode == b'120000':
                        target = os.fsdecode(contents)
                        if not (destination.parent / target).resolve().is_relative_to(base_dir.resolve()):
                            raise RuntimeError('test symlink escapes the base worktree: ' + path)
                        destination.symlink_to(target)
                    elif mode in (b'100644', b'100755'):
                        destination.write_bytes(contents)
                        if mode == b'100755':
                            destination.chmod(destination.stat().st_mode | 0o111)
                    else:
                        raise RuntimeError('unsupported test file mode: ' + path)
                for index, (path, name) in enumerate(selected):
                    runner = foreign.get(path)
                    exits = []
                    outputs = []
                    commands = []
                    missing_file = Path(temporary) / f'missing-{index}.json'
                    hits = False
                    missing = None
                    for label, directory, python in (('base', base_dir, base_python), ('head', head_dir, head_python)):
                        side = label == 'head'
                        with tempfile.TemporaryDirectory(prefix=f'repro-{label}-', dir=temporary) as home:
                            env = case_environment(home, pythonpath)
                            if runner:
                                state = Path(home) if isolated or runner in ('bash', 'vitest') else scratch
                                runner_root = root if runner == 'bash' and linux else sandbox_root
                                side_output = []
                                try:
                                    code, command, hits = run_foreign(runner, directory, path, name, env, state,
                                                                      f'{label}-{index}', side_output, runner_root,
                                                                       changes if exercised and side and exits == [1] else None)
                                except NoRepro as error:
                                    if runner != 'bash':
                                        raise
                                    bash_unavailable.append(str(error))
                                    side_output = [str(error)]
                                    code, command, hits = 2, [*BASH_COMMAND, path], False
                                outputs.extend(side_output)
                                exits.append(code)
                                commands.append(shlex.join(command))
                                continue
                            command = [python, str(Path(__file__).resolve()), '--case', path, name]
                            if not side:
                                if isolated:
                                    missing_file = Path(home) / 'missing.json'
                                env['COLD_REPRO_MISSING'] = str(missing_file)
                            if isolated:
                                readable = (*python_inputs(pythonpath), scratch / f'{label}-home')
                                exits.append(isolated_test(root, (directory, home), command, directory, env, outputs, readable=readable))
                            else:
                                exits.append(run_test(command, directory, env, outputs))
                            if not side and exercised and exits[-1] == 3 and missing_file.is_file():
                                missing = read_case_json(missing_file)
                            commands.append(subprocess.list2cmdline(command) if os.name == 'nt' else shlex.join(command))
                    base_exit, head_exit = exits
                    bash_errors = [side for side, code in zip(('base', 'head'), exits) if code not in (0, 1)] if runner == 'bash' else []
                    if bash_errors:
                        bash_unavailable.append(f'Bash test could not run on {" and ".join(bash_errors)}: {path}')
                    if runner != 'bash':
                        head_exit = 2 if head_exit == 3 else head_exit
                        if base_exit == 3 and missing is not None:
                            new_names = added_names(root, fork, head, production) if new_names is None else new_names
                            base_exit = 3 if all(isinstance(owners, list) and any(name in new_names for name in owners if isinstance(name, str)) for owners in missing) else 2
                        elif base_exit == 3:
                            base_exit = 2
                    if bash_errors:
                        kind = 'unproven'
                    elif base_exit in (1, 3) and head_exit == 0 and (not exercised or hits):
                        kind = 'proof' if base_exit == 1 else 'missing-on-base'
                    elif base_exit in (1, 3) and head_exit == 0:
                        kind = 'unproven'
                        unexercised = True
                        python_unobserved = python_unobserved or runner is None
                        vitest_untraced = vitest_untraced or runner == 'vitest'
                        bash_untraced = bash_untraced or runner == 'bash'
                        cargo_untraced = cargo_untraced or runner == 'cargo'
                    elif base_exit == head_exit == 0:
                        kind = 'acceptance'
                    elif head_exit == 1 or (base_exit in (0, 1) and head_exit != 0):
                        kind = 'regression'
                    elif base_exit == head_exit:
                        kind = 'unchanged'
                    else:
                        kind = 'unproven'
                    if path in foreign and head_exit == 2:
                        unrunnable.append(f'{path}::{name}')
                    report['tests'].append({'name': f'{path}::{name}', 'base_exit': base_exit,
                                            'head_exit': head_exit, 'kind': kind,
                                            'base_output': outputs[0], 'head_output': outputs[1], 'commands': commands})
            finally:
                if fork_added:
                    git(root, 'worktree', 'remove', '--force', str(fork_dir))
                if head_added:
                    git(root, 'worktree', 'remove', '--force', str(head_dir))
                git(root, 'worktree', 'remove', '--force', str(base_dir))
        proof = any(t['kind'] in ('proof', 'missing-on-base') for t in report['tests'])
        regression = any(t['kind'] == 'regression' for t in report['tests'])
        report['result'] = 'PASS' if proof and not regression else 'FAIL'
        for test in report['tests']:
            print(f"{test['name']}: base={test['base_exit']} head={test['head_exit']} ({test['kind']})")
        if bash_unavailable and not regression:
            raise NoRepro('; '.join(dict.fromkeys(bash_unavailable)))
        if unexercised and not proof and not regression:
            raise NoRepro('proof does not exercise the change: no replay established execution of changed production code'
                          + ('; python-coverage-not-runner-observed' if python_unobserved else '')
                          + ('; Vitest needs a prepared coverage provider and a changed line hit beyond collection' if vitest_untraced else '')
                          + ('; Bash replay has no behavior evidence' if bash_untraced else '')
                          + ('; see Cargo trace in the head test output' if cargo_untraced else ''))
        if unrunnable:
            cannot_judge = 'coldverify could not run these tests on head: ' + ', '.join(unrunnable)
        if report['result'] == 'PASS' and cannot_judge:
            raise NoRepro(cannot_judge)
        return 0 if report['result'] == 'PASS' else 1
    except NoRepro as exc:
        report.update(result='INCOMPLETE', error=str(exc))
        print('INCOMPLETE: repro gate: ' + str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, SyntaxError, subprocess.CalledProcessError, RuntimeError) as exc:
        report['error'] = str(exc)
        print('FAIL: repro gate: ' + str(exc), file=sys.stderr)
        return 1
    finally:
        if report['base'] is not None or report['head'] is not None or 'error' in report:
            evidence.parent.mkdir(parents=True, exist_ok=True)
            evidence.parent.joinpath('.gitignore').write_text('*\n')
            evidence.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
            print('Repro gate: ' + report['result'] + f' (evidence: {evidence})', flush=True)


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--case':
        sys.exit(run_case(sys.argv[2], sys.argv[3]))
    if len(sys.argv) == 6 and sys.argv[1] == '--case':
        sys.exit(run_traced_case(*sys.argv[2:]))
    if len(sys.argv) == 4 and sys.argv[1] == '--list':
        sys.exit(list_cases(sys.argv[2], sys.argv[3]))
    raise SystemExit('internal module')
