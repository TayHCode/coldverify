#!/usr/bin/env bash
set -euo pipefail
unit="${BASH_SOURCE[0]%/*}/archive.service"
if [[ ! -s "$unit" ]]; then
    echo 'missing archive service unit' >&2
    exit 2
fi
grep -qx 'ProtectHome=read-only' "$unit"
