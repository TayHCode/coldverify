# Compare a browser flow on base and head

The fixture creates two commits for a cart that starts at $20.00. On each commit, the flow opens its own Chrome tab, clicks **Apply $2 discount**, captures the displayed total, and closes the tab. Base shows $19.00 and exits 1. Head shows $18.00 and exits 0.

## Run the fixture

Run from the Coldverify checkout on a host with Docker or Podman. Set `CDP_ENDPOINT` to the shared Chrome endpoint reachable from the container. The fixture uses Node 22 in `docker.io/library/node:22-bookworm` and attaches to the running browser over CDP.

```sh
bash examples/flows/browser-comparison/run.sh browser-evidence
```

Use a fresh output directory for each run. Coldverify writes a receipt and the collected artifacts for each run. The comparison checks both PNGs against the SHA-256 hashes in the receipt. The second run uses a closed endpoint and requires `INCOMPLETE` with no screenshots.

The local Linux sandbox disables networking. Coldbox job VMs block private-network egress, so a shared Chrome endpoint on a private network needs a container with access to that endpoint.

## Recorded comparison

The run used Coldverify `6504d12` in a disposable builder and Chrome over CDP. Coldverify recorded one passing suite test in each run.

| Revision | Total after the click | Exit | Screenshot |
| --- | ---: | ---: | --- |
| Base | $19.00 | 1 | [Base PNG](evidence/comparison/.cold/flows/check-discount/base/artifacts/discount.png) |
| Head | $18.00 | 0 | [Head PNG](evidence/comparison/.cold/flows/check-discount/head/artifacts/discount.png) |

The comparison receipt records `checks.flows` as `PASS` and contains both PNG hashes. The overall strict receipt stays `INCOMPLETE` because there are no changed native test cases and no independent criteria evaluation. The unavailable endpoint produces `INCOMPLETE` for both the flow and the overall receipt. [Comparison receipt excerpt](evidence/comparison/receipt-excerpt.json), [unavailable endpoint receipt excerpt](evidence/unavailable/receipt-excerpt.json).

The excerpts omit runtime commands and retain the original PNG paths and hashes. `run.sh` produces the full receipts.
