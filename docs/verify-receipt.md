# Receipt and result JSON

Coldverify writes two JSON files under the checkout's self-ignoring `.cold/`
folder:

- `.cold/verify-receipt.json`: the whole verdict. It holds the claim, the
  commit, each check, the repro evidence and the test run. Every strict run
  that gets past its preflight (a Git checkout, and a verification skill
  without `--ci`) writes it. `--compat` runs never do.
- `.cold/verify-result.json`: the test run alone, written by
  `ci/verify_trace.py` whenever the tests run (strict or `--compat`) and copied
  into the receipt as `run`. Strict runs always run the tests, even after a
  failed repro, so `run` is null only when the test run wrote no result.

- `.cold/verify-flows.json`: the project's declared verification flows, run
  on the repro base and head (see [Verification flows](#verification-flows))
  and copied into the receipt as `flows`. Each flow's output and artifacts are
  kept under `.cold/flows/<flow>/<base|head>/`.

`.cold/verify-receipt.md` is the same receipt rendered for people and PR
comments. It is not a contract, so don't parse it.

A strict run also writes `.cold/verify-evidence.json`, the same verdict as a
shared ColdStack record, and every run marks `.cold/` with
`.cold/.cold-artifact.json` (owner, expiry, preserved copy). Both are described
in [`evidence.md`](evidence.md).

The machine-readable schema is
[`ci/verify-receipt.schema.json`](../ci/verify-receipt.schema.json) (JSON
Schema 2020-12). Its `$defs/result` is the schema of `verify-result.json`.
`tests/test_verify_receipt.py` checks every receipt the test runs produce
against it, and checks that the table below lists exactly the schema's fields.

## In GitHub Actions

`.cold/` lives in the job's checkout, so it goes away with the runner or the
Coldbox guest. When `GITHUB_STEP_SUMMARY` is set, coldverify also appends the
receipt markdown to the job summary, which GitHub keeps with the workflow run.
When the event payload says the repository is private
(`repository.private: true`), the summary also carries the whole receipt JSON:
the evidence package, with its redacted output excerpts and artifact hashes. A
public or unknown repository gets only the markdown, which is the same text
already posted to the PR.

## Trusted receipt and merge gate

A PR comment can be written by anyone with write access, so it proves nothing
on its own. With `--publish-check`, a strict run inside a GitHub Actions job
also publishes the receipt as a check run named `coldverify receipt`:

- on the PR head commit (the event's `pull_request.head.sha`), or on `HEAD`
  for other events;
- with `external_id` set to `tree`, so the check run names the exact tree the
  verdict is about;
- created `in_progress` first, then completed with conclusion `success` only
  for `PASS`. `FAIL` and `INCOMPLETE` are `failure`. If completing a `PASS`
  fails, coldverify tries to mark the run `failure`, and a run left
  `in_progress` never satisfies the gate. If GitHub applied the `success`
  but its reply was lost and the `failure` mark also fails, the check run
  stays `success` while the local receipt and the job's exit are
  `INCOMPLETE`;
- with the receipt markdown as its summary and the receipt JSON as its text.

The job needs `checks: write`. Only a GitHub App can create check runs, and
`GITHUB_TOKEN` is the GitHub Actions app, so a personal token can't forge one.
`GH_TOKEN` and `GITHUB_TOKEN` are removed from the environment while
the repro and the tests run, so the code under test doesn't inherit them.
That is not isolation: code running as the same user can still read the
verifier's original environment (for example `/proc/<pid>/environ`).
On success the run prints `Coldverify-Receipt: <check run URL>`. Outside
GitHub Actions, or when publishing fails, `checks.receipt` is `INCOMPLETE`.

`coldverify --require-receipt` checks for a published PASS receipt for the
current change. It looks up check runs on the current PR head and check runs
the PR body attaches with a `Coldverify-Receipt: <check run URL or id>` line.
To count, the check run and its receipt must meet all of these conditions.

- GitHub Actions published the `coldverify receipt` check run on the current
  PR head, completed it with conclusion `success`, and set its
  `external_id` to the Git tree of the checkout.
- The checkout is the PR head or, in a pull request job, a merge commit
  whose parents are the current PR base and head, in that order.
- The receipt's `commit` and `tree` name the checkout. Its passing suite
  records those same values in `run.head` and `run.tree`. Its repro
  records the PR base and source head in `repro.base` and `repro.head`.
- The receipt's claim hash matches the hash of the current `## Claim`
  section. The receipt's `builders` match the current `Builder-Model:`
  lines after lowercasing, deduplication, and sorting. Coldverify reads
  those values and the current base and head from GitHub when it checks
  the receipt.

Before opening a PR, run `coldverify --require-receipt --claim 'Your claim'`
with the checked-out head and the repro's default base, the merge base with
`origin/main`. You can attach that check run to the PR once it exists.
Put the attachment under a separate heading so the text of the claim stays
the same. The receipt then needs to match the inputs of the PR. An amend
invalidates a receipt even if it produces the same Git tree.

Exit 0 means `PASS`. Exit 1 means `FAIL` and gives a reason for each
rejected check run. Older receipts missing the suite tree need a fresh run.
Exit 2 means `INCOMPLETE`, for example when GitHub cannot supply the PR
details or check runs.

Limit: the trusted source is the GitHub Actions app, not one workflow. Any
Actions job in the repository with `checks: write`, including a workflow a
PR adds or edits, can publish a `coldverify receipt`. Workflow changes need
review like any other code until the receipt is published by a dedicated
GitHub App whose key PR workflows and the code under test can't reach
(issue #79).

## Verification flows

A project declares a flow by committing an executable file (mode `100755`)
directly in `.fleet/flows/`: a CLI command, an HTTP request script, or a Playwright script
that connects to a shared browser over CDP (`CDP_ENDPOINT` is passed through).
Coldverify has no app-specific drivers; the flow does the work and its exit
code is the answer: 0 passed, 1 failed, anything else (including a timeout
after 300 seconds) means it could not run. Subfolders of `.fleet/flows/` are
for the flows' helpers, and other files there are not run.

The [cart discount fixture](../examples/flows/browser-comparison/README.md) shows a browser flow with base and head screenshots.

A strict run lists the flows at `repro.head`, checks out `repro.base` and
`repro.head` in fresh worktrees, replaces the base checkout's `.fleet/flows/`
with the head's, and runs each flow from inside each checkout, with that
checkout as the working directory, so a flow that finds the application next
to itself tests base on base. A flow that `repro.base` declares and
`repro.head` lacks makes the run `INCOMPLETE` until the branch is rebased.
Each flow gets its own fresh base and head worktrees, so one flow's files never
reach another. Flows run where the repro ran. When `repro.backend` is `local`
or absent, they run on the machine running `coldverify`, and only when the
repro ran on that same host (`repro.host`); otherwise `checks.flows` is
`INCOMPLETE` (`flows run only on the repro host: the repro ran on HOST, this is
HOST`). When it is `container:IMAGE`, `coldbox` or `coldbox-api`, coldverify starts a fresh
container or Coldbox guest, sends each checkout (without `.git`) to a
`/tmp/cold-flow-*` folder there, runs the flow there under `sh`, which waits
for it so the exit recorded is the flow's own, and brings back its output, exit
status and artifacts; nothing of the target runs on the `coldverify` host. A
flow killed by a signal records `128 + N`, so it is never read as a 0 or 1. If
the backend cannot start, a step fails or a flow times out, `checks.flows` is
`INCOMPLETE` with the reason, and the container or guest, with anything the
flow left running, is deleted. When it is `ssh:HOST`, flows are not run
(`flows do not run on an ssh: backend: the repro ran on ssh:HOST`), because
nothing would remove what a flow leaves running on that host. Links in what comes back are never read: `output.txt` and
`exit` must be regular files, and linked artifacts are dropped. The environment is the repro's
clean one (a temporary `HOME`, no tokens), plus `COLD_FLOW_ARTIFACTS`, an empty
folder for the flow's screenshots, HTTP transcripts or other files. Coldverify
keeps the combined stdout and stderr, decoded as UTF-8 with replacement and
redacted, as `.cold/flows/<flow>/<side>/output.txt` and each regular file the flow wrote as
`.cold/flows/<flow>/<side>/artifacts/<path>`. Text artifacts are redacted with the
step-trace redactor; binary artifacts such as screenshots are kept as they
are, so a flow must not write secrets into them. A
flow that exits 1 on base and 0 on head with distinct output from each side
can cover an acceptance criterion when a strict run has access to an
independent verifier model.
The independent verifier reads the flow and its helpers at the head revision
and judges whether the assertions and observed outputs show the claimed
behavior. The receipt lists the flow's `.fleet/flows/<name>` path in
`covered_by`. If there were no new or changed test cases and every criterion
has proof, a linked flow satisfies `checks.repro`. Test failures and
infrastructure errors keep their verdicts.

Coldbox CI guests have no model credential. They record the flow under
`checks.flows` and keep the existing native repro requirement for bug fixes
in the CI merge gate. The informational `--criteria-check` on the host
currently receives test metadata from the PR receipt. A trusted handoff of
flow observations is needed before that check can link a flow to a criterion.

In a local Linux run, Coldverify starts a private user manager, session bus
and journal for flows that name `systemctl`, `systemd-run` or `loginctl`
in their executable. No repository setting is required. Call those commands
in the flow file itself to request the runtime. The sandbox keeps the outer
systemd and D-Bus sockets hidden.

The private runtime needs cgroup v2, systemd, journald and `dbus-daemon`.
On a regular Linux host, Coldverify requests a delegated scope from the
runner's user manager via `/run/user/<uid>`. In a Coldbox guest it requests
a transient service from the guest's system manager with the runner's UID
and no new privileges. If Coldverify cannot start the private runtime,
`checks.flows` is `INCOMPLETE`.

Copy the executable [Coldpass archive example](../examples/flows/coldpass-archive-host)
into a Coldpass checkout's `.fleet/flows/`. The flow loads the checked-out
unit into the private manager and gives it a local archive destination and
synthetic input. A fixture drop-in clears `ConditionHost` and directs
`ExecStart` to the checked-out archive script. The flow saves the unit
result and command output, then compares the reported source host with the
guest hostname. A wrong source host or a failure in the archive script
makes the flow exit 1. An unavailable manager or service setup makes it exit 125.

An infrastructure flow brings up what it checks itself (a process, a container,
or a Coldbox VM through `coldbox vm up`/`vm exec`), then checks a health
endpoint, a port, a `systemctl is-active` state or a resource limit, and tears
it down. When it can't provision on a side (no backend, no permission, a host
at its VM ceiling) it must exit with a code other than 0 or 1, by convention
Coldbox's 125, and print why as its last line. Never pass a refused
provisioning through as exit 1: that reads as red on base and could prove a
change that was never tested. `checks.flows` is then `INCOMPLETE`, naming each
side that could not run with its exit code and last output line, unless some
flow failed on head (exit 1), which makes it `FAIL` first. For example
`flows could not run: health (base exit 125: no permission to start the service)`.

The [items API example](../tests/fixtures/api-persisted-items) starts a
loopback HTTP service from each revision and gives it a fresh SQLite database.
It records the request, response and database rows after an accepted write
and a rejected write. The [receipt tests](../tests/test_verify_receipt.py)
exercise a base that returns `201` without saving the item, a head that saves
it, and a base where SQLite cannot open the store.

## Deployed verification

`coldverify --deployed URL_OR_HOST` checks a live deployment, after the change
has shipped. It runs no repro, no recipe and no base side. It lists the flows
at `HEAD`, keeps only those whose file contains `COLD_DEPLOYED_TARGET`, and runs
each once from a fresh worktree of `HEAD` with `COLD_DEPLOYED_TARGET` set to a
read-only proxy for the target, in the same clean environment and with the same output and artifact
keeping as a strict run (`.cold/flows/<flow>/deployed/`). A flow that doesn't
read the target would only test the checkout, so it is not run and the reason
names it; with none left, `checks.flows` is `INCOMPLETE`.

Before any flow runs, coldverify sends one `HEAD` request, without proxies or
redirects, to the target (a bare host is probed as `http://<host>/`). Any HTTP
answer, whatever its status, counts as contact. With no answer, `checks.flows`
is `INCOMPLETE` and no flow runs. `flows.contact` records the probe.

Each flow gets its own coldverify-owned HTTP proxy on `localhost`.
`COLD_DEPLOYED_TARGET` is the target with its scheme and host swapped for the
proxy's (`http://localhost:<port>/<path>`, or `localhost:<port>` for a bare
host). The proxy relays `GET` and `HEAD` requests for a path to the target,
without proxies or redirects, and answers every other method, and any request
for another host, with 405 without contacting the target. A redirect to the
target is rewritten to point at the proxy; a redirect anywhere else gets 502, so
a redirect-following client cannot leave the proxy. Every request and its
status goes to `.cold/flows/<flow>/deployed/exchanges.txt`, refused ones marked
`refused`. A flow that sent a refused request makes `checks.flows`
`INCOMPLETE`, before any other result. A flow that exits 0 without one 2xx or
3xx answer from the target through the proxy is `INCOMPLETE` too, so a flow
that only mentions `COLD_DEPLOYED_TARGET` cannot pass. When every flow got such
an answer, `flows.contact.flow_contact` is `proven`.

The proxy narrows read-only, it does not seal it: a flow that hard-codes the
real target, opens raw sockets or drives a browser over CDP goes around it, and
a `GET` handler that changes data still runs. Coldverify passes the flow no
tokens. The target is an
`http://` or `https://` URL or a bare host name with an optional port; a URL
with credentials is rejected, because the command line is recorded in the
receipt. `--deployed` cannot be combined with `--compat`, `--publish-check` or
`--require-receipt`, so a deployed `PASS` is never a tree-bound merge receipt.
The receipt's `environment.backend` is always `local`: the flows run where
coldverify runs, whatever `COLD_VERIFY_BACKEND` says.

The receipt is the same file with `deployed` set to the target. Its checks are
only `claim`, `flows` and, as in a strict run, `receipt`: `PASS` needs a claim
and every target-reading flow to exit 0 (`N flows passed against TARGET`).

## Archive

Every strict run that writes a receipt, except a `--deployed` run, also appends one JSON line to
`<git common dir>/coldverify/receipts.jsonl`. The git common dir is shared by
all worktrees of a clone and is never committed, so the archive outlives the
worktree that ran the check. A line holds `schema_version` (1), `archived_at`,
`commit`, `tree`, `pull_request`, `result`, `paths` (the files changed between
`repro.base` and `repro.head`), `change_type` and `proofs`. Each proof is a
`{"strategy", "name"}` pair: `repro` for a proof test, `missing-on-base` for a
new feature's test, `flow` for a flow red on base and green on head.
`change_type` joins the sorted kinds of the changed paths with `+`: `ci`
(`.github/`, `.fleet/`), `tests`, `docs`, otherwise the file suffix (`py`,
`go`), or `unknown` when the repro recorded no base and head. A failed append
prints a warning and never changes the verdict.

`coldverify --history` prints, tab-separated, how many archived runs of each
change type and strategy combination ended `PASS`, `FAIL` or `INCOMPLETE`,
and how many unreadable lines it skipped. A strict run inside a Coldbox guest
archives into the guest's clone, which the guest discards.

A suggestion is the newest `PASS` line with proofs that shares a changed path
with this run. Before the repro, a strict run prints it (changed paths taken
from the merge base of `HEAD` and `origin/main`) so its proofs can be run too.
The receipt names it again for `repro.base`...`repro.head` in `suggestion`,
with `used` true when this run ran at least one of its tests or flows,
whatever the outcome. A suggestion only proposes what to run; it is not a
check and never changes the verdict.

## Versioning

Both files carry `schema_version`. The receipt is at version `10` (version 2
added `checks.tamper`; version 3 added `tree`, `verifier`, `environment`,
`procedure`, `artifacts` and the repro output excerpts; version 4 added
`acceptance_criteria`, `criteria_verifier` and `checks.criteria`; version 5
added `risk` and `checks.risk`; version 6 added `flows` and
`checks.flows`; version 7 added `fingerprint` and `run.reused_from`; version 8
added `deployed`, the `deployed` flow kind and `flows.contact`; version 9 added
`suggestion`; version 10 added `builders`) and
`verify-result.json` is at version `1`. A change that
removes, renames or retypes a field, or adds a value a reader must understand
(a new verdict, check or `tests_source`), bumps the version in
`ci/verify_receipt.py` (`SCHEMA_VERSION`) or `ci/verify_trace.py`
(`RESULT_SCHEMA_VERSION`), in the schema and in this doc, all in the same
change.

Readers must reject a version they don't know. Don't guess at it. Coldverify
does this itself: when `verify-result.json` has another `schema_version`, or
none, the receipt's `tests` check is `INCOMPLETE`
(`.cold/verify-result.json has schema_version 2; this coldverify reads only 1`)
and `run` is `null`.

Every object is closed (`additionalProperties: false`). A new field means a new
schema and doc row in the same change.

## Fields

Verdicts are `PASS`, `FAIL` or `INCOMPLETE`. A check is a two-item array:
`[verdict, reason]`.

| Field | Type | Meaning |
| --- | --- | --- |
| `schema_version` | `10` | Receipt format version. |
| `result` | verdict | Overall verdict. Any `FAIL` check makes it `FAIL`, otherwise any `INCOMPLETE` check makes it `INCOMPLETE`, otherwise `PASS`. In a Coldbox guest the guest `checks.criteria` result is left out, and `reason` contains `criteria not evaluated in a CI guest, so this verdict does not cover them; see the "coldverify criteria" check on the PR head`, then `the builder identity was not checked` (neither `--builder-model` nor the PR's `Builder-Model:` lines are compared with a record the author cannot write, #122). The exit code is 0, 1 or 2 to match. |
| `reason` | string | The failing checks' reasons joined with `; `, or every reason on `PASS`. The last output line is `COLDVERIFY <result>: <reason>`. |
| `claim` | string | The claim from `--claim` or the PR body's `## Claim` section. Empty when none was stated. |
| `commit` | string or null | `git rev-parse HEAD` of the checkout, or null when it can't be read. |
| `tree` | string or null | `git rev-parse HEAD^{tree}` of the checkout: the tree the verdict is about, or null when it can't be read. |
| `fingerprint` | object | The three hashes a later strict `--cache` run must match to reuse this receipt's test run, taken after the repro and before the suite. |
| `fingerprint.tree` | string or null | `HEAD^{tree}` when the checkout has no uncommitted, untracked or ignored files outside `.cold/`, else null, which never matches. |
| `fingerprint.recipe` | string | SHA-256 over coldverify's `ci/fleet-verify.sh` and the repo's `.fleet/verify.sh`, `.fleet/verify.py` and `.fleet/verify.ps1`, each tagged by name. |
| `fingerprint.environment` | string or null | SHA-256 of `environment.host`, `os`, `python`, `backend` and `isolation`, `verifier`, where the suite is sent (`COLD_VERIFY_BACKEND` as configured), and every environment variable the suite inherits (name and value, except `_` and `OLDPWD`; a name matching token, secret, password, API key, credential or private key counts by name only), so a different `PYTEST_ADDOPTS`, `COLD_VERIFY_HOST` or `PATH` never matches; or null when `verifier.commit` is unknown or ends in `-dirty`, which never matches. |
| `pull_request` | integer or null | The branch's open PR, which the receipt is posted to, or null when there is none. Null also when `gh pr view` fails for any reason other than "no pull requests found" or "no git remotes found" (auth, network, rate limit, `gh` missing); `checks.receipt` is then `INCOMPLETE` with gh's error. |
| `builders` | array of strings | The builders the receipt was issued for: the PR's `Builder-Model:` lines when the run had a PR, otherwise `--builder-model`; lowercased, deduplicated and sorted. `--require-receipt` rejects the receipt when the PR's current lines differ. |
| `command` | array of strings | The coldverify command line, starting with `coldverify`. |
| `verifier` | object | The coldverify that judged the run. |
| `verifier.version` | string | The `version` in coldverify's `pyproject.toml`. |
| `verifier.commit` | string or null | The install's `COMMIT` stamp, else `git describe --always --dirty --abbrev=12` of its checkout, or null when neither can be read. A `git archive` install (Coldbox's guest refresh) has the archived commit in `COMMIT` through `export-subst`; a checkout ignores the unexpanded `$Format:%H$`. `coldverify --version` prints the same value. |
| `environment` | object | Where coldverify itself ran, and the backend the suite ran on. The repro may run on `repro.host`. |
| `environment.host` | string | The verification host: the runner named in `/etc/coldbox/host` inside a Coldbox guest, else the machine's node name. |
| `environment.os` | string | `platform.platform()`, for example `Linux-6.8.0-49-generic-x86_64-with-glibc2.39`. |
| `environment.python` | string | The Python version that ran coldverify. |
| `environment.backend` | string | Where the suite ran: `local`, `ssh:<host>`, `container:<image>`, `coldbox` or `coldbox-api`, from `run.backend`; when the run has none, from `repro.backend`, else `COLD_VERIFY_BACKEND`, else `ssh:<repro.host>` when the repro ran on another host, else `local`. Absent in receipts from an older coldverify. |
| `environment.isolation` | `local`, `ssh`, `container`, `coldbox-vm` or `coldbox-guest` | `coldbox-guest` when the backend is `local` inside a Coldbox job VM; else the backend's kind: `ssh`, `container`, `coldbox-vm` for `coldbox` and `coldbox-api`, or `local`. |
| `environment.guest_id` | string or null | The Coldbox guest's own node name, such as `cbx-1794280272-85d96d`, or null outside a guest. |
| `procedure` | array | The commands the run executed, in order: the coldverify command line, each repro case's base and head command (`repro.tests[].commands`), each recipe step, then each flow's base and head command (`flows.flows[].commands`). A deployed run has only the coldverify command line and each flow's one command. |
| `procedure[].step` | string | `coldverify`, `repro base <repro.tests[].name>`, `repro head <repro.tests[].name>`, a `run.steps[].step` name, `flow base <flows.flows[].name>`, `flow head <flows.flows[].name>` or, in a deployed run, `flow deployed <flows.flows[].name>`. |
| `procedure[].command` | string | The command as a shell-quoted string. |
| `artifacts` | array | The evidence files this receipt was built from, under `.cold/`: `verify-repro.json`, `verify-result.json` and `verify-flows.json` when they exist, then every file under `.cold/flows/`. |
| `artifacts[].path` | string | Path relative to the checkout. |
| `artifacts[].sha256` | string | Lowercase hex SHA-256 of the file's bytes when the receipt was written. |
| `acceptance_criteria` | array | What the claim requires. An independent verifier model derives the criteria from the claim and the exact tracked file bytes from the base commit. It links each criterion to proof tests or observed flows from a separate head snapshot. Git export attributes do not change the files in either criteria snapshot. The default route uses a Codex permission profile and probes confinement before each call. The standalone adapter uses a fresh Bubblewrap sandbox with a read-only snapshot and no network. Unavailable confinement leaves the check INCOMPLETE. The repository text it reads is untrusted; the prompts tell it so. Empty when none were derived; `checks.criteria` then says why. |
| `acceptance_criteria[].id` | string | `C1`, `C2`, and so on. |
| `acceptance_criteria[].text` | string | One observable behaviour that must hold after the change for the claim to be true. |
| `acceptance_criteria[].covered_by` | array of strings | The `repro.tests[].name` of each proof test (kind `proof` or `missing-on-base`) whose assertions check this criterion, or the path of a flow whose check and observed outputs cover the criterion. A fresh verifier call judges this from the criteria, flow observations and a read-only snapshot of the head. Empty when no proof covers it. |
| `criteria_verifier` | object or null | The model that wrote and linked the criteria, or null when none was resolved. |
| `criteria_verifier.provider` | string | The provider selected by the ColdStack route or declared by the trusted standalone caller. |
| `criteria_verifier.model` | string | The model selected by the ColdStack route or declared by the trusted standalone caller. Coldverify excludes every `--builder-model`. |
| `criteria_verifier.effort` | string | Its reasoning effort. |
| `suggestion` | object or null | The newest earlier `PASS` in the receipt archive for these paths (see [Archive](#archive)), or null when there is none, for a `--deployed` run, or when the archive could not be read. Never a check: it cannot change `result`. |
| `suggestion.commit` | string | That run's commit. |
| `suggestion.archived_at` | string | When it was archived, UTC. |
| `suggestion.shared_paths` | array of strings | The changed paths this run shares with it. |
| `suggestion.proofs` | array | Its `{"strategy", "name"}` proofs: what to run. |
| `suggestion.proofs[].strategy` | `repro`, `missing-on-base` or `flow` | How that proof proved its change, as in the archive. |
| `suggestion.proofs[].name` | string | The proof test or flow to run. |
| `suggestion.used` | boolean | True when this run ran one of them (in `repro.tests` or `flows.flows`), whatever its outcome. |
| `risk` | object or null | The change's risk tier, or null when it could not be told; `checks.risk` then says why. |
| `risk.tier` | `normal` or `high` | `high` when the change, compared with the merge base of `repro.base` and `repro.head`, adds, edits, deletes or renames a path matching a glob in the `high` list of `.fleet/risk.json` at that merge base or at `repro.base` (both lists count, so a branch older than the config still gets it), or touches `.fleet/risk.json` itself. A PR can't lower its own tier by editing the file. Otherwise `normal`. |
| `risk.config` | `.fleet/risk.json` or null | The config the tier came from, or null when the merge base has none. The file is `{"high": ["<git :(glob) pathspec>", ...]}`, such as `{"high": [".github/**", "**/auth/**", "**/migrations/**"]}`; anything else makes `checks.risk` `INCOMPLETE`. |
| `risk.matched` | array of strings | The changed paths that made the change `high`, sorted. Empty when `normal`. |
| `risk.signoff` | object or null | The independent verifier's sign-off on a `high` change, or null when none was given. |
| `risk.signoff.provider` | string | Its provider. Only `codex` is run. |
| `risk.signoff.model` | string | The model ColdStack's model router chose for `auditor/verifier` with every `--builder-model` excluded, as for `criteria_verifier`. It is never a builder. |
| `risk.signoff.effort` | string | Its reasoning effort. |
| `risk.signoff.approved` | boolean | Whether it signed off. It is given the rules first, then the claim, the matched paths and the full diff from the merge base as JSON strings it is told are the author's data, not instructions, and a read-only snapshot of the head. It approves only when the diff does what the claim says and nothing more in the high-risk paths. |
| `risk.signoff.reason` | string | Its reason, in its own words. |
| `checks` | object | One check per gate, in the order they ran. |
| `checks.claim` | check | `PASS` when a claim was stated, else `INCOMPLETE`. |
| `checks.repro` | check | Absent in a deployed run. The repro gate requires proof and no blocking transition. When the test gate found no new or changed cases, a criterion-linked observed flow can supply the proof. Acceptance (0/0) and unchanged non-zero tests never count as proof. `INCOMPLETE` with the error when the repro host is unreachable or drops the connection, staging or the transfer to it fails, or `git fetch` or `git worktree add` fails. |
| `checks.tests` | check | Absent in a deployed run. The test run's verdict (`run.result`, `run.reason`; `PASS` with `reused from <check run URL>: ...` when the run was reused), or `INCOMPLETE` when the tests did not run or `verify-result.json` has an unknown version. |
| `checks.tamper` | check | Absent in a deployed run. Test weakening and evidence tampering, compared with the merge base of `repro.base` and `repro.head`. `INCOMPLETE` when a test file is deleted or moved out of the test paths; a Python test case is deleted; a `skip`, `skipif`, `skipIf`, `skipUnless`, `skipTest`, `SkipTest`, `xfail`, `expectedFailure`, `xit`, `xdescribe` or `xtest` marker appears on a line of a test file or `conftest.py` that base didn't have, or is added to a pre-existing Python test case; an assertion (`assert`, or a call named `assert*`, `fail*` or `raises`) of a pre-existing Python test case, including methods of any top-level `Test*` class, is removed, changed, or moved under different control flow, or the case gains a `return`, `break` or `continue`; a file under `.fleet/flows/` is edited, deleted or renamed (`flow changed; needs review: PATH`; adding one is fine); a `.fleet/verify.*` file is added or edited, or deleted while another stays (`recipe changed; needs review: PATH`), or has uncommitted changes; the PR's `## Claim` section, re-read live with `gh pr view` after the run, differs from `claim`, or its `Builder-Model:` lines differ from those the run started with; or any of these comparisons fails. Otherwise `PASS`. |
| `checks.criteria` | check | Absent in a deployed run. In a Coldbox guest always `INCOMPLETE` (`acceptance criteria are checked by the separate "coldverify criteria" check on the PR head, run on a trusted host; no model credential in the CI guest by design`), without calling `cold` or `codex`. `PASS` when the verifier derived at least one acceptance criterion and every one is covered by a proof test or observed flow. `INCOMPLETE` when a criterion has no proof (`acceptance criteria no proof test covers: C2 (...)`); when there is no claim, no builder (no `--builder-model`: a `Builder-Model:` line in the PR description alone never chooses a verifier, #122), PR `Builder-Model:` lines (anywhere in the description, `## Claim` included) that do not name the same models as `--builder-model` (a missing line too) when the run has a PR, a failed PR lookup (the lines cannot be compared; the high-risk sign-off makes the same comparison), or no repro base and head; when a tracked path cannot be safely materialized in a criteria snapshot (the reason names the path); or when `cold`, `codex` or the verifier's answer fails (in a Coldbox guest a missing tool says `no model credential in the CI guest by design`) (missing tool, non-zero exit, timeout, malformed or incomplete JSON, a criterion over 500 characters, a link to a test that is not a proof, or a chosen model that is a builder). |
| `checks.risk` | check | Present for a `high` change or when the tier could not be told; absent for `normal`. `PASS` only when the independent verifier signed off (`high risk (PATHS): MODEL signed off: REASON`). `INCOMPLETE`, never `PASS`, when it did not sign off; when another check has not passed, so no sign-off is sought; when there is no repro base and head, `.fleet/risk.json` at base is invalid, or Git fails; when the diff (taken with `--text`, so `-diff` attributes can't hide content) is over 200,000 characters, or a matched file that exists at head is missing from the head snapshot (`export-ignore`); or when `cold`, `codex` or the answer fails as for `checks.criteria`. |
| `checks.flows` | check | Present only when the checkout's `HEAD`, `repro.head` or `repro.base` (whichever are available locally) declares flows. `FAIL` when a flow exits 1 on head (`flows failed on head: NAME`). Otherwise `INCOMPLETE` when a flow exits anything but 0 or 1 on either side, or the flows were not run (a local repro ran on another host: `flows run only on the repro host: ...`; the repro ran on an `ssh:` backend: `flows do not run on an ssh: backend: ...`; the repro's container or Coldbox backend could not start or return a flow's result: `flows: the ... backend gave no answer: ...`; a flow on base is missing from head (`flows on base missing from head (rebase onto base): NAME`), no repro base and head, `git fetch` or `git worktree add` failed). Otherwise `PASS` (`N flows passed on head, K of them failed on base`). In a deployed run it is always present: `FAIL` when a flow exits 1 against the target (`flows failed against TARGET: NAME`), `INCOMPLETE` when one exits anything else or no flow reads `COLD_DEPLOYED_TARGET`, otherwise `PASS` (`N flows passed against TARGET`, then the flows not run because they do not read it). |
| `checks.receipt` | check | `INCOMPLETE` while a PR exists and its post is not confirmed. It is saved before posting (`posting the receipt to PR #N was not confirmed`), so a run killed mid-post never leaves `PASS`, and kept when posting fails (`the receipt could not be posted to PR #N`). With `--publish-check` it is also saved as `INCOMPLETE` before publishing (`publishing the receipt as a check run was not confirmed`), and kept when publishing fails (`the receipt was not published as a check run: REASON`, including outside GitHub Actions). Absent once every post and publish succeeds, or when there is no PR and no `--publish-check`. |
| `repro` | object or null | The repro evidence from `.cold/verify-repro.json`, or null when the repro wrote none. Only `result` is always present. A transport or prepare error writes just `result` and `error`. |
| `repro.result` | verdict | The repro gate's verdict. |
| `repro.tests` | array | One entry per reproduced test. |
| `repro.tests[].name` | string | `<path>::<Class>.<method>` or `<path>::<function>`. |
| `repro.tests[].base_exit` | integer | The test's exit on the base commit. Proof requires 1 (assertion failure), or 3 for `missing-on-base`. |
| `repro.tests[].kind` | string | `proof`, `missing-on-base`, `acceptance`, `unchanged`, `regression`, or `unproven`. A strict native Python 1/0 replay is `unproven` pending runner-observed changed-line evidence. An import or attribute on base that names a function, class, variable or module added on head in that same importable module or class can record base exit 3. A 3/0 Python replay is also `unproven`. Older receipts can record such a proof as `missing-on-base` or omit `kind`. |
| `repro.tests[].head_exit` | integer | The test's exit on the head commit. Proof requires 0 (passed). |
| `repro.tests[].base_output` | string | The case's stdout and stderr on base, redacted with the step-trace redactor (private keys, JWTs, `token`/`secret`/`password`/`api_key` assignments, authorization headers, URL credentials, GitHub/AWS/Slack/OpenAI tokens, and secret-named environment values) and cut to one line of at most 1000 characters, head and tail kept. Only the first and last MiB of output are held, in memory. Absent in evidence from an older coldverify. |
| `repro.tests[].head_output` | string | The same excerpt for the head run. |
| `repro.tests[].commands` | array of strings | The exact base and head commands the gate ran for this case, shell-quoted. Native Python runs that side's Python and the verifier's `verify_repro.py --case` with the test file and name. Absent in evidence from an older coldverify. |
| `repro.base` | string or null | Base commit SHA. For a PR merge checkout, this is its authenticated first parent. A stale event base must be its ancestor, and it must belong to the fetched `origin/<GITHUB_BASE_REF>` history. Full checkouts use that local ref without network access. |
| `repro.head` | string or null | Exact PR head commit SHA, including when the checkout is a merge whose second parent must match it. |
| `repro.host` | string | Host that ran the repro. |
| `repro.platform` | string | `linux`, `darwin` or `windows`. |
| `repro.backend` | string | The execution backend the repro ran on (see `environment.backend`). Absent when the repro failed before a backend was chosen, and in evidence from an older coldverify. |
| `repro.opt_out_reason` | string or null | The `--no-repro-reason` text when the repro was skipped on purpose. |
| `repro.error` | string | Why the repro could not run or judge. Transport and prepare errors are redacted and cut to one line of at most 500 characters. |
| `flows` | object or null | The flows evidence from `.cold/verify-flows.json`, or null when the checkout declares no flows. Always present in a deployed run. |
| `flows.result` | verdict | The same verdict as `checks.flows`. |
| `flows.reason` | string | The same reason as `checks.flows`. |
| `flows.base` | string or null | The commit the flows ran on as base: `repro.base`. Null in a deployed run. |
| `flows.head` | string or null | The commit the flows ran on as head, and whose `.fleet/flows/` was run: `repro.head`, or `HEAD` in a deployed run. |
| `flows.host` | string | Host that ran the flows. With the repro's `backend` `local` (or absent), where coldverify ran; with `container:`, `coldbox` or `coldbox-api`, the node name of the fresh guest coldverify started for the flows. Each flow's checkout is sent there and run there, and its output and artifacts are brought back, so nothing of the target runs on the coldverify host. A failed transfer, or an exit status the backend and the flow's shell disagree on, makes `checks.flows` `INCOMPLETE`. |
| `flows.contact` | object or null | Deployed runs only: how contact with the target was established. `null` when the probe got no answer or no flow reads the target. |
| `flows.contact.established_by` | string | Always `probed by coldverify`. |
| `flows.contact.probe` | string | The probe and its answer: `HEAD <url> answered HTTP <status>`. |
| `flows.contact.flow_contact` | string | `proven` when every flow that ran got a 2xx or 3xx answer from the target through coldverify's read-only proxy, else `not proven`. |
| `flows.flows` | array | One entry per flow, by file name. Empty when the flows could not be set up. |
| `flows.flows[].name` | string | The flow's file name in `.fleet/flows/`. |
| `flows.flows[].base_exit` | integer or null | The flow's exit on base. 127 when it could not be started. Null in a deployed run. |
| `flows.flows[].head_exit` | integer | The flow's exit on head. |
| `flows.flows[].kind` | string | `proof` (1 on base, 0 on head), `acceptance` (0 and 0), `regression` (1 on head) or `unrunnable` (any other exit). In a deployed run, `deployed` (0 against the target), `regression` (1) or `unrunnable`. |
| `flows.flows[].base_output` | string | The flow's stdout and stderr on base, redacted and cut like `repro.tests[].base_output`. The whole redacted output is `.cold/flows/<name>/base/output.txt`. |
| `flows.flows[].head_output` | string | The same for head. |
| `flows.flows[].commands` | array of strings | The base and head commands, shell-quoted; in a deployed run, the one command, with `COLD_DEPLOYED_TARGET`. |
| `deployed` | string or null | The `--deployed` target the flows ran against, or null for a strict run. When set, `checks` has only `claim`, `flows` and possibly `receipt`; `repro`, `run`, `risk` and `criteria_verifier` are null and `acceptance_criteria` is empty. |
| `run` | object or null | The test run: the contents of `verify-result.json`, or null when the tests did not run. |
| `run.schema_version` | `1` | Result format version. |
| `run.result` | verdict | The test run's verdict. |
| `run.reason` | string | Why, such as `3 tests ran fresh and passed` or `exit 7`. |
| `run.head` | string or null | `HEAD` when the tests ran, or null outside Git. |
| `run.tree` | string or null | Git tree of the worktree the suite used. Absent or null when Coldverify could not bind the worktree to a committed tree. |
| `run.command` | array of strings | The test command coldverify ran: `bash <install>/ci/fleet-verify.sh`, which runs the repo's recipe, possibly behind a low-priority wrapper. |
| `run.exit_code` | integer | The suite launcher's exit code. |
| `run.infrastructure_phase` | `setup` or `execution` | Present when the suite launcher reports an infrastructure abort. The required suite gate fails. The test check records `INCOMPLETE`, or `FAIL` if signed JUnit records a failure or error. |
| `run.tests_ran` | integer | Tests that ran and were not skipped. |
| `run.tests_source` | `junit`, `output` or null | Where the count came from. `output` means it was counted from printed text, not JUnit XML, and is never enough to pass. |
| `run.backend` | string | Where the suite ran, as `environment.backend` spells it. The Linux or macOS lane records it when there is evidence of where the suite started or finished. Older Coldverify versions may omit it. |
| `run.steps` | array | One entry per recipe step, in order. |
| `run.steps[].step` | string | Step name (a `::group::` title, or `verify`). |
| `run.steps[].command` | string | The step's command. |
| `run.steps[].status` | `pass`, `fail` or `skip` | `skip` covers skipped and cached steps. |
| `run.steps[].duration_ms` | integer | Step time in milliseconds. |
| `run.reused_from` | string | Present only when this run was not executed but reused, with `coldverify --cache` on a `normal`-risk change, from a trusted receipt: the URL of a `coldverify receipt` check run published by GitHub Actions with conclusion `success`, `external_id` equal to `fingerprint.tree`, and a schema-9 receipt whose `fingerprint` equals this run's and whose `run` is a valid `verify-result.json` with `result` `PASS` (a host without `jsonschema` never reuses). The other `run` fields are that receipt's, with `reason` prefixed `reused from <URL>: `. A `high` change, a lookup that fails, or any mismatch runs the suite fresh. |

## `verify-result.json`

`verify-result.json` has exactly the `run.*` fields above, without the `run.`
prefix. Its schema is `$defs/result` in the same schema file.

## Example

This receipt shows an earlier verifier that accepted Python hit reports. Current
strict native Python replays record these exits as `unproven`.

```json
{
  "schema_version": 10,
  "claim": "add() returns the sum",
  "commit": "079aa7c1f0e5d1e4b9a1c6f2d8e3b7a4c5d6e7f8",
  "tree": "5c1e8f2a9b3d4c6e7f8091a2b3c4d5e6f7081920",
  "fingerprint": {"tree": "5c1e8f2a9b3d4c6e7f8091a2b3c4d5e6f7081920",
                  "recipe": "0d4f9a1c2b3e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8",
                  "environment": "7e6d5c4b3a291807f6e5d4c3b2a19080f7e6d5c4b3a291807f6e5d4c3b2a1908"},
  "pull_request": 7,
  "builders": ["claude-opus-5-5"],
  "command": ["coldverify", "--ci"],
  "verifier": {"version": "0.1.0", "commit": "edb46820a1b2"},
  "environment": {"host": "build-host", "os": "Linux-6.8.0-49-generic-x86_64-with-glibc2.39", "python": "3.12.3",
                  "isolation": "coldbox-guest", "guest_id": "cbx-1794280272-85d96d"},
  "procedure": [
    {"step": "coldverify", "command": "coldverify --ci"},
    {"step": "repro base tests/test_calc.py::Add.test_sum", "command": "/tmp/cold-repro-x/base-venv/bin/python /opt/coldverify/ci/verify_repro.py --case tests/test_calc.py Add.test_sum"},
    {"step": "repro head tests/test_calc.py::Add.test_sum", "command": "/tmp/cold-repro-x/head-venv/bin/python /opt/coldverify/ci/verify_repro.py --case tests/test_calc.py Add.test_sum /tmp/cold-repro-x/changed-lines.json /tmp/cold-repro-x/hits-0.json"},
    {"step": "verify", "command": "bash /opt/coldverify/ci/fleet-verify.sh"}
  ],
  "artifacts": [
    {"path": ".cold/verify-repro.json", "sha256": "3f0c5d1e8a2b4c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d"},
    {"path": ".cold/verify-result.json", "sha256": "9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a0b9c8d7e6f5a4b3c2d1e0f9a8b"}
  ],
  "acceptance_criteria": [
    {"id": "C1", "text": "add(2, 3) returns 5", "covered_by": ["tests/test_calc.py::Add.test_sum"]}
  ],
  "criteria_verifier": {"provider": "codex", "model": "gpt-6.1-terra", "effort": "high"},
  "risk": {"tier": "normal", "config": ".fleet/risk.json", "matched": [], "signoff": null},
  "suggestion": null,
  "checks": {
    "claim": ["PASS", "claim stated"],
    "repro": ["PASS", "1 repro tests failed on base and passed on head"],
    "tests": ["PASS", "3 tests ran fresh and passed"],
    "tamper": ["PASS", "no tests weakened, recipe and claim unchanged"],
    "criteria": ["PASS", "1 acceptance criteria, each covered by a proof test"]
  },
  "repro": {
    "result": "PASS",
    "tests": [{"name": "tests/test_calc.py::Add.test_sum", "base_exit": 1, "head_exit": 0, "kind": "proof",
               "base_output": "F ==== FAIL: test_sum (test_calc.Add.test_sum) ---- AssertionError: -1 != 5 ---- Ran 1 test in 0.000s FAILED (failures=1)",
               "head_output": ". ---- Ran 1 test in 0.000s OK",
               "commands": ["/tmp/cold-repro-x/base-venv/bin/python /opt/coldverify/ci/verify_repro.py --case tests/test_calc.py Add.test_sum",
                            "/tmp/cold-repro-x/head-venv/bin/python /opt/coldverify/ci/verify_repro.py --case tests/test_calc.py Add.test_sum /tmp/cold-repro-x/changed-lines.json /tmp/cold-repro-x/hits-0.json"]}],
    "base": "4b82e0c9d1f2a3b4c5d6e7f8091a2b3c4d5e6f70",
    "head": "079aa7c1f0e5d1e4b9a1c6f2d8e3b7a4c5d6e7f8",
    "host": "build-host",
    "platform": "linux",
    "opt_out_reason": null
  },
  "run": {
    "schema_version": 1,
    "result": "PASS",
    "reason": "3 tests ran fresh and passed",
    "head": "079aa7c1f0e5d1e4b9a1c6f2d8e3b7a4c5d6e7f8",
    "command": ["bash", "/opt/coldverify/ci/fleet-verify.sh"],
    "exit_code": 0,
    "tests_ran": 3,
    "tests_source": "junit",
    "steps": [{"step": "verify", "command": "bash /opt/coldverify/ci/fleet-verify.sh", "status": "pass", "duration_ms": 1840}]
  },
  "flows": null,
  "deployed": null,
  "result": "PASS",
  "reason": "claim stated; 1 repro tests failed on base and passed on head; 3 tests ran fresh and passed; no tests weakened, recipe and claim unchanged; 1 acceptance criteria, each covered by a proof test"
}
```
