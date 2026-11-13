# coldverify step cache

A verify recipe can wrap a step so `coldverify` skips it when nothing the step
depends on has changed since it last passed.

## Invariant

A step is skipped only when all of these hold:

1. Its cache key matches an entry. The key is a sha256 over the step id, the
   bytes of the repo recipe (`.fleet/verify.sh`, `.fleet/verify.py`, `.fleet/verify.ps1`, whichever
   exist) and the name and bytes of every declared input. Each field is tagged
   and length-prefixed, so different names and contents cannot produce the same
   key.
2. The entry was written by a run of that step that exited 0, at a recorded
   commit (`head`).
3. The entry is valid. Missing, unreadable, corrupt or wrong-schema entries are
   a miss and the step runs.

A step also runs, and is never recorded, when an input is missing, is absolute,
leaves the repository (`..` or a symlink out), is a directory containing a symlink, or when the repository has no
commit. A failed step is never recorded, and neither is a pass whose recipe or
inputs changed while it ran. Undeclared inputs are invisible to the
cache, so declare everything the step reads.

Release and merge-gate runs pass `coldverify --gate`. A gated step is reused
only when the entry's `tree` equals the current tree, where the current tree is
`HEAD^{tree}` of a working tree with no uncommitted, untracked or ignored
files. A dirty tree has no tree hash, so every gated step runs. Ignored files
count because a step can read them and the tree hash cannot see them; only
`.cold/`, the wrapper's own state, is exempt. The tree is checked again
when each step starts and when it passes, so a recipe that edits tracked files
before or during a step cannot reuse or record a clean-tree pass.

A cached step was not run fresh, so a run that reuses one ends `INCOMPLETE`
(exit 2), never `PASS`. `coldverify` runs every step by default and still
records the passes; `coldverify --cache` reuses them. In a strict run of a
`normal`-risk change, `--cache` first looks for a trusted PASS receipt whose
tree, recipe and environment hashes all match (`fingerprint` in
`docs/verify-receipt.md`) and reuses its whole test run as `PASS`, saying
`reused from <check run URL>`. Runs without the cache,
and gate runs, also run every test file selected by the recipe, without
change-based filtering.

## Use in a recipe

```bash
python3 "${COLD_VERIFY_CACHE:?}" step "link check" --input docs --input scripts/check_links.py -- \
  python3 scripts/check_links.py
```

`--input` is a file, or a directory whose files are all included. Paths are
relative to the repository root, which is the recipe's working directory. The
wrapper prints the usual markers (`::group::`, `::endgroup::`,
`FAILED (CODE): TITLE`) and, on a hit, `::skip::TITLE`, so the
[trace](verify-trace.md) shows `skip`. It also prints `::inputs::KEY`, which the
trace records as that step's `inputs_hash`.

`coldverify` sets `COLD_VERIFY_CACHE` (this script), `COLD_VERIFY_CACHE_DIR`,
`COLD_VERIFY_HEAD`, `COLD_VERIFY_TREE`, `COLD_VERIFY_GATE` and
`COLD_VERIFY_NO_CACHE` for the recipe (and the [test filter](verify-filters.md) variables), replacing any inherited values. Without
`COLD_VERIFY_CACHE_DIR` and `COLD_VERIFY_HEAD` the wrapper simply runs the step.
Hosts that do not forward the environment (the Windows ssh run) therefore
always run every step.

## Storage

One JSON file per key in `.cold/verify-cache/<key>.json`, ignored by git:

```json
{"schema_version":1,"key":"9f2c...","step":"link check","head":"079aa7c...","tree":"4b82..."}
```

`tree` is null when the working tree was dirty. Delete the directory to clear
the cache. Entries are replaced atomically.
