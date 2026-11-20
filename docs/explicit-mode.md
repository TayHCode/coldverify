# Explicit mode: base, head and test command

**Changed in #172:** the test command now runs in a bubblewrap sandbox with no network. A test that used to reach the
network now fails on both sides, which is a FAIL. When both sides fail with a name-resolution or unreachable-network
error, the reason says the test may need network rather than the fix being wrong. The output text alone cannot prove
that, so the verdict stays FAIL.

`coldverify --base SHA --head SHA --test-command COMMAND [--install-command COMMAND] [--claim TEXT]` verifies a change for a caller that has no
`.fleet/verify.*` recipe or PR, such as ColdContribute. Run it in a clean checkout of `--head`
that also holds `--base`, an ancestor of `--head`.

1. Coldverify makes a throwaway worktree at `--base`, with the head's added or changed test files (the repro test-path
   rule, plus `conftest.py`) copied in, and a throwaway `HOME`. It runs the install command, if given, then the test
   command there, and deletes both. Then it does the same at `--head`, so the base run never sees the head checkout,
   and the head's dependencies (venv, `node_modules`, build output, Go and npm caches under `HOME`) are built by the
   head's own install. Nothing writable is shared between the sides; there is no shared download cache. If no test file
   changed, the run is INCOMPLETE.
2. Both commands run as `sh -c COMMAND` in bubblewrap: the whole filesystem read-only; `/run`, `/tmp`, `/var`,
   `/home`, `/root`, `/srv`, `/mnt`, `/media` (where host Unix sockets live) and the caller's checkout hidden (only its
   `.git` is readable); and only that side's worktree and `HOME` writable. Tools must come from the system paths
   (`/usr`, `/opt`), as in a Coldbox guest. The install shares the host network namespace, so host abstract Unix
   sockets stay reachable from it (#175). A sandbox that does not start is INCOMPLETE, never a failing test. They get the scrubbed environment
   repro tests and flows get (no tokens) and the same 300 s limit each. The install gets the network; the test gets
   none. Without a working `bwrap` nothing runs and the result is INCOMPLETE. An install that fails or times out on
   either side is INCOMPLETE with the reason (`install on base failed with exit N: ...`, `install on head timed out
   after 300 s`), and that side's test does not run. Suite results and flow evidence left in the checkout are deleted
   before the receipt is written.
3. Exit 1 on base and 0 on head is a proof. A head exit of 1, or a base exit of 0, fails. Any other exit, or a
   timeout, is INCOMPLETE.
4. The result is the receipt's `repro`, as one test named `test command`. `.cold/verify-receipt.json` is the usual
   receipt. By default the repository suite is absent, so `checks.tests` and a successful repro's overall
   `result` are INCOMPLETE. Read `checks.repro` for the before and after verdict.

Pass `--suite-command COMMAND` to run the repository suite at `--head`. Coldverify installs dependencies again
in a fresh worktree and runs the suite there with the same sandbox and time limit as the repro.
Use the existing signed test adapters, for example
`--suite-command 'python3 "$COLD_VERIFY_UNITTEST" discover'`. For pytest, run `"$COLD_VERIFY_PYTEST"` with the
Python that has pytest installed. A suite PASS requires a positive test count from signed JUnit. The receipt
records the suite's commit in `run.head` and its tree in `run.tree`. A revision mismatch leaves the suite and
receipt INCOMPLETE.

The command is untrusted input. It runs only in a Coldbox guest, or where the operator set `COLD_VERIFY_BACKEND=local`
(normally inside a disposable VM). Anywhere else the repro is INCOMPLETE and the command never runs.

Exit codes are 0 for PASS, 1 for FAIL, and 2 for INCOMPLETE or a usage error. A usage error prints to stderr
and writes no receipt. A checkout whose `HEAD` differs from `--head` is a usage error when `--suite-command`
is absent.
