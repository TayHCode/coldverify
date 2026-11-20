import contextlib
import io
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ci'))
import verify_receipt

DOCS_ONLY = {'claim': ('INCOMPLETE', 'no claim'), 'repro': ('INCOMPLETE', 'repro: no new or changed test cases found'),
             'tests': ('PASS', '9 tests ran fresh and passed'), 'tamper': ('INCOMPLETE', 'assertions of existing tests changed'),
             'risk': ('INCOMPLETE', 'high risk (.github/workflows/verify.yml)')}


def receipt(result='INCOMPLETE', run=True, evidence=None, **checks):
    return {'result': result, 'checks': {**DOCS_ONLY, **checks}, 'run': {'result': 'PASS'} if run else None,
            'repro': evidence or {'opt_out_reason': None}}


def exit_code(*args):
    with contextlib.redirect_stdout(io.StringIO()) as output:
        code = verify_receipt.ci_exit(*args)
    return code, output.getvalue()


class MergeGateExit(unittest.TestCase):
    def test_a_change_without_a_claim_or_proof_passes_and_says_the_receipt_is_incomplete(self):
        code, output = exit_code(receipt(), False)
        self.assertEqual(code, 0)
        self.assertIn('COLDVERIFY CI PASS: the receipt says INCOMPLETE', output)

    def test_a_repro_that_fails_on_a_change_that_is_not_a_bug_fix_passes(self):
        self.assertEqual(exit_code(receipt('FAIL', repro=('FAIL', 'repro tests did not fail on base and pass on head')), False)[0], 0)

    def test_failing_tests_fail(self):
        code, output = exit_code(receipt('FAIL', tests=('FAIL', 'exit 1')), False)
        self.assertEqual(code, 1)
        self.assertIn('COLDVERIFY CI FAIL: tests', output)

    def test_tests_that_did_not_run_fail(self):
        self.assertEqual(exit_code(receipt(run=False, tests=('INCOMPLETE', 'the tests did not run')), False)[0], 1)

    def test_a_recorded_incomplete_suite_fails(self):
        run = receipt(tests=('INCOMPLETE', 'the JUnit XML records no test that ran'))
        run['run'] = {'result': 'INCOMPLETE'}
        self.assertEqual(exit_code(run, False), (1, 'COLDVERIFY CI FAIL: tests\n'))

    def test_failing_flows_fail(self):
        self.assertEqual(exit_code(receipt('FAIL', flows=('FAIL', 'flows failed on head: health')), False)[0], 1)

    def test_declared_flows_that_could_not_run_fail(self):
        self.assertEqual(exit_code(receipt(flows=('INCOMPLETE', 'flows could not run: health')), False)[0], 1)

    def test_a_bug_fix_without_a_proof_fails(self):
        code, output = exit_code(receipt(), True)
        self.assertEqual(code, 1)
        self.assertIn('COLDVERIFY CI FAIL: repro of a bug fix', output)
        self.assertIn('This PR is marked as a fix; add a test that fails without your change and passes with it.', output)
        self.assertIn('title or branch starts with "fix"', output)

    def test_a_bug_fix_whose_repro_fails_fails(self):
        self.assertEqual(exit_code(receipt('FAIL', repro=('FAIL', 'repro tests did not fail on base and pass on head')), True)[0], 1)

    def test_a_bug_fix_that_opted_out_of_the_repro_passes(self):
        opted = receipt(evidence={'opt_out_reason': 'needs hardware'}, repro=('INCOMPLETE', 'repro opted out: needs hardware'))
        self.assertEqual(exit_code(opted, True)[0], 0)

    def test_a_bug_fix_whose_proof_does_not_exercise_a_changed_line_passes_as_compat_does(self):
        unexercised = receipt(evidence={'opt_out_reason': None, 'tests': [{'name': 'pin', 'base_exit': 1, 'head_exit': 0, 'kind': 'unproven'}]},
                              repro=('INCOMPLETE', 'proof does not exercise the change'))
        self.assertEqual(exit_code(unexercised, True)[0], 0)

    def test_a_bug_fix_with_only_acceptance_tests_fails(self):
        accepted = receipt(evidence={'opt_out_reason': None, 'tests': [{'name': 'a', 'base_exit': 0, 'head_exit': 0, 'kind': 'acceptance'}]},
                           repro=('INCOMPLETE', 'proof does not exercise the change'))
        self.assertEqual(exit_code(accepted, True)[0], 1)

    def test_a_bug_fix_with_a_regression_fails_even_with_a_test_that_fails_then_passes(self):
        mixed = receipt(evidence={'opt_out_reason': None, 'tests': [{'name': 'pin', 'base_exit': 1, 'head_exit': 0, 'kind': 'unproven'},
                                                                     {'name': 'old', 'base_exit': 0, 'head_exit': 1, 'kind': 'regression'}]},
                        repro=('INCOMPLETE', 'proof does not exercise the change'))
        self.assertEqual(exit_code(mixed, True)[0], 1)

    def test_a_bug_fix_with_a_proof_passes(self):
        self.assertEqual(exit_code(receipt(repro=('PASS', '1 repro tests failed on base and passed on head')), True)[0], 0)


if __name__ == '__main__':
    unittest.main()
