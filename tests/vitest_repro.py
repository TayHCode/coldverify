import os
import unittest

from test_verify_repro_languages import fixture_gate, outcomes


class VitestRepro(unittest.TestCase):
    def test_strict_vitest_traces_the_changed_typescript_line(self):
        code, receipt = fixture_gate('vitest', exercised=True, node_modules=os.environ['COLD_TEST_VITEST_MODULES'])
        self.assertEqual((code, receipt['result']), (0, 'PASS'), receipt)
        self.assertEqual(outcomes(receipt), [('test/calc.test.ts::adds two numbers', 1, 0, 'proof')])

    def test_strict_vitest_does_not_credit_a_changed_line_executed_only_at_import(self):
        source = 'export function add(a: number, b: number) {\n  return a %s b;\n}\nexport const sum = add(1, 2);\n'
        test = 'import { expect, it } from "vitest";\nimport { sum } from "../calc";\nit("adds", () => { expect(sum).toBe(3); });\n'
        code, receipt = fixture_gate('vitest', exercised=True, base_extra={'calc.ts': source % '-'},
                                     extra={'calc.ts': source % '+', 'test/calc.test.ts': test},
                                     node_modules=os.environ['COLD_TEST_VITEST_MODULES'])
        self.assertEqual((code, receipt['result']), (2, 'INCOMPLETE'), receipt)
        self.assertEqual(outcomes(receipt), [('test/calc.test.ts::adds', 1, 0, 'unproven')])
