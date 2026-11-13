import unittest

from repro_gate import ReproGate


class PytestFunctionsReproduce(unittest.TestCase):
    gate = ReproGate.gate

    def outcome(self, files):
        code, receipt = self.gate(files)
        return code, receipt['result'], [(t['name'], t['base_exit'], t['head_exit']) for t in receipt['tests']]

    def test_pytest_raises_that_does_not_raise_on_base_is_a_reproduction(self):
        source = 'import pytest\nfrom value import VALUE\ndef test_value():\n    with pytest.raises(ZeroDivisionError):\n        1 / (VALUE - 1)\n'
        self.assertEqual(self.outcome({'test_value.py': source}), (0, 'PASS', [('test_value.py::test_value', 1, 0)]))

    def test_an_xfail_function_proves_nothing(self):
        source = 'import pytest\nfrom value import VALUE\n@pytest.mark.xfail\ndef test_value():\n    assert VALUE == 1\n'
        self.assertEqual(self.outcome({'test_value.py': source}), (1, 'FAIL', [('test_value.py::test_value', 2, 2)]))

    def test_an_xfail_whose_condition_does_not_apply_still_reproduces(self):
        source = 'import pytest\nfrom value import VALUE\n@pytest.mark.xfail(False, reason="other platforms")\ndef test_value():\n    assert VALUE == 1\n'
        self.assertEqual(self.outcome({'test_value.py': source}), (0, 'PASS', [('test_value.py::test_value', 1, 0)]))
