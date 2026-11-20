#!/usr/bin/env python3
"""Plant bugs in a scratch copy of this checkout and compare a full-suite run with the Luna-filtered run.

    eval.py --scratch DIR [--out results.json] [--bugs 1 2 ...] [--baseline]

DIR must not exist. It receives a git copy of HEAD as `main`; each bug is applied
as an uncommitted edit, both runs happen there, and the edit is reverted. Nothing
is written to this checkout. Set COLD_VERIFY_MODEL_RATES to price the calls.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[4]
IDENTITY = ['-c', 'user.name=eval', '-c', 'user.email=eval@localhost']
RECIPE_STEP = re.compile(r"""verify_filter\.py}" run --suite '([^']+)' -- (.+)$""", re.MULTILINE)
SUMMARY = re.compile(r'filters: calls=(\d+) model_calls=(\d+) model_failures=(\d+) tokens=(\d+) cost=\$([\d.]+) run=(\d+) skip=(\d+)')
GROUP = re.compile(r'::group::tests (\S+) \((\d+) of (\d+)\)')
RAN = re.compile(r'^Ran (\d+) tests?', re.M)

RATES_ENV = 'COLD_VERIFY_MODEL_RATES'

BUGS = [
    (1, 'scripts/check_markdown_links.py', 'sys.exit(bool(errors))', 'sys.exit(0)', 'link checker always exits 0'),
    (2, 'ci/check-conflicts.py', 'if size >= 7:', 'if size >= 8:', 'conflict marker size off by one'),
    (3, 'ci/check-caps.py', 'upper = (ast.Lt, ast.LtE) if predicates[node] else (ast.Gt, ast.GtE)',
     'upper = (ast.Gt, ast.GtE) if predicates[node] else (ast.Lt, ast.LtE)', 'cap lint compares the wrong direction'),
    (4, 'ci/scope_check.py', 'if expected and changed > int(expected[1]):', 'if expected and changed >= int(expected[1]):',
     'size flag fires at the estimate instead of above it'),
    (5, 'ci/verify_timing.py', "row['duration_ms'] - row['waiting_ms'] - row['setup_ms']", "row['duration_ms'] - row['waiting_ms']",
     'running time includes setup time'),
    (6, 'ci/update-cold.py', 'if behind:', 'if behind > 1:', 'one missing commit is reported as up to date'),
    (7, 'ci/verify_cache.py', "for kind, files in ((b'R', recipes), (b'I', inputs)):", "for kind, files in ((b'R', recipes),):",
     'declared inputs left out of the cache key'),
    (8, 'ci/verify_trace.py', "self.steps[-1]['status'] = 'fail'", "self.steps[-1]['status'] = 'pass'",
     'a nonzero exit is traced as a pass'),
    (9, 'ci/review.py', "            or value['sha'] != sha or value['model'] != model\n", "            or value['model'] != model\n",
     'review result for the wrong commit is accepted'),
    (10, 'scripts/check_citations.py', 'if last > lines:', 'if last > lines + 1:', 'citation one line past the file is accepted'),
]


def git(scratch, *args):
    return subprocess.run(['git', '-C', str(scratch), *IDENTITY, *args], text=True, capture_output=True, check=True).stdout


def build_scratch(scratch):
    scratch.mkdir(parents=True)
    archive = subprocess.Popen(['git', '-C', str(ROOT), 'archive', 'HEAD'], stdout=subprocess.PIPE)
    subprocess.run(['tar', '-x', '-C', str(scratch)], stdin=archive.stdout, check=True)
    if archive.wait():
        raise SystemExit('git archive failed')
    git(scratch, 'init', '-q', '-b', 'main')
    git(scratch, 'add', '-A')
    git(scratch, 'commit', '-q', '-m', 'baseline')


def suites(scratch):
    recipe = (scratch / '.fleet/verify.sh').read_text()
    found = [(suite, shlex.split(command)) for suite, command in RECIPE_STEP.findall(recipe)]
    if not found:
        raise SystemExit('no filtered suite steps found in .fleet/verify.sh')
    return found


def run_suites(scratch, env):
    results = []
    for suite, command in suites(scratch):
        started = time.monotonic()
        done = subprocess.run([sys.executable, 'ci/verify_filter.py', 'run', '--suite', suite, '--', *command],
                              cwd=scratch, text=True, capture_output=True, env=env)
        text = done.stdout + done.stderr
        summary = SUMMARY.search(text)
        group = GROUP.search(text)
        results.append(dict(
            suite=suite, code=done.returncode, seconds=round(time.monotonic() - started, 1),
            kept=int(group[2]) if group else 0, total=int(group[3]) if group else 0,
            skipped=re.findall(r'^skip  (\S+)', text, re.M), failed=re.findall(r'^fail  (\S+)', text, re.M),
            unit_tests=sum(int(n) for n in RAN.findall(text)),
            calls=int(summary[2]) if summary else 0, model_failures=int(summary[3]) if summary else 0,
            tokens=int(summary[4]) if summary else 0, cost=float(summary[5]) if summary else 0.0))
    return results


def environment(change_file):
    env = {key: value for key, value in os.environ.items() if key == RATES_ENV or not key.startswith('COLD_VERIFY_')}
    env.update(PYTHONDONTWRITEBYTECODE='1', COLDSTACK_CITATION_ROOT=os.environ.get('COLDSTACK_CITATION_ROOT', str(Path.home() / 'projects')))
    env['COLD_VERIFY_CHANGES'] = str(change_file)
    return env


def write_unknown_change(path):
    path.write_text(json.dumps(dict(files=None, diff=None, trusted=False, oversized=False)), encoding='utf-8')
    return path


def total(results, key):
    return round(sum(result[key] for result in results), 4)


def summarize(results):
    return dict(caught=any(result['failed'] or result['code'] == 1 for result in results),
                exit_codes=[result['code'] for result in results],
                failed_files=sorted(name for result in results for name in result['failed']),
                kept=total(results, 'kept'), skipped=sum(len(result['skipped']) for result in results),
                unit_tests=total(results, 'unit_tests'), calls=total(results, 'calls'),
                model_failures=total(results, 'model_failures'), tokens=total(results, 'tokens'),
                cost=total(results, 'cost'), seconds=total(results, 'seconds'),
                skipped_files=sorted(name for result in results for name in result['skipped']))


def evaluate(scratch, bug):
    number, path, old, new, description = bug
    source = scratch / path
    text = source.read_text(encoding='utf-8')
    if text.count(old) != 1:
        raise SystemExit(f'bug {number}: expected exactly one match for the edit in {path}, found {text.count(old)}')
    source.write_text(text.replace(old, new), encoding='utf-8')
    try:
        sys.path.insert(0, str(scratch / 'ci'))
        import verify_filter
        with tempfile.TemporaryDirectory(prefix='luna-eval-') as state:
            change = Path(state) / 'change.json'
            verify_filter.write_change(verify_filter.collect_change(scratch, {}), change)
            full = run_suites(scratch, environment(write_unknown_change(Path(state) / 'unknown.json')))
            filtered = run_suites(scratch, environment(change))
    finally:
        git(scratch, 'checkout', '--', '.')
        git(scratch, 'clean', '-fdq')
    a, b = summarize(full), summarize(filtered)
    return dict(bug=number, file=path, description=description, full=a, filtered=b, full_suites=full, filtered_suites=filtered,
                missed=sorted(set(a['failed_files']) - set(b['failed_files'])),
                wrongly_skipped=sorted(set(a['failed_files']) & set(b['skipped_files'])))


def table(rows, priced):
    lines = ['| Bug | Full suite caught | Filtered caught | Kept / skipped | Model calls | Tokens | Cost | Full s | Filtered s |',
             '| --- | --- | --- | --- | --- | --- | --- | --- | --- |']
    for row in rows:
        full, filtered = row['full'], row['filtered']
        cost = f"${filtered['cost']:.4f}" if priced else 'unpriced'
        lines.append(f"| {row['bug']}. {row['description']} | {'yes' if full['caught'] else 'NO'} | {'yes' if filtered['caught'] else 'NO'} | "
                     f"{filtered['kept']} / {filtered['skipped']} | {filtered['calls']:g} | {filtered['tokens']:g} | {cost} | "
                     f"{full['seconds']:.0f} | {filtered['seconds']:.0f} |")
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scratch', type=Path, required=True)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--bugs', type=int, nargs='*')
    parser.add_argument('--baseline', action='store_true', help='also run the unmodified copy once with no filter')
    args = parser.parse_args()
    if args.scratch.exists():
        raise SystemExit(f'{args.scratch} already exists')
    build_scratch(args.scratch)
    results = {}
    if args.baseline:
        with tempfile.TemporaryDirectory(prefix='luna-eval-') as state:
            results['baseline'] = summarize(run_suites(args.scratch, environment(write_unknown_change(Path(state) / 'unknown.json'))))
        print('baseline', json.dumps(results['baseline']), flush=True)
    rows = []
    for bug in BUGS:
        if args.bugs and bug[0] not in args.bugs:
            continue
        row = evaluate(args.scratch, bug)
        rows.append(row)
        print(f"bug {row['bug']}: full={row['full']['caught']} filtered={row['filtered']['caught']} "
              f"kept={row['filtered']['kept']} skipped={row['filtered']['skipped']} calls={row['filtered']['calls']:g} "
              f"tokens={row['filtered']['tokens']:g} full={row['full']['seconds']}s filtered={row['filtered']['seconds']}s "
              f"missed={row['missed']}", flush=True)
        results['bugs'] = rows
        if args.out:
            args.out.write_text(json.dumps(results, indent=1) + '\n', encoding='utf-8')
    print(table(rows, bool(os.environ.get(RATES_ENV))))


if __name__ == '__main__':
    main()
