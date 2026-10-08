#!/usr/bin/env bash
# The whole flow on a throwaway regtest node: fund the demo wallet, prove everything once,
# spend a coin, prove only the change, then produce the year's balance report, and one that leaves dust out.
# Needs a Bitcoin Core binary: BITCOIN_CORE_DIR, or refcheck/fetch.sh run in the bip322-core checkout
# this venv was set up from (setup.sh --core).
# Usage: examples/report_walkthrough.sh [DIR]   the artifacts go to a fresh temporary directory, or to DIR,
# which must be new or empty: nothing that is already there is ever deleted or overwritten.
set -euo pipefail
if [ $# -gt 0 ]; then  # first of all: a directory that cannot be used is refused before anything is looked for or started
  WORK=$1
  if [ -e "$WORK" ] && { [ ! -d "$WORK" ] || [ -n "$(ls -A "$WORK")" ]; }; then
    echo "error: $WORK exists and is not an empty directory; give a new or empty one" >&2
    exit 2
  fi
  mkdir -p "$WORK"
else
  WORK=$(mktemp -d "${TMPDIR:-/tmp}/bip322-report-walkthrough.XXXXXX")
fi
WORK=$(cd "$WORK" && pwd)  # absolute: the commands below run from the repository root
cd "$(dirname "$0")/.."
CORE=$(.venv/bin/python -m bip322audit.testing core)
DATADIR=$(mktemp -d)
PORT=$(.venv/bin/python -m bip322audit.testing port)
CLI="$CORE/bin/bitcoin-cli -regtest -datadir=$DATADIR -rpcport=$PORT -rpcuser=demo -rpcpassword=demo"
DEV=.venv/bin/bip322-dev; AUDIT=.venv/bin/bip322-audit; REPORT=.venv/bin/bip322-report
PDF=$(.venv/bin/python -c 'import weasyprint' 2>/dev/null && echo --pdf || true)   # the PDF when WeasyPrint is installed
LEDGER="$WORK/ledger"
step() { printf '\n\033[1m=== %s ===\033[0m\n' "$*"; }
run() { printf '$ %s\n' "$*" >&2; "$@"; }
cleanup() { $CLI stop >/dev/null 2>&1 || true; sleep 1; rm -rf "$DATADIR"; }
trap cleanup EXIT

sign_bundle() {  # cosigners A and C sign every PSBT of a bundle (hardware signers in real life), then finalize
  local B=$1
  for P in "$B"/to_sign/*.psbt; do
    N=$(basename "$P" .psbt)
    $DEV signpsbt "$P" "$WORK/cosigner-A.json" -o "$B/signed/$N-A-part.psbt"
    $DEV signpsbt "$P" "$WORK/cosigner-C.json" -o "$B/signed/$N-C-part.psbt"
  done
  run $AUDIT --cli "$CLI" finalize "$B"
}

step "0. a private regtest node with a wallet"
"$CORE/bin/bitcoind" -regtest -datadir="$DATADIR" -rpcport=$PORT -rpcuser=demo -rpcpassword=demo -listen=0 -connect=0 -fallbackfee=0.0001 -daemonwait >/dev/null
$CLI createwallet miner >/dev/null; MINE=$($CLI -rpcwallet=miner getnewaddress); $CLI -rpcwallet=miner generatetoaddress 101 "$MINE" >/dev/null

step "1. the wallet: dummy cosigners, descriptor (regtest encoding), watch-only import into Core"
for L in A B C; do $DEV keygen --label $L --seed "demo cosigner $L" --network regtest -o "$WORK/cosigner-$L.json"; done
$DEV makewallet -t 2 --name demo-2of3 --network regtest "$WORK"/cosigner-{A,B,C}.json -o "$WORK/wallet.desc" 2>/dev/null
$CLI createwallet watch true true "" false true >/dev/null
DESC0=$(.venv/bin/bip322 -w "$WORK/wallet.desc" wallet | .venv/bin/python -c "import json,sys;print(json.load(sys.stdin)['descriptor'])")
.venv/bin/python - "$CLI" "$DESC0" <<'PY'
import json, subprocess, sys
cli, desc = sys.argv[1].split(), sys.argv[2]
body = desc.split("#")[0]
descs = [subprocess.run([*cli, "getdescriptorinfo", body.replace("<0;1>", b)], capture_output=True, text=True, check=True).stdout for b in ("0", "1")]
req = [{"desc": json.loads(d)["descriptor"], "timestamp": "now", "range": [0, 50], "active": False} for d in descs]
print(subprocess.run([*cli, "-rpcwallet=watch", "importdescriptors", json.dumps(req)], capture_output=True, text=True, check=True).stdout.strip())
PY

step "2. two deposits, confirmed"
A0=$(.venv/bin/bip322 -w "$WORK/wallet.desc" --network regtest deriveaddresses 0); A1=$(.venv/bin/bip322 -w "$WORK/wallet.desc" --network regtest deriveaddresses 2 --change)
$CLI -rpcwallet=miner sendtoaddress "$A0" 0.5 >/dev/null; $CLI -rpcwallet=miner sendtoaddress "$A1" 0.1 >/dev/null
$CLI -rpcwallet=miner generatetoaddress 2 "$MINE" >/dev/null

step "3. the owner proves everything once (the first bundle in the ledger)"
run $AUDIT --cli "$CLI" -w watch snapshot --depth 1 --text "Proof of control {date}" -o "$LEDGER/snapshot-first"
sign_bundle "$LEDGER/snapshot-first"

step "4. a payment is prepared; before broadcasting, the change address is proven (a bundle joins the ledger)"
CHANGE=$(.venv/bin/bip322 -w "$WORK/wallet.desc" --network regtest deriveaddresses 5 --change)
FUNDED=$($CLI -rpcwallet=watch walletcreatefundedpsbt '[]' "[{\"$MINE\":0.2}]" 0 "{\"subtractFeeFromOutputs\":[0],\"changeAddress\":\"$CHANGE\"}" | .venv/bin/python -c "import json,sys;print(json.load(sys.stdin)['psbt'])")
$CLI decodepsbt "$FUNDED" | .venv/bin/python -c "import json,sys;[print('output', o['value'], o['scriptPubKey']['address']) for o in json.load(sys.stdin)['tx']['vout']]"
run $AUDIT --cli "$CLI" -w watch prove "$CHANGE" --depth 1 --text "Proof of control {date}" --ledger "$LEDGER"
sign_bundle "$(ls -td "$LEDGER"/snapshot-*/ | head -1)"

step "5. the proof of the change address is valid: broadcast"
PRIV=$(.venv/bin/python - "$WORK" <<'PY'
import json, sys
from pathlib import Path
w = Path(sys.argv[1])
keys = [json.loads((w / f"cosigner-{l}.json").read_text()) for l in "ABC"]
def desc(holder):
    parts = [(k["xprv_expression"] if i == holder else k["xpub_expression"]) for i, k in enumerate(keys)]
    return "wsh(sortedmulti(2," + ",".join(parts) + "))"
print(json.dumps([desc(0), desc(1)]))
PY
)
SIGNED=$($CLI descriptorprocesspsbt "$FUNDED" "$PRIV" | .venv/bin/python -c "import json,sys;d=json.load(sys.stdin);assert d['complete'];print(d['hex'])")
$CLI sendrawtransaction "$SIGNED" >/dev/null; $CLI -rpcwallet=miner generatetoaddress 2 "$MINE" >/dev/null
echo "mid-year, nothing is left to prove: the change address already is"
$AUDIT --cli "$CLI" -w watch snapshot --depth 1 --skip-proven "$LEDGER" 2>&1 | tail -1 || true

step "6. year end: one full bundle over every output, so each closing coin has a proof dated at (or after) the closing block"
run $AUDIT --cli "$CLI" -w watch snapshot --depth 1 --text "Proof of control {date}" -o "$LEDGER/snapshot-year-end"
sign_bundle "$LEDGER/snapshot-year-end"

step "7. the year's balance report: opening and closing balances, movements, every closing coin backed by a verified proof"
run $REPORT --cli "$CLI" -w watch --year "$(date -u +%Y)" --ledger "$LEDGER" $PDF -o "$WORK/report"
ls "$WORK/report"

step "8. the same, from a cached history and for two heights"
run $REPORT --cli "$CLI" -w watch history -o "$WORK/history.json"
run $REPORT --cli "$CLI" --history "$WORK/history.json" --from-height 103 --to-height 105 --ledger "$LEDGER" --rate 950000 --currency SEK -o "$WORK/report-q"

step "9. unsolicited dust arrives: 600 satoshi from someone else, to an address of the wallet"
DUST=$(.venv/bin/bip322 -w "$WORK/wallet.desc" --network regtest deriveaddresses 7)
$CLI -rpcwallet=miner sendtoaddress "$DUST" 0.00000600 >/dev/null; $CLI -rpcwallet=miner generatetoaddress 1 "$MINE" >/dev/null
echo "as it stands the statement holds a UTXO without a proof: RESULT: ATTENTION, exit status 1"
STATUS=0; run $REPORT --cli "$CLI" -w watch --year "$(date -u +%Y)" --ledger "$LEDGER" -o "$WORK/report-unproven-dust" || STATUS=$?
[ "$STATUS" -eq 1 ] || { echo "expected exit status 1 (ATTENTION), got $STATUS" >&2; exit 1; }
echo "with --dust 1000 it is left out of every figure, counted in a note and listed in the JSON: RESULT: OK"
run $REPORT --cli "$CLI" -w watch --year "$(date -u +%Y)" --ledger "$LEDGER" --dust 1000 -o "$WORK/report-dust"

step "done - artifacts in $WORK (open $WORK/report/report.html)"
