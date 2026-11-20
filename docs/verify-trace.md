# coldverify step trace

`coldverify` writes `.cold/verify-trace.jsonl` in the repository root next to
its normal output. Agents and Coldfix read this file instead of the long log.
The human output is unchanged. The latest-run file is replaced on every run.
Each run also appends its rows
to `.cold/verify-history.jsonl`, under a writer lock. Both files use the same
`run_id`. No history rotation is performed. Concurrent runs retain separate
rows in history; the latest-run file belongs to the last writer.

`.cold/` ignores itself, so it never dirties the checkout.

## Schema (`schema_version` 3)

One JSON object per line, one line per step, in the order the steps ran:

| Field | Type | Meaning |
| --- | --- | --- |
| `schema_version` | integer | Always `3` for newly written rows. |
| `run_id` | string | Random hex id shared by every record of one run. |
| `head` | string or null | `git rev-parse HEAD` of the repository root, or null when it has no commit. |
| `snapshot_tree` | string or null | SHA-256 of the exported working-tree entries before tests start. Null when setup fails before export or a custom command does not export a snapshot. |
| `host` | string | Hostname of the machine running the trace wrapper, including for verification transported to another host. |
| `platform` | string | Lowercase OS of that host, such as `linux`, `windows`, or `darwin`. This is the coordinator OS, not the remote runner OS. |
| `concurrent_runs` | integer | Number of active trace wrappers for this user on this host at this run's start, including this run, across repositories. A sample, not a peak or admission limit. |
| `step` | string | Step name. |
| `command` | string | What the step ran: the group title for a marked step, the whole verify command for an unmarked one. |
| `status` | `"pass"`, `"fail"` or `"skip"` | Outcome. |
| `duration_ms` | integer | Wall-clock time of the step. |
| `failing_excerpt` | string | Only when `status` is `"fail"`: the last lines of the step's output, secrets redacted. |
| `waiting_ms` | integer | Part of this step's `duration_ms` spent blocked on the host-local run registry lock before the verify command was launched. |
| `setup_ms` | integer | Part of this step's `duration_ms` between that launch preparation and the exported snapshot being ready. 0 when no snapshot receipt is written (custom commands, macOS). |
| `inputs_hash` | string or null | The step's cache key: sha256 of the step name, the repo recipe (`.fleet/verify.sh`, `.fleet/verify.ps1`, whichever exist) and, for a step wrapped by the [step cache](verify-cache.md), its declared inputs; null when the repo has no recipe file and the step declares no inputs. |

Example:

```json
{"schema_version":3,"snapshot_tree":null,"host":"build-host","platform":"linux","concurrent_runs":1,"run_id":"9f2c...","head":"079aa7c...","step":"pip install -r requirements.txt","command":"pip install -r requirements.txt","status":"pass","duration_ms":4120,"waiting_ms":0,"setup_ms":0,"inputs_hash":null}
{"schema_version":3,"snapshot_tree":null,"host":"build-host","platform":"linux","concurrent_runs":1,"run_id":"9f2c...","head":"079aa7c...","step":"python tests","command":"python tests","status":"fail","duration_ms":8800,"waiting_ms":0,"setup_ms":0,"failing_excerpt":"assertion failed: expected 1\nFAILED (1): python tests","inputs_hash":null}
```

## Timing fields

Schema v3 adds `waiting_ms` and `setup_ms` to every row. Each is the overlap of
the step's interval with the phase, so the phase lands on the step that was open
while it happened. Time before the first `::group::` belongs to no step, so on a
grouped recipe the wait and setup that precede the first group are not
reported; a recipe that prints no markers is one step and reports all of it.
Rows from v1 and v2 traces have no split.

## Snapshot identity and compatibility

Schema v2 adds the four context fields above. Existing field meanings are
unchanged. Schema v3 adds `waiting_ms` and `setup_ms`; v1 and v2 rows read as null for them. Readers must accept v1 rows without these fields and treat the
missing values as unknown, not infer them from `head`. `read_records` in
`ci/verify_trace.py` reads mixed v1/v2 histories with null defaults for absent
context. The step-cache schema is separate and remains unchanged.

The exporter hashes the snapshot that it sends to verification, rather than
rehashing the source after tests. Linux and Windows use the staged export;
macOS hashes its native checkout before the recipe runs. Entries are sorted
by their relative POSIX path bytes. Each entry contributes three fields:
path bytes, ASCII octal mode including file type and permissions, and raw
file bytes or symlink target bytes without following the link. Each field
starts with an unsigned 8-byte big-endian byte length. Directories contribute
no entry. Only the root `.git` directory or gitfile is excluded. Dirty and
untracked files included by the exporter are included in the digest. Git
filters and line-ending normalization do not affect hashing of exported bytes.
Fresh Git metadata, timestamps, reflogs and packing do not affect the digest.

`head` records the source HEAD separately. Refs, history, index state and
Git configuration are not captured by `snapshot_tree`. This trace is diagnostic,
not an attestation-reuse key. Checks that read uncaptured Git or environment
inputs must rerun. Identical digests alone do not classify a failure as a flake.

Host and concurrency describe the coordinator, which is where concurrent
exports consume scratch space. They do not measure remote runner load or
verifies launched by other users or machines. A per-user host-local lock
registry at `~/.local/state/cold/verify-runs` tracks live wrappers across
different scratch directories; abandoned entries are removed on the next run
without counting crashed processes. OS lock release handles process exits.

## Keep the installed command current

The installer creates a Python `runpy` launcher at `~/.local/bin/cold` that
points to the checkout passed to the installer; it is not a pip installation or
a symlink. Updating another worktree does not update that command.

From a ColdStack checkout run:

```sh
python3 ci/update-cold.py
python3 ci/update-cold.py --update
python3 ci/update-cold.py
```

The check uses `cold` on PATH, resolves a direct checkout, symlink or the
installer's launcher without executing it, and warns with exit 1 if commits
from the locally fetched `origin/main` are missing. Update fetches main and
fast-forwards the installed checkout only when it is clean and on `main`.
It verifies the `cold` entry point, checkout root and ColdStack GitHub origin
before updating, so an unrelated Python launcher cannot redirect the update.
The merge uses `--no-overwrite-ignore` to preserve ignored local files that
upstream starts tracking. It preserves the launcher and refuses dirty,
divergent, detached or feature
checkouts. Repeating update is safe. Unrecognized installations return exit 2
and need their own installer; no files are rewritten. Run this update after
merging ColdStack changes. The check alone makes no network request.

To test a candidate before merge, select its CLI explicitly on PATH:

```sh
export PATH="$PWD:$PATH"
coldverify </dev/null
```

This tests the candidate CLI without repointing the installed launcher.

## How steps are found

The tracer reads the verify command's output as it streams and echoes it
unchanged. Stderr is merged into stdout so lines keep their true order and stay
with the step that printed them. It recognises these line markers:

- `::group::TITLE` ... `::endgroup::` is one step named `TITLE`.
  Groups nested inside a group are part of the outer step's output.
- `::skip::TITLE` records a `skip` step with no duration.
- `FAILED (CODE): TITLE` marks the step `TITLE` as `fail` and adds the line to
  its excerpt. If no such step exists, the output after the last group becomes
  a step named `TITLE`.
- `::inputs::KEY` sets the `inputs_hash` of the next `::group::` or `::skip::`
  step. The step cache prints it.
- `::elapsed::MS` sets the `duration_ms` of the next top-level `::group::`
  instead of the time its lines took to arrive. The parallel runner prints it.
- Output outside any group becomes a step named `verify` (the whole run when
  the recipe prints no markers, otherwise the tail after the last group, such
  as the Python test run).

A non-zero exit with no `FAILED` marker marks the last step `fail`.

## Verdict

The test run ends with `TESTS PASS`, `TESTS FAIL` or `TESTS INCOMPLETE` and
the reason, and saves them to `.cold/verify-result.json`
(fields: [verify-receipt.md](verify-receipt.md)).

The test count comes from JUnit XML that coldverify's own test adapters wrote.
The recipe gets an empty directory in `COLD_VERIFY_JUNIT_DIR` and a per-run key
in `COLD_VERIFY_JUNIT_KEY`. An adapter writes each report there as `*.xml` and
signs it with the key (HMAC-SHA256 in `<name>.xml.sig`). An XML file without a
valid signature never counts. Every
`<testcase>` without `<skipped>` in a signed file counts as a test that ran. For Python unittest,
`python3 "$COLD_VERIFY_UNITTEST" ...` is a drop-in for `python3 -m unittest ...`
that writes the report; a test method whose subtests all skipped, with none
passing, is reported as skipped. For pytest, `"$python" "$COLD_VERIFY_PYTEST" ...`
is a drop-in for `"$python" -m pytest ...`; run it with the Python that has
pytest installed. Generic detection writes one for unittest and pytest.
The adapters remove both variables before tests start, so nested runs inside tests
are not counted and tests cannot sign a report.

The key reaches everything the recipe runs, so a recipe could sign its own
report. A strict run therefore also compares every `.fleet/verify.*` file with
the merge base: if the change adds, edits or deletes one, `checks.tamper` is
`INCOMPLETE` (`recipe changed; needs review`) even when its tests passed; so does
an uncommitted edit to a recipe. Deleting every recipe is allowed, because coldverify's
own generic detection then runs the tests; it hides the key from project installs.

Limit: the signature does not isolate PR code that still runs as the same user.
A script the unchanged recipe calls, or test code inside the adapter's process,
can still reach the key. Detecting such changes is the tamper check's job.

- `FAIL` (the failing exit code, or 1): a step or command failed, or signed
  JUnit XML records a failure or error.
- `INCOMPLETE` (exit 2): the suite launcher reported an infrastructure
  abort during setup or execution. The required suite gate fails and the
  receipt names the phase. A signed JUnit failure or error still yields
  `FAIL` with exit 1.
- `INCOMPLETE` (exit 2): a step was skipped or reused from the cache, a suite
  ran only part of its tests, a JUnit file has no valid signature or can't be
  read, the XML records no test that ran, or there is no XML at all. Without XML, a count found in the
  output (unittest `Ran N tests`, `GENERIC_EXECUTED_TESTS=N`, the pytest
  summary, `cargo test`, `node --test`, Jest, Vitest) is still reported, but as
  `tests_source: "output"`: unbound, because printed text proves nothing ran.
- `PASS` (exit 0): every step ran fresh and passed, and the JUnit XML records at
  least one test that ran and none that failed (`tests_source: "junit"`).

Nothing that was not run fresh counts as a pass.

## Parallel steps

A recipe can run independent steps at the same time:

```bash
python3 "${COLD_VERIFY_PARALLEL:?}" --jobs 4 \
  --step "lint" "npm run lint" \
  --step "unit tests" "npm test"
```

`COLD_VERIFY_PARALLEL` is set by `coldverify` and points at
`ci/verify_parallel.py`. Each `--step TITLE COMMAND` runs with `bash -c` from
the recipe's directory. Behaviour:

- At most `--jobs N` steps run at once. Without it the cap is
  `COLD_VERIFY_JOBS`, else the CPU count available to the process (`nproc`).
  `--jobs 1` is serial.
- Every step gets its own `TMPDIR` (also `TMP` and `TEMP`) inside the recipe's
  `TMPDIR`, removed when the step ends, pass or fail.
- Output is buffered per step and replayed in declared order as
  `::group::`/`::endgroup::`, so trace rows and their order are the same for
  any `--jobs` value. Each step's real run time is recorded through `::elapsed::`.
  Output appears when the step and all earlier steps have finished.
- Such a step records `waiting_ms` and `setup_ms` of 0: it ran inside the
  recipe, after the lock and snapshot phases ended.
- Every step runs even after a failure. Failed steps print `FAILED (CODE): TITLE`;
  the exit code is that of the first failing step in declared order.
- Steps must not share files or ports. `concurrent_runs` still counts wrappers on
  the host, not steps inside a run.

## Excerpt

`failing_excerpt` is the last 20 lines of the failed step's output
(`EXCERPT_LINES` in `ci/verify_trace.py`). Redaction runs on the whole step
output before the tail is cut. It replaces private key blocks, `Authorization`
header values, `name=value` pairs whose name contains token, secret, password,
api key or credential, credentials in URLs, common token shapes (GitHub, `sk-`,
AWS, Slack, JWT), and the value of any environment variable with such a name.
Redaction is pattern-based; treat the trace as diagnostic output, not a place
that is guaranteed free of secrets.

## CI runs (`coldverify --ci`)

- Each CI run appends a step table, with failing excerpts, to the GitHub job summary.
- Every CI run executes the recipe. A pass recorded for the same tree by an earlier run is never reused.

## Claim, repro and receipt

`coldverify` passes only when it can show the work. It checks three things:

1. Claim: the `## Claim` section of the PR description, or `--claim TEXT`.
   A run with no claim is `INCOMPLETE`.
2. Repro, for every change: select new or changed Python unittest cases and
   module-level pytest functions (`def test_add(): assert ...`). Strict Python
   replay records the assertion failure on base (exit 1) and pass on head
   (exit 0) as `unproven` until the runner can independently observe a
   changed line on the pinned revision. Without another qualifying proof,
   the repro is `INCOMPLETE` with reason `python-coverage-not-runner-observed`.
   `--compat` keeps the plain 1/0 rule.
   Strict mode selects new or changed test-named `.sh` files with a
   `#!/bin/bash`, `#!/usr/bin/bash`, or `#!/usr/bin/env bash` shebang.
   Coldverify runs each whole file from the checkout root in the Linux sandbox,
   using the head test on both sides. The repro receipt records both commands
   and exits. Exit 0 means pass and exit 1 is the test failure convention.
   Use exit 2 for setup errors. A missing Bash or a setup exit makes the replay
   incomplete. A 1/0 replay remains `unproven` pending behavior evidence.
   Go, Rust and JavaScript tests are selected too, one top-level test at a
   time: `go test -run` next to `go.mod`, `cargo test --test` for files directly
   in a crate's `tests/`, and `jest -t` (when `package.json` names jest) or
   `node --test --test-name-pattern` next to `package.json`. A JavaScript test
   is red only on an assertion failure. In strict mode, a Go 1/0 case counts
   as proof when its coverage exceeds the zero-test control for a block that
   overlaps a changed production line. Blank lines and comments do not count.
   For Cargo, strict mode compares the passing integration test's Rust
   coverage with the same binary running zero tests. The selected test must
   increase the count on a changed non-test Rust line. Install the compiler's
   matching `llvm-profdata` and `llvm-cov` on the test host, for example with the Rustup
   `llvm-tools-preview` component. Missing tools or no changed-line hit
   leave the 1/0 Cargo result `unproven`. The head test output records the reason.
   From `tests/`, run `python3 -m unittest -v cargo_repro` on a Linux host with
   those tools to exercise the real Cargo fixture.
   Node and Jest compare coverage from the passing head case with the same file run with
   zero cases selected. The repro receipt records a changed JavaScript line
   reached by the selected case. Without that evidence, a 1/0 replay is
   `unproven`.
   `--compat` accepts a 1/0 replay without coverage.
   To run the strict Jest fixture with npm access, run
   `python3 -m unittest -v test_verify_repro_languages.FixtureRepos.check_strict_jest_records_the_changed_javascript_line`
   from `tests/`.
   For a package that declares Vitest, Coldverify runs the selected named test
   on base and head using the package's prepared `node_modules`. To prove a
   changed line ran on head, it compares coverage with the same file collected
   with zero test cases selected. The package setup must provide either
   `@vitest/coverage-v8` or `@vitest/coverage-istanbul` for that comparison.
   To run Coldverify's real Vitest fixture on a test host, copy
   `tests/fixtures/repro/vitest/base/package.json.fixture` to `package.json`
   in a scratch directory and run `npm install` there.
   From `tests/`, run `COLD_TEST_VITEST_MODULES="$prepared_modules" python3 -m unittest -v vitest_repro`
   with `prepared_modules` set to the absolute path of that `node_modules`.
   A 0/0 test is acceptance; identical non-zero exits are unchanged. Neither
   counts as proof or blocks a proof, except that any exit 1 on head fails.
   A base pass lost on head, or a base assertion failure not passing on head,
   also fails. Without proof the gate still fails (or is `INCOMPLETE` under
   the existing conditions below). Output and JSON tests include `kind`:
   `proof`, `acceptance`, `unchanged`, `regression` for blocking transitions,
   or `unproven` for other transitions that supply no proof. An error,
   skip or `xfail` is not a failure on base, except in strict runs when the
   error is an `ImportError` or `AttributeError` for a name the head adds to
   non-test code in that same module or class (a new function, class,
   variable or module). That base error
   is exit 3, and a head pass records an `unproven` 3/0 replay. Pytest functions run under
   pytest, which is installed for the repro when the project doesn't provide
   it. Pytest test classes are not reproduced yet.
   No such test, uncommitted work, or `--no-repro-reason TEXT` is
   `INCOMPLETE`, with the reason recorded. So is a changed test file in a
   language Coldverify can't reproduce yet (JS, Go, Rust, Java, C#,
   PowerShell, ...), even when the Python cases beside it pass; a Python
   case that fails the repro is still `FAIL`.
3. Tests: the verdict above.

Any `FAIL` makes the run `FAIL` (exit 1); otherwise any `INCOMPLETE` makes it
`INCOMPLETE` (exit 2); only three passes make `PASS` (exit 0). The tests run
even when the repro fails, and their verdict is in the receipt. Outside a Git checkout nothing runs: the run
ends `COLDVERIFY INCOMPLETE: <dir> is not a Git checkout` (exit 2) and writes
no receipt. `--compat` keeps ColdStack's verify gate's behavior there.

The receipt goes to `.cold/verify-receipt.json` and `.cold/verify-receipt.md`
and is printed. It has the claim, the commit, the command, each check's
result and reason, the repro cases with base and head exit codes, and every
test step with its command, status and time. When the branch has an open PR,
the receipt is posted to it with `gh pr comment`; until that post is confirmed,
the saved receipt and the run are `INCOMPLETE`. The last line is `COLDVERIFY PASS`, `COLDVERIFY FAIL` or
`COLDVERIFY INCOMPLETE` with the reasons. The JSON receipt is versioned
(`schema_version`), and [verify-receipt.md](verify-receipt.md) documents
every field.

Failure markers belong to the recipe level. A `FAILED: <step>` or
`FAILED (<code>): <step>` line after a top-level `::endgroup::` marks
that step failed. Inside an open group, these lines are command output,
including nested fixture runs, and do not set the outer step's status.
Step wrappers must close their group before emitting a failure marker.
A nonzero command exit still fails verification even without a marker.
