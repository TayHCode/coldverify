import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHIPPED = ['ci', 'scripts', 'tests', '.fleet', '.github', 'coldverify', 'pyproject.toml', 'README.md']
RETIRED = re.compile(r'protect-independent|active-repos|windows/verify\.ps1|test-verify\.ps1'
                     r'|\bonboard\b|\brun_result\b|run-result|verify-exceptions|verify-hosted|verify-macos')


def shipped_files():
    for name in SHIPPED:
        path = ROOT / name
        for file in [path] if path.is_file() else sorted(path.rglob('*')):
            if file.is_file() and '__pycache__' not in file.parts and file != Path(__file__).resolve():
                yield file


class RetiredFleetCode(unittest.TestCase):
    def test_no_shipped_file_references_retired_fleet_code(self):
        files = list(shipped_files())
        self.assertGreater(len(files), 50)
        hits = [f'{file.relative_to(ROOT)}:{number}: {line.strip()}'
                for file in files
                for number, line in enumerate(file.read_text(errors='replace').splitlines(), 1)
                if RETIRED.search(line)]
        self.assertEqual(hits, [])

    def test_retired_files_are_gone(self):
        for name in ('ci/onboard.py', 'ci/run_result.py', 'ci/run-result.schema.json', 'ci/verify-exceptions.json',
                     'ci/templates/verify-hosted.yml', 'ci/templates/verify-macos.yml',
                     'ci/templates/verify-coldbox.yml'):
            self.assertFalse((ROOT / name).exists(), name)
        self.assertEqual(sorted((ROOT / 'ci').rglob('*.ps1')), [])

    def test_policy_helpers_fit_in_a_small_module(self):
        lines = (ROOT / 'ci/verify_policy.py').read_text().splitlines()
        self.assertLess(len(lines), 100)


if __name__ == '__main__':
    unittest.main()
