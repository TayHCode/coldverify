import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ci'))
import synthetic_policy
import verify_context
import verify_criteria
import verify_receipt

PASSED = {'claim': ('PASS', 'claim stated'), 'repro': ('PASS', 'repro red on base, green on head'),
          'tests': ('PASS', '3 tests ran fresh and passed'), 'tamper': ('PASS', 'no tampering')}
GUEST_CRITERIA = ('INCOMPLETE', verify_criteria.GUEST)


class GuestVerdict(unittest.TestCase):
    def setUp(self):
        synthetic_policy.use(self.addCleanup)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.marker = Path(folder.name) / 'host'
        patcher = mock.patch.object(verify_context, 'COLDBOX_HOST_FILE', self.marker)
        patcher.start()
        self.addCleanup(patcher.stop)
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop('COLDBOX_GUEST', None)

    def in_guest(self):
        self.marker.write_text('runner-a\n')

    def test_in_a_guest_the_criteria_pointer_does_not_block_a_pass_and_the_verdict_says_so(self):
        self.in_guest()
        result, reason = verify_receipt.combine({**PASSED, 'criteria': GUEST_CRITERIA})
        self.assertEqual(result, 'PASS')
        self.assertIn('criteria not evaluated in a CI guest', reason)
        self.assertIn('"coldverify criteria" check', reason)

    def test_in_a_guest_the_verdict_says_the_builder_identity_was_not_checked(self):
        self.in_guest()
        result, reason = verify_receipt.combine({**PASSED, 'criteria': GUEST_CRITERIA})
        self.assertEqual(result, 'PASS')
        self.assertIn('builder identity was not checked', reason)
        self.assertIn('"Builder-Model:" lines', reason)
        result, reason = verify_receipt.combine({**PASSED, 'tests': ('FAIL', 'tests broke'), 'criteria': GUEST_CRITERIA})
        self.assertEqual(result, 'FAIL')
        self.assertIn('builder identity was not checked', reason)

    def test_in_a_guest_every_other_check_still_counts(self):
        self.in_guest()
        for name, verdict in (('tests', 'INCOMPLETE'), ('repro', 'FAIL'), ('risk', 'INCOMPLETE')):
            with self.subTest(check=name):
                checks = {**PASSED, name: (verdict, f'{name} broke'), 'criteria': GUEST_CRITERIA}
                result, reason = verify_receipt.combine(checks)
                self.assertEqual(result, verdict)
                self.assertIn(f'{name} broke', reason)
                self.assertIn('criteria not evaluated in a CI guest', reason)

    def test_in_a_guest_any_other_criteria_result_still_counts(self):
        self.in_guest()
        for criteria in (('INCOMPLETE', 'acceptance criteria no proof test covers: C1'), ('FAIL', verify_criteria.GUEST)):
            with self.subTest(criteria=criteria):
                result, reason = verify_receipt.combine({**PASSED, 'criteria': criteria})
                self.assertEqual(result, criteria[0])
                self.assertNotIn('criteria not evaluated in a CI guest', reason)

    def test_outside_a_guest_the_same_criteria_result_blocks_a_pass(self):
        result, reason = verify_receipt.combine({**PASSED, 'criteria': GUEST_CRITERIA})
        self.assertEqual(result, 'INCOMPLETE')
        self.assertNotIn('criteria not evaluated in a CI guest', reason)

    def test_the_environment_cannot_turn_a_host_into_a_guest(self):
        for value in ('1', 'true', 'runner-a'):
            with self.subTest(COLDBOX_GUEST=value):
                os.environ['COLDBOX_GUEST'] = value
                self.assertEqual(verify_receipt.combine({**PASSED, 'criteria': GUEST_CRITERIA})[0], 'INCOMPLETE')

    def test_the_environment_can_only_turn_a_guest_back_into_a_host(self):
        self.in_guest()
        os.environ['COLDBOX_GUEST'] = '0'
        self.assertEqual(verify_receipt.combine({**PASSED, 'criteria': GUEST_CRITERIA})[0], 'INCOMPLETE')

    def test_a_marker_naming_no_listed_host_is_not_a_guest(self):
        self.marker.write_text('laptop\n')
        self.assertEqual(verify_receipt.combine({**PASSED, 'criteria': GUEST_CRITERIA})[0], 'INCOMPLETE')


if __name__ == '__main__':
    unittest.main()
