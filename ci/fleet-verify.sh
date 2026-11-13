#!/usr/bin/env bash
# Run the current repo's tests in a Coldbox VM, on its approved native macOS host, or locally when no runner is configured.
#   ci/fleet-verify.sh [repo-dir]
set -euo pipefail
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES
dir=$(cd "${1:-.}" && { git rev-parse --show-toplevel 2>/dev/null || pwd; })
here=$(cd "$(dirname "$0")" && pwd)
platform=$(python3 "$here/linux-verify.py" --platform "$dir")
if [ "$platform" = macos ]; then
  if ! git -C "$dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    stage=$(mktemp -d)
    trap 'rm -rf "$stage"' EXIT
    python3 "$here/linux-verify.py" --export "$stage/checkout" "$dir"
    dir=$stage/checkout
  fi
  python3 "$here/mac-verify.py" "$dir"
  exit
fi
if [ "$platform" = linux ]; then
  exec python3 "$here/linux-verify.py" "$dir"
fi
if [ "$platform" = linux-generic ] || [ "$platform" = local ]; then
  exec python3 "$here/linux-verify.py" --generic "$dir"
fi
echo "INCOMPLETE: unsupported verification platform '$platform'; expected local, Linux or approved native macOS." >&2
exit 2
