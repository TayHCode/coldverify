# coldverify test filters

`coldverify` can run fewer test files when it can say why. The recipe
(`.fleet/verify.sh`) runs each suite through `ci/verify_filter.py`, which keeps
an ordered list of named filters and asks each in turn whether a test file runs.
This sits on top of the [step cache](verify-cache.md), which skips whole steps.

## Filters

A filter is `Filter(name, function, model=False)`. `function(test, change)`
returns `Decision(run, reason, tokens, cost)` or `None` for "no opinion". The
first decision wins. Order, first to last:

| Filter | Runs the test when |
| --- | --- |
| `changes-unknown` | The changed files could not be determined (no `origin/main` or `main` to compare with, no change data, or a git error). |
| `changed-test-file` | The test file is in the diff, including untracked files. |
| `config-change` | A config or lockfile changed (`package-lock.json`, `*.toml`, `requirements*.txt`, `conftest.py`, `.fleet/`, `.github/` and similar). Every test runs. |
| `luna` | The model ColdStack's model router names for `builder-trivial` rates how likely the diff affects the test. Skips it only when the rating is under 0.2. |

A test that no filter excludes runs (`default`). Filters with `model=True` must
come after all others, and names must be unique; `select` raises otherwise.

## The Luna filter

For each test file that no earlier filter decided, `luna` runs one
`codex exec` call (route from ColdStack's model router for `builder-trivial`, which must be
a `codex` provider) with the diff and the test source, and expects
`{"probability": 0-1, "reason": "..."}`. The call runs in an empty scratch
folder under the criteria verifier's codex permission profile
(`ci/codex_sandbox.py`): it can read only that folder, the codex executable and
minimal system paths (no `/etc`), and a `codex sandbox` probe before each call
must show a file outside the folder unreadable. A rating under 0.2 skips the
test; 0.2 and above runs it. Unsure, a reply that does not parse or is out of
range, a failed sandbox probe, a timeout, an error, a test larger than 100,000
bytes, or a missing route all run the test. The calls are sequential, one per
test file, so a full run pays about 4 seconds and 22,000 input tokens per
undecided file.

Cost is 0 unless `COLD_VERIFY_MODEL_RATES="input,cached,output"` (price per
million tokens) is set; there is no price list in the repo. Tokens are always
reported.

## Fail-open

Anything unsure runs the test:

- The diff is untrusted, larger than 200,000 bytes, or unavailable: the model
  filter returns "run" without being called.
- A filter raises, returns something other than a `Decision` with a boolean
  `run`, or skips without a reason: the test runs and the reason says why. A model filter that does
  this counts as a failure.
- After 3 model failures in one verify run (counted across all suites), the
  model is not called again and the remaining tests run.
- The diff given to filters includes untracked files; their size counts toward
  the 200,000-byte limit.
- The Windows ssh path does not forward the environment, so it has no change
  data and runs everything.

Untrusted means `COLD_VERIFY_UNTRUSTED` is set, or `GITHUB_ACTIONS` is set and
the event is neither a `push` nor a pull request whose head repo is the base
repo (a fork, or an event that cannot be read or names no repo).

## Output

Every skipped test prints one line naming the filter that skipped it:

```
skip  tests/test_b.py  [luna] rated 0.05 likely affected: docs-only change
filters: calls=12 model_calls=3 model_failures=0 tokens=21 cost=$1.5000 run=1 skip=2
```

If every test in a suite is skipped, or no file matches, the run prints
`INCOMPLETE: ...` and exits 2. Nothing left to run is never a pass. A failing
selected test exits 1 and prints `FAILED (1): <step>`, which the
[trace](verify-trace.md) records as a failed step.

## Preview

```sh
coldverify --preview        # alias: --explain-skips
```

Prints every test as `run` or `skip` with the filter and reason, then the
summary line, and runs no test. It executes `.fleet/verify.sh` locally with
`COLD_VERIFY_PREVIEW=1`; the recipe skips its other checks in that mode and the
filter prints the plan instead of running. A recipe that does not branch on
`$COLD_VERIFY_PREVIEW` outside a comment is refused with `INCOMPLETE` (exit 2), so preview can never
run tests by accident. A checkout with no `.fleet/verify.sh` is also `INCOMPLETE`, with a message
that names the missing recipe; a `.fleet/verify.py` recipe has no preview mode. Neither case changes
what a real verify run does. Preview does not need the verification skill and is not
transported to another host.

## Environment

`coldverify` computes the changed files and diff in the source checkout (the
Linux snapshot has no history) and passes them as a JSON file named by
`COLD_VERIFY_CHANGES`, plus `COLD_VERIFY_FILTER` (the script path). Both, and
`COLD_VERIFY_PREVIEW`, replace any inherited value.

## Use in a recipe

```bash
python3 "${COLD_VERIFY_FILTER:-ci/verify_filter.py}" run --suite 'tests/test*.py' -- \
  python3 -m unittest discover -s '{dir}' -p '{name}'
```

`--suite` is a git glob pathspec (repeatable). The command runs once per
selected file; `{file}`, `{dir}` and `{name}` are the file's path, directory and
base name. Selected files run in parallel, each with its own `TMPDIR`. Output is
printed in file order after all of them finish. Under `verify_parallel.py`, all
steps and their test files share one cap of `COLD_VERIFY_JOBS` running
processes (default: the CPU count), so suites running side by side never add up
to more than that.
