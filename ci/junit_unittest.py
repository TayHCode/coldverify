#!/usr/bin/env python3
"""Run unittest and optionally write a JUnit XML report into $COLD_VERIFY_JUNIT_DIR.

    junit_unittest.py [unittest arguments, e.g. discover -s tests -p 'test*.py']

coldverify counts tests only from that XML, signed with the per-run key in $COLD_VERIFY_JUNIT_KEY. Both variables
are removed before any test runs, so suites that start their own unittest runs do not add to the count. Exit 1 for an unsuccessful run, 5 for an empty suite,
and 0 otherwise, including a skipped class. Docs: docs/verify-trace.md
"""
import hashlib
import hmac
import os
import sys
import unittest
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

JUNIT_ENV = 'COLD_VERIFY_JUNIT_DIR'
JUNIT_KEY_ENV = 'COLD_VERIFY_JUNIT_KEY'
SIGNATURE_SUFFIX = '.sig'


def signature(key, data):
    return hmac.new(key.encode(), data, hashlib.sha256).hexdigest()


def sign(path, key):
    path = Path(path)
    if key and path.is_file():
        path.with_name(path.name + SIGNATURE_SUFFIX).write_text(signature(key, path.read_bytes()), encoding='ascii')


def take_junit_environment():
    return os.environ.pop(JUNIT_ENV, None), os.environ.pop(JUNIT_KEY_ENV, None)


def real_case(test):
    return isinstance(test, unittest.TestCase) and not hasattr(test, 'test_case')


class JUnitResult(unittest.TextTestResult):
    junit_dir = None
    junit_key = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cases = {}

    def case(self, test):
        return self.cases.setdefault(test.id(), {'outcome': None, 'message': '', 'subtest_passed': False, 'subtest_skips': []})

    def outcome(self, test, outcome, message=''):
        case = self.case(getattr(test, 'test_case', test))
        if case['outcome'] not in ('failure', 'error'):
            case.update(outcome=outcome, message=message)

    def startTest(self, test):
        self.case(test)
        super().startTest(test)

    def addError(self, test, err):
        self.outcome(test, 'error', self._exc_info_to_string(err, test))
        super().addError(test, err)

    def addFailure(self, test, err):
        self.outcome(test, 'failure', self._exc_info_to_string(err, test))
        super().addFailure(test, err)

    def addSubTest(self, test, subtest, err):
        if err is None:
            self.case(test)['subtest_passed'] = True
        else:
            kind = 'failure' if issubclass(err[0], test.failureException) else 'error'
            self.outcome(test, kind, self._exc_info_to_string(err, test))
        super().addSubTest(test, subtest, err)

    def addSkip(self, test, reason):
        if real_case(test):
            self.outcome(test, 'skipped', reason)
        elif hasattr(test, 'test_case'):
            self.case(test.test_case)['subtest_skips'].append(reason)
        super().addSkip(test, reason)

    def stopTest(self, test):
        case = self.case(test)
        if case['outcome'] is None and case['subtest_skips'] and not case['subtest_passed']:
            case.update(outcome='skipped', message=case['subtest_skips'][0])
        super().stopTest(test)

    def addUnexpectedSuccess(self, test):
        self.outcome(test, 'failure', 'unexpected success')
        super().addUnexpectedSuccess(test)

    def stopTestRun(self):
        super().stopTestRun()
        if self.junit_dir:
            path = Path(self.junit_dir) / f'unittest-{uuid.uuid4().hex}.xml'
            write(self.cases, path)
            sign(path, self.junit_key)


def write(cases, path):
    suite = ET.Element('testsuite', name='unittest', tests=str(len(cases)))
    for attribute, outcome in (('failures', 'failure'), ('errors', 'error'), ('skipped', 'skipped')):
        suite.set(attribute, str(sum(case['outcome'] == outcome for case in cases.values())))
    for identifier, case in cases.items():
        classname, _, name = identifier.rpartition('.')
        element = ET.SubElement(suite, 'testcase', classname=classname, name=name)
        if case['outcome']:
            ET.SubElement(element, case['outcome'], message=case['message'][:500]).text = case['message']
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(suite).write(path, encoding='utf-8', xml_declaration=True)


class JUnitRunner(unittest.TextTestRunner):
    resultclass = JUnitResult


def main(argv=None):
    JUnitResult.junit_dir, JUnitResult.junit_key = take_junit_environment()
    sys.path[0] = os.getcwd()
    program = unittest.main(module=None, argv=['python3 -m unittest', *(sys.argv[1:] if argv is None else argv)],
                            testRunner=JUnitRunner, exit=False)
    result = program.result
    sys.exit(1 if not result.wasSuccessful() else 5 if not result.testsRun and not result.skipped else 0)


if __name__ == '__main__':
    main()
