import ast
import re
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
CLOCK = re.compile(r'monotonic|perf_counter|time\.time')
TIMING = re.compile(r'monotonic|perf_counter|time\.time|elapsed|duration|_ms\b|_s\b|seconds')


def compared(test, line):
    if isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], (ast.Lt, ast.LtE)):
        yield line, test.left, test.comparators[0]


def upper_bounds(test):
    for node in ast.walk(test):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ('assertLess', 'assertLessEqual') and len(node.args) >= 2:
                yield node.lineno, node.args[0], node.args[1]
            elif node.func.attr == 'assertTrue' and node.args:
                yield from compared(node.args[0], node.lineno)
        elif isinstance(node, ast.Assert):
            yield from compared(node.test, node.lineno)


def unmarked_wall_clock_bounds(source, name):
    found = []
    for test in ast.walk(ast.parse(source)):
        if not isinstance(test, ast.FunctionDef) or not test.name.startswith('test'):
            continue
        if any(isinstance(decorator, ast.Name) and decorator.id == 'run_alone' for decorator in test.decorator_list):
            continue
        assigns = [(node.targets, node.value) for node in ast.walk(test) if isinstance(node, ast.Assign)]
        assigns += [([node.target], node.value) for node in ast.walk(test) if isinstance(node, ast.AnnAssign) and node.value]
        clocked = set()
        while True:
            more = {target.id for targets, value in assigns
                    if CLOCK.search(ast.unparse(value))
                    or clocked & {name.id for name in ast.walk(value) if isinstance(name, ast.Name)}
                    for target in targets if isinstance(target, ast.Name)}
            if more <= clocked:
                break
            clocked |= more
        for line, measured, limit in upper_bounds(test):
            if not (isinstance(limit, ast.Constant) and isinstance(limit.value, (int, float))):
                continue
            names = {node.id for node in ast.walk(measured) if isinstance(node, ast.Name)}
            if TIMING.search(ast.unparse(measured)) or names & clocked:
                found.append(f'{name}:{line} {test.name}')
    return found


class WallClockTestsRunAlone(unittest.TestCase):
    def test_every_absolute_wall_clock_bound_is_marked_run_alone(self):
        found = [hit for path in sorted(TESTS.glob('test*.py'))
                 for hit in unmarked_wall_clock_bounds(path.read_text(encoding='utf-8'), path.name)]
        self.assertEqual(found, [], 'mark these tests @run_alone (tests/run_alone.py) so they run after the parallel pool')

    def test_an_unmarked_bound_is_found_and_a_marked_one_is_not(self):
        body = ('    def test_fast(self):\n        started = time.monotonic()\n'
                '        self.assertLess(time.monotonic() - started, 2)\n')
        self.assertEqual(unmarked_wall_clock_bounds(f'class Slow:\n{body}', 'planted.py'), ['planted.py:4 test_fast'])
        self.assertEqual(unmarked_wall_clock_bounds(f'class Slow:\n    @run_alone\n{body}', 'planted.py'), [])

    def test_bounds_written_as_comparisons_or_on_clock_derived_names_are_found(self):
        source = ('class Slow:\n    def test_fast(self):\n        start = time.monotonic()\n        took = time.monotonic() - start\n'
                  '        self.assertTrue(time.monotonic() - start < 2)\n        assert took <= 2\n        self.assertLess(took, 2)\n')
        self.assertEqual(sorted(unmarked_wall_clock_bounds(source, 'planted.py')),
                         ['planted.py:5 test_fast', 'planted.py:6 test_fast', 'planted.py:7 test_fast'])

    def test_a_bound_on_a_name_derived_from_clock_readings_is_found(self):
        source = ('class Slow:\n    def test_fast(self):\n        start = time.monotonic()\n        end = time.monotonic()\n'
                  '        took = end - start\n        self.assertLess(took, 2)\n')
        self.assertEqual(unmarked_wall_clock_bounds(source, 'planted.py'), ['planted.py:6 test_fast'])
        annotated = source.replace('took = end - start', 'took: float = end - start')
        self.assertEqual(unmarked_wall_clock_bounds(annotated, 'planted.py'), ['planted.py:6 test_fast'])

    def test_a_bound_on_a_size_is_not_a_wall_clock_bound(self):
        source = 'class Size:\n    def test_small(self):\n        self.assertLess(len(lines), 100)\n'
        self.assertEqual(unmarked_wall_clock_bounds(source, 'size.py'), [])


if __name__ == '__main__':
    unittest.main()
