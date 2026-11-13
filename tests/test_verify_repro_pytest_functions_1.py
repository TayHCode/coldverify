import unittest

from repro_gate import ReproGate


class PytestFunctionsReproduce(unittest.TestCase):
    gate = ReproGate.gate

    def outcome(self, files):
        code, receipt = self.gate(files)
        return code, receipt['result'], [(t['name'], t['base_exit'], t['head_exit']) for t in receipt['tests']]

    def test_a_function_that_fails_on_base_and_passes_on_head_is_a_pass(self):
        self.assertEqual(self.outcome({'test_value.py': 'from value import VALUE\ndef test_value():\n    assert VALUE == 1\n'}),
                         (0, 'PASS', [('test_value.py::test_value', 1, 0)]))

    def test_a_function_that_passes_on_base_is_a_failure(self):
        self.assertEqual(self.outcome({'test_value.py': 'def test_value():\n    assert True\n'}),
                         (1, 'FAIL', [('test_value.py::test_value', 0, 0)]))

    def test_an_error_on_base_is_not_a_reproduction(self):
        files = {'value.py': 'VALUE = 1\nONE = 1\n', 'test_value.py': 'import value\ndef test_value():\n    assert value.VALUE == value.ONE\n'}
        self.assertEqual(self.outcome(files), (1, 'FAIL', [('test_value.py::test_value', 2, 0)]))
