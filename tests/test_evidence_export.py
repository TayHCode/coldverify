from datetime import datetime, timedelta, timezone
import functools
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

import test_verify_receipt as receipts

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ci'))
import evidence_export
import synthetic_policy
import verify_context
import verify_repro as repro
import verify_repro_transport as transport

SHARED = ROOT / 'docs/coldstack'


@functools.cache
def shared_schemas():
    marker = json.loads((SHARED / 'artifact-marker.schema.json').read_text())
    record_file = marker['properties']['retention']['$ref'].split('#')[0]
    record = json.loads((SHARED / record_file).read_text())
    return {'marker': marker, 'record': record}, Registry().with_resource(record_file, Resource.from_contents(record))


def errors(kind, document):
    schemas, registry = shared_schemas()
    validator = Draft202012Validator(schemas[kind], registry=registry, format_checker=FormatChecker())
    return [f'{list(error.absolute_path)}: {error.message}' for error in validator.iter_errors(document)]


def parse(moment):
    return datetime.strptime(moment, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)


class Export(unittest.TestCase):
    commit = receipts.Receipt.commit
    coldverify = receipts.Receipt.coldverify

    def setUp(self):
        receipts.Receipt.setUp(self)
        self.env = {key: value for key, value in self.env.items() if not key.startswith('GITHUB_')}

    def exported(self, *args, **env):
        subprocess.run(['git', '-C', str(self.repo), 'remote', 'add', 'origin', 'git@github.com:example/project.git'], check=False)
        result, receipt = self.coldverify(*args, **env)
        path = self.repo / '.cold/verify-evidence.json'
        record = json.loads(path.read_text()) if path.is_file() else None
        marker = json.loads((self.repo / '.cold/.cold-artifact.json').read_text())
        self.assertEqual(errors('marker', marker), [])
        if record:
            self.assertEqual(errors('record', record), [])
            self.assertEqual(marker['retention'], record['retention'])
            self.assertEqual(marker['provenance']['run_id'], record['run_id'])
        return result, receipt, record, marker

    def test_a_verified_receipt_exports_a_valid_shared_record_with_every_evidence_field(self):
        before = datetime.now(timezone.utc).replace(microsecond=0)
        result, receipt, record, marker = self.exported('--claim', 'Adds a value')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(record['outcome'], 'pass')
        self.assertNotIn('failure', record)
        self.assertEqual(record['repo'], {'name': 'example/project', 'commit': self.head})
        self.assertEqual(record['claim'], {'statement': 'Adds a value', 'source': 'coldverify --claim'})
        self.assertEqual([step['step'] for step in record['steps']], ['check claim', 'check repro', 'check tests', 'check tamper', 'check criteria'])
        self.assertEqual(record['procedure']['commands'], [step['command'] for step in receipt['procedure']])
        self.assertEqual((record['environment']['host'], record['environment']['isolation']),
                         (receipt['environment']['host'], receipt['environment']['isolation']))
        self.assertEqual([artifact['path'] for artifact in record['artifacts']],
                         ['.cold/verify-repro.json', '.cold/verify-result.json', '.cold/verify-receipt.json'])
        for artifact in record['artifacts']:
            self.assertEqual(artifact['sha256'], hashlib.sha256((self.repo / artifact['path']).read_bytes()).hexdigest())
        self.assertEqual((record['retention']['owner'], record['retention']['policy']), ('coldverify', 'temporary'))
        self.assertNotIn('preserved_at', record['retention'])
        created = parse(marker['provenance']['created_at'])
        self.assertGreaterEqual(created, before)
        self.assertEqual(parse(record['retention']['expires']) - created, timedelta(days=14))
        self.assertEqual(marker['provenance']['repo'], record['repo'])

    def test_an_explicit_claim_is_never_attributed_to_the_pull_request(self):
        for args, source in ((('--claim', 'Typed by hand'), 'coldverify --claim'), (('--claim', 'From the PR'), 'coldverify --claim'),
                             ((), 'pull request')):
            with self.subTest(source=source):
                _, receipt, record, _ = self.exported(*args, FAKE_GH_BODY='## Claim\nFrom the PR\n')
                self.assertEqual(receipt['pull_request'], 7)
                self.assertEqual(record['claim'], {'statement': receipt['claim'], 'source': source})

    def test_an_unwritable_record_keeps_the_verdict_exit_code(self):
        transport = self.tool / 'ci/verify_repro_transport.py'
        transport.write_text(receipts.FAKE_TRANSPORT.replace('    return int(', "    (Path(root) / '.cold/verify-evidence.json').mkdir()\n    return int("))
        result, receipt = self.coldverify('--claim', 'Adds a value', GITHUB_REPOSITORY='example/project')
        self.assertEqual((result.returncode, receipt['result']), (0, 'PASS'), result.stdout + result.stderr)
        self.assertIn('the shared evidence record was not written', result.stderr)

    def test_a_failed_receipt_exports_a_product_failure(self):
        result, receipt, record, _ = self.exported('--claim', 'Adds a value', FAKE_RECIPE_EXIT='1')
        self.assertEqual((result.returncode, receipt['result']), (1, 'FAIL'))
        self.assertEqual(record['outcome'], 'fail')
        self.assertEqual(record['failure'], {'class': 'product', 'cause': 'test_failure', 'detail': receipt['reason']})
        failed = [step for step in record['steps'] if step['status'] == 'fail']
        self.assertEqual([step['step'] for step in failed], ['check tests'])
        self.assertEqual(failed[0]['failing_excerpt'], receipt['checks']['tests'][1])

    def test_a_wrong_repro_is_a_behavior_mismatch(self):
        evidence = {**self.repro_pass, 'result': 'FAIL', 'tests': [{'name': 't', 'base_exit': 0, 'head_exit': 0}]}
        _, receipt, record, _ = self.exported('--claim', 'Adds a value', FAKE_REPRO_CODE='1', FAKE_REPRO_EVIDENCE=json.dumps(evidence))
        self.assertEqual((receipt['result'], receipt['checks']['repro'][0], receipt['checks']['criteria'][0]), ('FAIL', 'FAIL', 'INCOMPLETE'))
        self.assertEqual(record['outcome'], 'incomplete')
        answered = {**receipt, 'checks': {**receipt['checks'], 'criteria': ['PASS', 'covered']}}
        record = evidence_export.evidence_record(answered, 'example/project', self.repo, record['run_id'], record['retention'], 'coldverify --claim')
        self.assertEqual(errors('record', record), [])
        self.assertEqual((record['outcome'], record['failure']['cause']), ('fail', 'behavior_mismatch'))

    def test_an_incomplete_receipt_is_never_exported_as_pass_or_fail(self):
        result, receipt, record, _ = self.exported()
        self.assertEqual((result.returncode, receipt['result']), (2, 'INCOMPLETE'))
        self.assertEqual((record['outcome'], record['failure']['class'], record['failure']['cause']),
                         ('incomplete', 'harness', 'skipped_step'))
        self.assertNotIn('claim', record)

    def test_a_failure_beside_an_unanswered_check_follows_the_shared_rule_and_is_incomplete(self):
        evidence = {**self.repro_pass, 'result': 'FAIL', 'tests': [{'name': 't', 'base_exit': 0, 'head_exit': 0}]}
        _, receipt, record, _ = self.exported(FAKE_REPRO_CODE='1', FAKE_REPRO_EVIDENCE=json.dumps(evidence))
        self.assertEqual(receipt['result'], 'FAIL')
        self.assertEqual(record['outcome'], 'incomplete')

    def test_an_actions_run_claims_no_preserved_copy(self):
        event = self.repo.parent / 'event.json'
        for private in (True, False):
            with self.subTest(private=private):
                event.write_text(json.dumps({'repository': {'private': private}}))
                _, _, record, marker = self.exported('--claim', 'Adds a value', GITHUB_STEP_SUMMARY=str(self.repo.parent / 'summary.md'),
                                                     GITHUB_EVENT_PATH=str(event), GITHUB_EVENT_NAME='push',
                                                     GITHUB_REPOSITORY='example/project', GITHUB_SERVER_URL='https://github.com',
                                                     GITHUB_RUN_ID='42')
                self.assertNotIn('preserved_at', record['retention'])
                self.assertNotIn('preserved_at', marker['retention'])

    def test_without_a_repository_name_only_the_receipt_and_marker_are_written(self):
        result, receipt = self.coldverify('--claim', 'Adds a value')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.repo / '.cold/verify-evidence.json').exists())
        marker = json.loads((self.repo / '.cold/.cold-artifact.json').read_text())
        self.assertEqual(errors('marker', marker), [])
        self.assertNotIn('repo', marker['provenance'])

    def test_a_stale_evidence_record_never_survives_a_later_run(self):
        self.exported('--claim', 'Adds a value')
        subprocess.run(['git', '-C', str(self.repo), 'remote', 'remove', 'origin'], check=True)
        self.coldverify('--claim', 'Adds a value')
        self.assertFalse((self.repo / '.cold/verify-evidence.json').exists())

    def test_a_compat_run_marks_its_evidence_directory_but_writes_no_record(self):
        result = subprocess.run([sys.executable, str(self.tool / 'coldverify'), '--ci', '--compat'], cwd=self.repo,
                                env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.repo / '.cold/verify-result.json').is_file())
        self.assertFalse((self.repo / '.cold/verify-evidence.json').exists())
        marker = json.loads((self.repo / '.cold/.cold-artifact.json').read_text())
        self.assertEqual(errors('marker', marker), [])
        self.assertEqual(marker['retention']['owner'], 'coldverify')


class RemoteMarkers(unittest.TestCase):
    def setUp(self):
        host_marker = patch.object(verify_context, 'COLDBOX_HOST_FILE', Path('/nonexistent/coldbox/host'))
        host_marker.start()
        self.addCleanup(host_marker.stop)
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in ('COLD_VERIFY_BACKEND', 'COLD_VERIFY_HOST'):
            os.environ.pop(name, None)
        os.environ['COLD_VERIFY_BACKEND'] = 'ssh:runner-a'

    def test_the_staged_repro_directory_carries_a_short_lived_coldverify_marker(self):
        markers = []

        def tar(command, **_):
            markers.append(json.loads((Path(command[4]) / '.cold-artifact.json').read_text()))
            return unittest.mock.MagicMock()

        with patch.object(transport, 'prepare', return_value={'details': ['fix', '', 'base', 'head'], 'checkout': 'head'}), \
             patch.object(transport.subprocess, 'check_output', return_value='linux\n'), \
             patch.object(transport.platform, 'node', return_value='control-a'), \
             patch.object(transport.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')), \
             patch.object(transport.subprocess, 'Popen', side_effect=tar), \
             patch.object(transport.verify_repro, 'setup_git'), \
             patch.object(transport.shutil, 'rmtree'):
            transport.run(Path.cwd())
        self.assertEqual(len(markers), 1)
        marker = markers[0]
        self.assertEqual(errors('marker', marker), [])
        retention = marker['retention']
        self.assertEqual((retention['owner'], retention['policy']), ('coldverify', 'temporary'))
        self.assertEqual(parse(retention['expires']) - parse(marker['provenance']['created_at']), timedelta(days=1))

    def test_the_repro_working_directory_carries_the_marker_while_the_cases_run(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as out:
            root = Path(tmp)
            seen = Path(out) / 'seen.json'

            def git(*args):
                return subprocess.check_output(['git', '-C', tmp, *args], stderr=subprocess.DEVNULL, text=True).strip()
            git('init', '-q', '-b', 'main')
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@localhost')
            (root / '.gitignore').write_text('.cold/\n')
            (root / 'value.py').write_text('VALUE = 0\n')
            git('add', '.')
            git('commit', '-qm', 'base')
            base = git('rev-parse', 'HEAD')
            (root / 'value.py').write_text('VALUE = 1\n')
            (root / 'tests').mkdir()
            (root / 'tests/test_value.py').write_text(
                'import json, unittest\nfrom pathlib import Path\nfrom value import VALUE\n'
                'class Check(unittest.TestCase):\n def test_value(self):\n'
                '  marker = (Path(__file__).resolve().parents[2] / ".cold-artifact.json").read_text()\n'
                f'  Path({str(seen)!r}).write_text(marker)\n'
                '  self.assertEqual(json.loads(marker)["retention"]["owner"], "coldverify")\n  self.assertEqual(VALUE, 1)\n')
            git('add', '.')
            git('commit', '-qm', 'fix')
            head = git('rev-parse', 'HEAD')
            with patch.object(repro, 'metadata', return_value=('fix fixture', '', base, head)):
                self.assertEqual(repro.gate(root), 0)
            evidence = json.loads((root / '.cold/verify-repro.json').read_text())
            self.assertEqual([(test['base_exit'], test['head_exit']) for test in evidence['tests']], [(1, 0)])
            marker = json.loads(seen.read_text())
        self.assertEqual(errors('marker', marker), [])
        self.assertEqual(parse(marker['retention']['expires']) - parse(marker['provenance']['created_at']), timedelta(days=1))

    def test_the_dispatched_linux_directory_carries_a_short_lived_coldverify_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / 'bin').mkdir()
            (base / 'sitecustomize.py').write_text("import os, platform\nplatform.node = lambda: os.environ.get('FIXTURE_NODE', 'fixture-controller')\n"
                                                  + synthetic_policy.SHIM)
            ssh = base / 'bin/ssh'
            ssh.write_text(f'#!/usr/bin/env bash\nexport FIXTURE_NODE=runner-a\nexport PYTHONPATH="{base}"\n'
                           'while [[ $1 == -* ]]; do shift 2; done\nshift\n'
                           f'case "$1" in "rm -rf /tmp/cold-verify-"*) cp "${{1#rm -rf }}/.cold-artifact.json" {base}/seen.json ;; esac\n'
                           'exec bash -c "$1"\n')
            ssh.chmod(0o755)
            root = base / 'repo'
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / '.fleet').mkdir()
            (root / '.fleet/verify.sh').write_text('true\n')
            env = {**os.environ, 'GITHUB_REPOSITORY': 'example-org/native-tool', 'PATH': f'{base / "bin"}:{os.environ["PATH"]}',
                   'COLD_VERIFY_BACKEND': 'ssh:runner-a', 'COLDBOX_GUEST': '0', 'PYTHONPATH': str(base)}
            result = subprocess.run([sys.executable, str(ROOT / 'ci/linux-verify.py'), str(root)],
                                    env=env, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            marker = json.loads((base / 'seen.json').read_text())
        self.assertEqual(errors('marker', marker), [])
        self.assertEqual(marker['retention']['owner'], 'coldverify')
        self.assertEqual(parse(marker['retention']['expires']) - parse(marker['provenance']['created_at']), timedelta(days=1))


if __name__ == '__main__':
    unittest.main()
