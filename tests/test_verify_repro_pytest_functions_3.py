import unittest

from repro_gate import ReproGate


class PytestFunctionsReproduce(unittest.TestCase):
    gate = ReproGate.gate

    def outcome(self, files):
        code, receipt = self.gate(files)
        return code, receipt['result'], [(t['name'], t['base_exit'], t['head_exit']) for t in receipt['tests']]

    def test_a_strict_xfail_that_passes_on_head_proves_nothing(self):
        source = 'import pytest\nfrom value import VALUE\n@pytest.mark.xfail(strict=True)\ndef test_value():\n    assert VALUE == 1\n'
        self.assertEqual(self.outcome({'test_value.py': source}), (1, 'FAIL', [('test_value.py::test_value', 2, 2)]))

    def test_a_parametrized_function_is_one_repro_test(self):
        source = 'import pytest\nfrom value import VALUE\n@pytest.mark.parametrize("expected", [1, 1.0])\ndef test_value(expected):\n    assert VALUE == expected\n'
        self.assertEqual(self.outcome({'tests/test_value.py': source}), (0, 'PASS', [('tests/test_value.py::test_value', 1, 0)]))
