#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

export PYTHONDONTWRITEBYTECODE=1
if [ -n "${COLD_VERIFY_PREVIEW:-}" ]; then
  exec python3 "${COLD_VERIFY_FILTER:-ci/verify_filter.py}" run --suite 'tests/test*.py' -- python3 ci/junit_unittest.py discover -s '{dir}' -p '{name}'
fi

python3 "${COLD_VERIFY_PARALLEL:-ci/verify_parallel.py}" --step 'tests' 'python3 scripts/run_tests.py'
