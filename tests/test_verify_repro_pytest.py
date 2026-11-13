import unittest

from repro_gate import ReproGate


class UnsupportedLanguageIsIncomplete(unittest.TestCase):
    RED_GREEN = 'import unittest\nfrom value import VALUE\nclass Check(unittest.TestCase):\n def test_value(self):\n  self.assertEqual(VALUE, 1)\n'
    GREEN_ON_BASE = 'import unittest\nclass Check(unittest.TestCase):\n def test_value(self):\n  self.assertTrue(True)\n'
    JS_TEST = 'require("node:test")\n'

    gate = ReproGate.gate

    def test_a_js_test_change_is_incomplete_and_names_the_opt_out(self):
        code, receipt = self.gate({'test/a.test.js': self.JS_TEST})
        self.assertEqual((code, receipt['result']), (2, 'INCOMPLETE'))
        self.assertIn('coldverify cannot reproduce .js tests yet; pass --no-repro-reason', receipt['error'])
        self.assertIn('test/a.test.js', receipt['error'])

    def test_every_unsupported_kind_is_named(self):
        code, receipt = self.gate({'pkg/calc_test.go': 'package calc\n', 'tests/integration.rs': '#[test]\nfn it() {}\n'})
        self.assertEqual((code, receipt['result']), (2, 'INCOMPLETE'))
        self.assertIn('cannot reproduce .go, .rs tests yet', receipt['error'])

    def test_a_python_repro_that_passes_beside_a_js_test_is_still_not_a_pass(self):
        code, receipt = self.gate({'test_value.py': self.RED_GREEN, 'test/a.test.js': self.JS_TEST})
        self.assertEqual((code, receipt['result']), (2, 'INCOMPLETE'))
        self.assertEqual([(t['base_exit'], t['head_exit']) for t in receipt['tests']], [(1, 0)])
        self.assertIn('cannot reproduce .js tests yet', receipt['error'])

    def test_a_python_repro_that_fails_beside_a_js_test_is_still_a_failure(self):
        code, receipt = self.gate({'test_value.py': self.GREEN_ON_BASE, 'test/a.test.js': self.JS_TEST})
        self.assertEqual((code, receipt['result']), (1, 'FAIL'))
        self.assertEqual([(t['base_exit'], t['head_exit']) for t in receipt['tests']], [(0, 0)])

    def test_a_python_repro_that_fails_alone_is_still_a_failure(self):
        code, receipt = self.gate({'test_value.py': self.GREEN_ON_BASE})
        self.assertEqual((code, receipt['result']), (1, 'FAIL'))
