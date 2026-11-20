#!/usr/bin/env bash
set -euo pipefail

example=$(cd "$(dirname "$0")" && pwd)
verifier=$(realpath "$example/../../../coldverify")
output=$(realpath -m "${1:?usage: run.sh EVIDENCE_DIRECTORY}")
backend=${COLD_VERIFY_BACKEND:-container:docker.io/library/node:22-bookworm}
: "${CDP_ENDPOINT:?set CDP_ENDPOINT to a running shared Chrome browser}"

export GIT_AUTHOR_NAME=Fixture GIT_AUTHOR_EMAIL=fixture@localhost
export GIT_COMMITTER_NAME=Fixture GIT_COMMITTER_EMAIL=fixture@localhost
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
mkdir -p "$output"
mkdir "$output/comparison" "$output/unavailable"
work=$(mktemp -d "$output/.fixture.XXXXXX")
trap 'rm -rf "$work"' EXIT
cd "$work"

git init -q -b main
sed 's/const discount = 2;/const discount = 1;/' "$example/cart.html" > cart.html
mkdir -p tests .fleet
cat > tests/test_cart.py <<'PY'
from pathlib import Path
import unittest


class Cart(unittest.TestCase):
    def test_cart_starts_at_twenty_dollars(self):
        self.assertIn('>Total: $20.00</p>', Path('cart.html').read_text())
PY
cat > .fleet/verify.sh <<'SH'
set -e
python3 "$COLD_VERIFY_UNITTEST" discover -s tests -v
SH
git add .
git commit -qm "Base applies a \$1 discount"
git update-ref refs/remotes/origin/main HEAD
git switch -qc correct-discount
cp "$example/cart.html" cart.html
install -D -m 755 "$example/check-discount" .fleet/flows/check-discount
git add .
git commit -qm "Apply the advertised \$2 discount"

for leg in comparison unavailable; do
    endpoint=$CDP_ENDPOINT
    if [[ $leg == unavailable ]]; then
        endpoint=http://localhost:1
    fi
    destination=$output/$leg
    mkdir -p "$destination/.cold"
    status=0
    CDP_ENDPOINT=$endpoint COLD_VERIFY_BACKEND=$backend python3 "$verifier" --ci --no-cache \
        --claim "Clicking Apply \$2 discount changes the displayed cart total from \$20.00 to \$18.00" \
        > "$destination/run.log" 2>&1 || status=$?
    cat "$destination/run.log"
    cp -R .cold/{verify-receipt.json,verify-repro.json,verify-result.json,verify-flows.json,flows} "$destination/.cold/"
    python3 - "$leg" "$status" "$destination" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

leg, status, output = sys.argv[1:]
root = Path(output)
receipt = json.loads((root / '.cold/verify-receipt.json').read_text())
assert receipt['checks']['tests'][0] == 'PASS', receipt['checks']['tests']
assert receipt['run']['tests_ran'] == 1, receipt['run']
assert int(status) in (0, 2), receipt['checks']
flow, = receipt['flows']['flows']
base, head = flow['base_exit'], flow['head_exit']
screenshots = {}
for item in receipt['artifacts']:
    path = item['path']
    data = (root / path).read_bytes()
    assert hashlib.sha256(data).hexdigest() == item['sha256'], path
    if path.endswith('.png'):
        assert data.startswith(b'\x89PNG\r\n\x1a\n'), path
        screenshots[path] = item['sha256']
if leg == 'comparison':
    assert (base, head, flow['kind']) == (1, 0, 'proof'), flow
    assert receipt['checks']['flows'][0] == 'PASS', receipt['checks']['flows']
    for side, expected in [('base', '$19.00'), ('head', '$18.00')]:
        assert flow[f'{side}_output'].endswith(f'Observed Total: {expected}.'), flow
        path = f'.cold/flows/check-discount/{side}/artifacts/discount.png'
        assert path in screenshots, path
    assert len(set(screenshots.values())) == 2, screenshots
else:
    assert int(status) == 2 and receipt['result'] == 'INCOMPLETE', receipt['checks']
    assert (base, head, flow['kind']) == (125, 125, 'unrunnable'), flow
    assert receipt['checks']['flows'][0] == 'INCOMPLETE' and not screenshots, receipt['checks']['flows']
print(f"{leg}: {base}/{head} {receipt['checks']['flows'][0]}, {len(screenshots)} receipt-linked screenshots")
PY
done
