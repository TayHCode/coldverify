#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

export PYTHONDONTWRITEBYTECODE=1
filter() { python3 "${COLD_VERIFY_FILTER:-ci/verify_filter.py}" run "$@"; }
filtered() {
  local glob=$2 file list pool=() alone=() pool_status=2 alone_status=2
  shift 2
  list=$(mktemp)
  git ls-files -z -co --exclude-standard -- ":(glob)$glob" > "$list" || { rm -f "$list"; return 1; }
  while IFS= read -r -d '' file; do
    if [[ $file == *[][*?\\]* ]]; then echo "INCOMPLETE: $file has a glob character in its name; rename it" >&2; return 1; fi
    if grep -qE '^[[:space:]]*@run_alone([[:space:]]|$)' "$file"; then alone+=(--suite "$file"); else pool+=(--suite "$file"); fi
  done < "$list"
  rm -f "$list"
  if (( ${#pool[@]} )); then filter "${pool[@]}" "$@"; pool_status=$?; fi
  if (( ${#alone[@]} )); then COLD_VERIFY_JOBS=1 filter "${alone[@]}" "$@"; alone_status=$?; fi
  (( pool_status == 0 || pool_status == 2 )) || return "$pool_status"
  (( alone_status == 0 || alone_status == 2 )) || return "$alone_status"
  return $(( pool_status == 2 && alone_status == 2 ? 2 : 0 ))
}
export -f filter filtered

suite=(--suite 'tests/test*.py' -- python3 ci/junit_unittest.py discover -s '{dir}' -p '{name}')
if [ -n "${COLD_VERIFY_PREVIEW:-}" ]; then
  exec python3 "${COLD_VERIFY_FILTER:-ci/verify_filter.py}" run "${suite[@]}"
fi

python3 "${COLD_VERIFY_PARALLEL:-ci/verify_parallel.py}" \
  --step 'tests' "filtered --suite 'tests/test*.py' -- python3 ci/junit_unittest.py discover -s '{dir}' -p '{name}'"
