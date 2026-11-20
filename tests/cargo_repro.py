import shutil
import unittest
from unittest.mock import patch

from test_verify_repro_languages import FIXTURES, fixture_gate, outcomes


class CargoRepro(unittest.TestCase):
    def test_strict_cargo_proves_a_changed_rust_line(self):
        code, receipt = fixture_gate('cargo', exercised=True)
        self.assertEqual((code, receipt['result']), (0, 'PASS'), receipt)
        self.assertEqual(outcomes(receipt), [('tests/add.rs::adds', 1, 0, 'proof')])
        self.assertIn('Cargo trace: ran src/lib.rs:2', receipt['tests'][0]['head_output'])

    def test_strict_cargo_rejects_coverage_of_unchanged_rust_lines(self):
        cargo = FIXTURES / 'cargo'
        helper = '\npub fn value() -> &\'static str {\n    include_str!("value.txt").trim()\n}\n'
        code, receipt = fixture_gate(
            'cargo', exercised=True,
            base_extra={'src/lib.rs': (cargo / 'base/src/lib.rs').read_text() + helper, 'src/value.txt': 'old'},
            extra={'src/lib.rs': (cargo / 'head/src/lib.rs').read_text() + helper, 'src/value.txt': 'new',
                   'tests/add.rs': '#[test]\nfn adds() {\n    assert_eq!(calc::value(), "new");\n}\n'},
        )
        self.assertEqual((code, receipt['result']), (2, 'INCOMPLETE'), receipt)
        self.assertEqual(outcomes(receipt), [('tests/add.rs::adds', 1, 0, 'unproven')])
        self.assertIn('Cargo trace: no changed Rust line ran', receipt['tests'][0]['head_output'])

    def test_strict_cargo_does_not_credit_a_changed_line_run_only_at_startup(self):
        test = '''extern "C" fn startup() {
    std::hint::black_box(calc::add(2, 3));
}

#[cfg(target_os = "linux")]
#[used]
#[link_section = ".init_array"]
static STARTUP: extern "C" fn() = startup;

#[test]
fn adds() {
    assert_eq!(include_str!("../value.txt"), "new");
}
'''
        code, receipt = fixture_gate('cargo', exercised=True, base_extra={'value.txt': 'old'},
                                     extra={'value.txt': 'new', 'tests/add.rs': test})
        self.assertEqual((code, receipt['result']), (2, 'INCOMPLETE'), receipt)
        self.assertEqual(outcomes(receipt), [('tests/add.rs::adds', 1, 0, 'unproven')])
        self.assertIn('Cargo trace: no changed Rust line ran in the selected test', receipt['tests'][0]['head_output'])

    def test_strict_cargo_without_llvm_tools_keeps_the_replay_unproven(self):
        which = shutil.which
        with patch.object(shutil, 'which', side_effect=lambda name, *args, **kwargs:
                          None if name in ('llvm-profdata', 'llvm-cov') else which(name, *args, **kwargs)):
            code, receipt = fixture_gate('cargo', exercised=True)
        self.assertEqual((code, receipt['result']), (2, 'INCOMPLETE'), receipt)
        self.assertEqual(outcomes(receipt), [('tests/add.rs::adds', 1, 0, 'unproven')])
        self.assertIn('Cargo trace: LLVM coverage tools are unavailable', receipt['tests'][0]['head_output'])
