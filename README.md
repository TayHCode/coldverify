# coldverify
Coldverify: the verify engine behind ColdStack's verify gate

## Install

Coldverify needs Python 3.12 or newer on Linux or macOS, `git`, and no Python packages. Outside a GitHub Actions pull-request event, a strict run also needs `gh`, logged in (`gh auth login` or `GH_TOKEN`), to find the branch's PR; without it the receipt check is INCOMPLETE. A checkout with no git remote (and no `GH_REPO`) has no PR, so there the receipt check does not need `gh`. Explicit mode (`--base`, `--head`, `--test-command`) runs the test command only in bubblewrap and is INCOMPLETE without it: install `bubblewrap` (`apt install bubblewrap`), on a host that lets an unprivileged user create a user namespace (a fresh Ubuntu 24.04 machine does; an unprivileged container on a host with `kernel.apparmor_restrict_unprivileged_userns=1` does not). Where bwrap cannot start, this repository's explicit-mode tests skip and say why. Older Pythons are refused: `pip` and `pipx` refuse to install it, and `python3 coldverify` exits 2 with the Python it found. `coldverify --version` prints the version and the commit it was built from.

From a clone, with no install step:

```sh
git clone https://github.com/TayHCode/coldverify.git
cd coldverify
python3 coldverify --version
```

With pipx, from that clone:

```sh
pipx install .
pipx ensurepath
coldverify --version
```

Open a new shell after `pipx ensurepath` if `coldverify` is not found yet. `pipx install` works where `pip install` is refused as an externally managed environment (PEP 668), as on Ubuntu 24.04. `requirements.txt` is only for this repository's tests (`jsonschema` checks the receipt schema).

Inside a virtual environment, `pip` installs it the same way:

```sh
python3 -m pip install .
```

To pin it, check out an exact commit in the clone (`git checkout --detach <commit>`) before installing. Under the `cold` umbrella, its tool line is
`coldverify https://github.com/TayHCode/coldverify.git python3 coldverify --help`.

## Run it as ColdStack's verify gate

Run `coldverify` from the target checkout. Without `--ci` it first needs a project verification skill, a `.<agent>/skills/verify-*/SKILL.md` in the checkout made with `skills/create-verification-skill`, and exits 2 until one exists; `--ci` skips that step for scripted runs. It accepts every flag of ColdStack's verify gate (`--bug-fix`, `--no-repro-reason`, `--ci`, `--no-cache`, `--gate`, `--preview`/`--explain-skips`), so that gate can run `exec coldverify --compat ARGS`. When no runner is configured, the recipe and the repro run on this machine. A runner is configured when `COLD_VERIFY_HOST` is set or the checkout points at a GitHub repository of the optional policy file's `owner`. A checkout outside a Coldbox guest runs in a fresh Coldbox VM: `coldbox vm up` on this machine when `coldbox` is on `PATH`, otherwise over ssh on `COLD_VERIFY_HOST` (or the optional policy file's `coldbox_host`), whose Coldbox admission places the VM. The VM is deleted afterwards, on failure and interrupt too; one orphaned by an interrupted `vm up` is deleted by Coldbox's reaper after its TTL (2 h). If neither answers, the run is INCOMPLETE and names the host; it never falls back to this machine or to another runner. The run ships this checkout's `ci/` with it, so nothing else needs to be installed in the VM.

`COLD_VERIFY_BACKEND` overrides that choice for Linux checkouts. Both the repro and the suite then run on it, and the receipt records it as `environment.backend` and `environment.isolation`:

| Value | Runs on |
| --- | --- |
| `local` | this machine |
| `ssh:<host>` | `<host>` over `ssh -o BatchMode=yes`; it needs `python3` and `git`, and `<host>` must be the machine's own hostname, not an IP address or ssh alias (#87) |
| `container:<image>` | a throwaway container from `<image>` (`docker`, else `podman`), removed afterwards; the image needs `python3` and `git` |
| `coldbox` | a disposable Coldbox VM (`coldbox vm up`, `vm exec`, `vm down`), over ssh on `COLD_VERIFY_HOST` (or the policy file's `coldbox_host`) when `coldbox` is not on `PATH` |
| `coldbox-api` | the suite: a Coldbox job VM through `coldbox api submit`, `status` and `receipt`, on the same host as `coldbox`. It runs the repo's `coldbox checks` on the pushed `HEAD` commit, so the checkout must be clean, and it waits up to 2 hours. It returns no signed JUnit XML, so a strict run's tests are INCOMPLETE; use it with `--compat` or `ci/fleet-verify.sh`. That is also why `coldbox` does not switch to the API on its own: today's strict PASS would become INCOMPLETE, and a repo with `.fleet/verify.sh` but no `coldbox.toml` verify command would fail `coldbox checks`. A `failed` submission, a timeout or any verdict but pass or fail is INCOMPLETE. When that coldbox has no `api` subcommand, the suite runs as with `coldbox`. The repro always runs as with `coldbox` |

A backend that can't be started or reached makes the run `INCOMPLETE`, never `PASS`. Only the operator's environment sets the backend; nothing in the checkout can.

By default Coldverify is stricter than ColdStack's verify gate: a run is PASS only with a claim, at least one proof test that fails by assertion on base (exit 1) and passes on head (exit 0) while running a changed non-test line inside a function, fresh steps, a real test count from JUnit XML that coldverify's test adapter signed, a verify recipe the change did not add, edit or delete (deleting every recipe is allowed) (see `docs/verify-trace.md`), no deleted test file or case, no added skip or xfail marker, no removed or changed assertion in a pre-existing test, and a PR `## Claim` that still matches the verified claim (`checks.tamper`, see `docs/verify-receipt.md`), and acceptance criteria that each have a covering proof test (`checks.criteria`). An independent verifier writes those criteria from the claim and the base code only: `--builder-model MODEL` (repeat for each model that built the change) names the builders. When the run has a PR, the `Builder-Model: MODEL` lines anywhere in its description, `## Claim` included, must name the same models, or the criteria are INCOMPLETE before any model call; a `Builder-Model:` line alone never chooses a verifier, because the PR author writes it. The verifier is independent of the builder only as far as whoever passes `--builder-model` is: a builder record the author cannot write does not exist yet (#122, #123). ColdStack's model router picks an `auditor/verifier` model that is none of them, and `codex exec` runs it read-only, once to derive the criteria and once, fresh, to link each one to the proof tests that check it. Without a builder, ColdStack's CLI or `codex`, the run is INCOMPLETE. Coldbox CI guests hold no model credential by design, so in a guest `checks.criteria` is always INCOMPLETE and points to the separate `coldverify criteria` check. That one result is the exception to the criteria rules above: it does not count toward a guest's verdict, and the verdict's reason then always contains `criteria not evaluated in a CI guest`, so a guest PASS never means criteria were covered (its shared evidence record stays `incomplete`, see `docs/evidence.md`). Outside a guest, criteria count as usual. Only a root-owned `/etc/coldbox/host` naming a listed host makes a guest; `COLDBOX_GUEST=0` can turn guest mode off but nothing in the environment can turn it on. A trusted host (not a CI job) makes that check with `coldverify --criteria-check PR [--publish-check]`: it reads the PR description and commits from GitHub, takes the builders from `--builder-model` and is INCOMPLETE before any model call unless the description's `Builder-Model:` lines name the same models, takes the proof test names as untrusted input from the newest strict GitHub Actions receipt comment, which must end with a `<!-- coldverify repro: {json} -->` block matching the PR's base, head and claim (at most 200, and each file must exist inside the head snapshot), derives and links the criteria the same way, prints the result JSON and, with `--publish-check`, posts it as a `coldverify criteria` check run on the head (success only for PASS). `--publish-check` is refused in GitHub Actions and Coldbox guests. The check is informational: nothing may require or gate on it until #79 gives it a publisher other workflows cannot imitate, and its builder is only as trusted as whoever runs it (#122, #123). `--bug-fix` changes nothing because a repro is always required. Selected tests that pass on both sides are acceptance tests; identical non-zero exits are unchanged tests. Neither counts as proof. A new feature's test that errors on base only because it imports or reads a name the head adds is `missing-on-base` and counts as proof when its head pass runs a changed line. The tests run even when the repro fails. Any assertion failure on head, lost base pass, or unresolved base assertion failure makes the repro FAIL. Classifications appear in output and evidence. Steps are never reused unless `--cache` is given, and `--no-cache` overrides `--cache`. With `--cache`, a `normal`-risk change reuses the test run of a trusted PASS receipt (a GitHub Actions `coldverify receipt` check run) only when its tree, recipe and environment hashes all match, and the receipt says `reused from <check run URL>`; a `high`-risk change always runs the suite fresh. Inside GitHub Actions or a Coldbox guest, strict `--cache` is refused (INCOMPLETE) until trusted receipts can't be forged by another workflow (#79). Each strict run also appends a line to the clone's receipt archive, and `coldverify --history` shows which proof strategy produced which verdict for which change type (`docs/verify-receipt.md#archive`). The receipt's `suggestion` names the newest earlier PASS for the same paths and whether this run used its proofs; it never changes the verdict.

In a GitHub Actions job with `checks: write`, `--publish-check` also publishes the receipt as a `coldverify receipt` check run bound to the tree. `coldverify --require-receipt` is the merge gate: it exits 0 only when GitHub Actions published a PASS receipt for the tree of `HEAD`, on the PR head or attached to the PR body with a `Coldverify-Receipt: <check run URL>` line. A receipt stops counting once the PR's `Builder-Model:` lines differ from the builders it records. A pre-PR receipt counts when its tree matches; ancestry does not count (see `docs/verify-receipt.md`).

`coldverify --deployed URL_OR_HOST` verifies a live deployment instead of a change: it runs the project's flows that read `COLD_DEPLOYED_TARGET` once against that target and writes the same receipt, with no repro or tests. The flows must only read from the target (see `docs/verify-receipt.md`).

In a GitHub Actions job, a strict `--ci` run without `--publish-check` is the merge gate: it exits 1 only when the tests fail, declared flows fail or could not run, the secret scan or lesson checks find something (`docs/fast-lane.md`), or a bug fix has no test that fails on base and passes on head (what `--compat` accepts; a proof that runs no changed line still counts here, while the receipt says `INCOMPLETE`) and no `--no-repro-reason`: it then says "this PR is marked as a fix; add a test that fails without your change and passes with it". A PR is a bug fix when its title or branch starts with `fix` (`fix:`, `fix/`, `fix-`, `fix_` or the word), or its description has a line `Bug fix: yes` or `Type: bug`, or says it fixes a bug or fixed the bug; labels do not count. A PR that is not a fix avoids this by not using those words. Every other run exits 0, and the receipt still says `INCOMPLETE` or `FAIL` with the reason, so a docs, config or refactor change merges without a proof test while its receipt stays honest.

`--compat` gives ColdStack's verify gate's verdicts instead. The repro runs only for a bug fix: a `fix` title or branch, a bug-fix line in the PR body, or `--bug-fix`. A bug fix without a red-then-green repro fails (exit 1) unless `--no-repro-reason` opts out. No claim is read and no receipt is written or posted. The exit code is the recipe's, whether or not it printed a test count, and steps are reused unless `--no-cache` is given.

Other products read a strict run's verdict from `.cold/verify-evidence.json` in the shared ColdStack format, and cleaners read the owner and expiry of every evidence directory from its `.cold-artifact.json`; see `docs/evidence.md`.

## Run the tests

```sh
python3 -m pip install -r requirements.txt
python3 scripts/run_tests.py
```

`scripts/run_tests.py` runs every test under `tests/` in its own process, one per CPU (`--jobs N` to change). It fails if any test fails, or if a discovered test neither ran nor was skipped by its class or module fixture (which `unittest discover` also accepts). Tests that check wall-clock time carry `@run_alone` from `tests/run_alone.py` and run one at a time after the rest, so the other tests cannot slow them down. It prints the slowest tests. `cd tests && python3 -m unittest discover -p "test*.py"` still runs the suite serially.

## Limitations

- Linux and macOS only, Python 3.12 or newer. Windows and WSL are refused.
- A strict PASS needs a proof test that fails by assertion on base and passes on head; changes with no such test (docs, config, pure refactors) are FAIL or INCOMPLETE by design.
- Acceptance criteria need a model through `cold` or `codex`; without one, `checks.criteria` is INCOMPLETE.
- The recipe runs as the same user as the verifier, so a hostile change can tamper with the verifier's outputs. Run it on code you are willing to execute, ideally in a disposable VM or container backend.
- Remote backends (`ssh:`, `container:`, `coldbox`) need those tools installed and reachable; a backend that cannot start makes the run INCOMPLETE, never PASS.
