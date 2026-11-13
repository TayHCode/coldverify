# Shared evidence and integrations

Coldverify's own receipt (`docs/verify-receipt.md`) stays the standalone
default. Beside it, a strict run also writes the same verdict in the shared Cold
Stack format, so other products can read it without knowing the receipt's
fields, and it marks every evidence directory with an owner and an expiry date
so a cleaner knows whose files they are.

Both formats belong to ColdStack. Coldverify follows them and does not define
them:

| Contract | ColdStack source | Pinned copy here |
| --- | --- | --- |
| Run result v1 with the evidence fields | `docs/run-result.md`, `ci/run-result.schema.json` | [`coldstack/run-result.schema.json`](coldstack/run-result.schema.json) |
| Artifact marker v1 | `docs/artifact-marker.md`, `ci/artifact-marker.schema.json` | [`coldstack/artifact-marker.schema.json`](coldstack/artifact-marker.schema.json) |

The copies are from ColdStack `ecfcb0150a75c7cba6bd29e7f64804f9e4cb4071`.
`tests/test_evidence_export.py` validates everything coldverify writes against
them. To follow a newer ColdStack version, copy both files again and rerun the
tests.

## `.cold/verify-evidence.json`

One run result v1 record per strict run, written after the receipt. It is not
written by `--compat` runs, or when the repository name or the full commit is
unknown (no `GITHUB_REPOSITORY` and no `github.com` `origin` remote), because
the shared schema requires both. A run removes the previous run's record before
it starts.

| Run result field | From the receipt |
| --- | --- |
| `run_id` | A new random id, also in the `.cold/` marker's `provenance.run_id`. |
| `repo` | `GITHUB_REPOSITORY` or the `origin` remote's `owner/repo`, and `commit`. |
| `model` | `unknown` in all three fields: coldverify does not know which model made the change. |
| `steps` | One `check <name>` step per receipt check, in order. `PASS` is `pass`, `FAIL` is `fail` (with the reason as `failing_excerpt`), `INCOMPLETE` is `skip`. Checks are not timed, so `duration_ms` is 0. |
| `outcome`, `failure` | Any `skip` step makes it `incomplete`, class `harness`, cause `skipped_step`. Otherwise a `fail` step makes it `fail`, class `product`, cause `test_failure` when the first failed check is `tests`, else `behavior_mismatch`. Otherwise `pass`. `failure.detail` is the receipt's `reason`. |
| `time`, `cost` | All `null` (unknown). |
| `environment` | The receipt's `host`, `os`, `isolation` and `guest_id`, plus `tools.python` and `tools.coldverify`. |
| `claim` | `statement` is the claim; `source` is `pull request` when it is the PR's `## Claim` section, else `coldverify --claim`. Absent when no claim was stated. |
| `procedure` | `recipe: coldverify` and the receipt's `procedure` commands in order. |
| `artifacts` | The receipt's artifacts plus `.cold/verify-receipt.json`, each with its SHA-256. |
| `retention` | The same block as the `.cold/` marker below. |

The shared rule is stricter than the receipt in two cases, because the shared
schema says a skipped step means no product verdict: a receipt that is `FAIL`
and also has an `INCOMPLETE` check exports as `incomplete`, and so does a
Coldbox guest `PASS`, whose criteria were not evaluated (its `failure.detail`
contains `criteria not evaluated in a CI guest`). It never exports as `pass`
unless the receipt is `PASS`.

## Evidence directory markers

| Directory | Marker | Retention |
| --- | --- | --- |
| `.cold/` in the checkout, every run including `--compat`, written while holding `.cold/verify.lock` | `.cold/.cold-artifact.json` | `owner: coldverify`, `temporary`, expires 14 days after the run. A strict run adds `provenance.run_id` and `provenance.repo`. |
| The repro stage copied to the repro host as `/tmp/cold-repro-<id>`, and the `cold-repro-*` working directory the repro creates for its base and head worktrees | `.cold-artifact.json` in each | `temporary`, expires 1 day after it was created. Coldverify removes both when the repro ends; the marker covers a run that was killed first. |
| The Linux lane's `/tmp/cold-verify-<id>` on the runner | `.cold-artifact.json` in it | Same as the repro stage. |

No marker sets `retention.preserved_at`: coldverify keeps no durable copy of
the whole evidence directory. The job summary of a private repository's Actions
run holds the receipt JSON, not the record or the other files.

The `.cold/` marker covers the whole folder, including the timing history
`.cold/verify-history.jsonl`. All of it is per-checkout and disposable. An
expired marker sends a cleaner back to its own rules, the same as no marker, so
the marker never makes anything easier to delete than it is today.

ColdCleaner reads these markers once its support ships. Until then a marker
protects nothing.

## For other products

**Proof of change (ColdPass, ColdContribute).** When `coldverify` is on `PATH`,
run it in the checkout of the exact revision:

```sh
coldverify --ci --claim "what the change does"
```

The exit code is 0 for `PASS`, 1 for `FAIL` and 2 for `INCOMPLETE`, except in a GitHub Actions job, where a
`--ci` run without `--publish-check` exits as the merge gate (see the README); read the verdict from `result`. Read
`.cold/verify-evidence.json` for the shared record, or
`.cold/verify-receipt.json` for the full receipt. Treat a missing
`coldverify`, any other exit code, or a missing record as "not verified", never
as a pass, and keep your own standalone check as the fallback.

**Lessons (ColdFix).** `coldfix ingest --format cold-verify --input
.cold/verify-trace.jsonl` reads coldverify's failed steps today. ColdFix does not
read run result records yet: `coldpass_sources.normalize()` is the one place it
will. Once it does, `.cold/verify-evidence.json` gives it both failed and
verified verdicts through that shared parser, with no coldverify-specific code.
