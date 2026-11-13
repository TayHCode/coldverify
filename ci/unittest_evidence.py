#!/usr/bin/env python3
"""Run unittest discovery and report methods that were not skipped."""
import sys
import unittest

from junit_unittest import JUnitResult, take_junit_environment


class ExecutionResult(JUnitResult):
    skipped_cases = 0

    def addSkip(self, test, reason):
        if isinstance(test, unittest.case.TestCase) and not hasattr(test, 'test_case'):
            self.skipped_cases += 1
        super().addSkip(test, reason)


def cases(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from cases(test)
        else:
            yield test


def main(starts):
    ExecutionResult.junit_dir, ExecutionResult.junit_key = take_junit_environment()
    discovered = {}
    for start in starts:
        for test in cases(unittest.TestLoader().discover(start, pattern='test*.py')):
            discovered.setdefault(test.id(), test)
    suite = unittest.TestSuite(discovered.values())
    result = unittest.TextTestRunner(verbosity=2, resultclass=ExecutionResult).run(suite)
    executed = result.testsRun - result.skipped_cases
    print(f'GENERIC_EXECUTED_TESTS={executed}')
    return 0 if result.wasSuccessful() and executed > 0 else 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:] or ['.']))
