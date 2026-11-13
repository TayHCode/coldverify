# Pull request secret scan, lesson checks and fast lane

`coldverify --compat --ci` (what ColdStack's verify gate runs in Coldbox CI) does three extra things on a `pull_request`
event, after the repro gate and before the suite. A strict run does the first two, the secret scan and lesson checks,
before its repro; it has no fast lane. Code: `ci/verify_fastlane.py`.

Every CI run prints `coldverify environment: <sha256>`. The hash covers the guest's installed packages, `python3` and
`node` versions, this coldverify checkout, the coldstack checkout and the `coldbox` binary.

## Secret scan

The scan runs release-check's own scanner (`Scanner.file` from `/opt/release-check/release_check.py`) over every file
the pull request adds or changes. It reports `secrets` findings on the lines the pull request adds. It also reports
credential files, such as `*.pem`, when the pull request adds them. Private data such as host names is not reported,
because these repositories are private.

| Result | Meaning |
|---|---|
| `Secret scan: PASS` | no unreviewed secret finding on an added line |
| `FAIL: secret scan: <kind> in <path>:<line>` | a finding; the value is never printed, and CI fails |
| `WARN: secret scan: <kind> in <path>:<line> ...` | a heuristic finding in a warn-only folder; CI does not fail |
| `INCOMPLETE: secret scan could not run` | release-check is missing or broken, git failed, the warn-only list is invalid, or a line of the diff is longer than 1 MiB (`DIFF_LINE_LIMIT`); CI fails |

A diff line over 1 MiB is never read whole and never counted as scanned: both the secret scan and the lesson checks print `could not run: ValueError` and CI fails. There is no override. Keep such a line out of the pull request: pretty-print or split the file (a minified asset or a one-line lockfile), or generate it at build time instead of committing it. If it must be committed as one line, raising the cap is the owner's decision.

The base is the merge commit's first parent, or the merge base when CI checked out the pull request head.

To clear a test fixture:

- `assigned-secret` and `entropy` are heuristics. Run release-check on the repository, then copy the finding into
  `release-check.json` `approvals` with `"classification": "false-positive"`, its `source_sha256` and a `reason`. The
  approval stops applying when the file changes.
- The exact formats (`private-key`, `github-token`, `aws-key`, `api-key`, `jwt`, `credential-url`, `bearer`) cannot be
  approved. Build the fixture at run time instead, for example `'-----BEGIN ' + 'PRIVATE KEY-----'`.

A repository can list warn-only folders in `.fleet/secret-scan-warn-only`, one relative folder ending in `/` per
line (for example `Conversations/`). In those folders, `assigned-secret` and `entropy` findings print `WARN` and do not
fail CI; every exact format still fails. The list is read from the base commit, so a pull request cannot loosen its own
scan. It suits folders of redacted snapshots.

## Lesson checks

These run next to the secret scan and enforce the Coldfix lessons that a check can decide without judgment. Each failure line names the fix, and CI fails.

| Lesson | Check | Fix |
|---|---|---|
| L13 | A line the pull request adds starts with `<<<<<<<`, `\|\|\|\|\|\|\|` or `>>>>>>>`, then a space or the end of the line. `=======` alone is not checked, since it underlines Markdown headings. | Finish the merge and delete the marker lines. A test fixture builds them at run time, for example `'<' * 7`. |
| L14 | The checkout's own `.gitignore` does not ignore `.worktrees/probe/x`. Global excludes and `info/exclude` are not read; the matching rule must come from a `.gitignore`. | Add `/.worktrees/` to the repository's `.gitignore`. |

In the warn-only folders listed on the base commit (see the secret scan), an L13 marker prints
`WARN: L13 conflict marker in <path>:<line> ...` and does not fail CI, because those folders hold quoted transcripts.
`Lesson checks: PASS` or `FAIL` sums them up. If git fails, the result is `INCOMPLETE: lesson checks could not run` and
CI fails.

## Fast lane

A pull request skips the suite only when its tree already passed the full suite on the same toolchain. The builder:

1. Pushes the branch, up to date with `main`.
2. Runs `gh workflow run verify.yml --ref <branch>` and waits for it to pass.
3. Opens the pull request with one line in its description: `Verified-Run: https://github.com/OWNER/REPO/actions/runs/RUN_ID`.
   CI reads the description from the event that started the run, so editing an open pull request's description
   changes nothing until the next push, or until it is closed and reopened.

CI then prints `PASS: fast lane: tree <tree> already passed the full suite on this toolchain in <url>` and exits 0. It
does this only when every check below holds. Anything else prints `Full suite: <reason>.` and runs the suite as before.

- The secret scan and the lesson checks passed.
- There is exactly one `Verified-Run` line, and it names this repository.
- The run is a `workflow_dispatch` run of this same workflow file, completed with `success`.
- The run has exactly one job named like this job, on a `coldbox` runner, and it passed.
- That job's log prints exactly one environment hash, and it equals this run's.
- The run's commit has the same tree as the clean checkout CI is testing. If `main` moved since the run, the merge
  commit has a different tree and the full suite runs.

The lookup needs `GITHUB_TOKEN` with `actions: read`. Without it, the full suite runs. The workflow passes the token
on the `coldbox checks` step:

```yaml
permissions:
  contents: read
  actions: read
...
      - run: coldbox checks
        env:
          GITHUB_TOKEN: ${{ github.token }}
```

`linux-verify.py` removes the token before the recipe runs.
