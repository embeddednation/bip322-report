# bip322-report: design notes

What the report claims, how each claim is established, and what the tool
deliberately does not do. The proofs themselves are bip322-audit's business
(see its `docs/DESIGN.md`); this package only reads and re-verifies them.

## Claims and their evidence

* **Balances at a block.** The wallet's unspent outputs after every block up
  to and including the height, computed from the wallet's own transaction
  history: every output paying a wallet address, minus every such output spent
  by a transaction confirmed at or before that height. Unconfirmed transactions
  count for nothing; conflicted, replaced and abandoned ones are dropped.
* **Movements.** The transactions confirmed strictly after the opening block
  and up to the closing block, each reduced to the wallet's side: outputs it
  spent, outputs it received, outputs that left. Net is received minus spent.
  The fee is exact when every input was the wallet's (inputs minus all
  outputs); it does not rely on the wallet's own fee bookkeeping. A
  transaction with inputs from others as well is `mixed`: its fee cannot be
  known from the wallet's side and is stated as unknown, its amount is the
  wallet's net. An output without an address (a data output with value, a
  bare script) is an output that left. Within a block, movements follow
  their position in the block, so a spend never precedes the receipt it
  spends.
* **Reconciliation.** Opening + net = closing. Both balances and the net are
  computed from the same history, so this holds by construction: it shows
  that the statement is consistent with itself (every row adds up to the
  closing balance), not that the history is complete. A transaction
  missing from the history changes the figures together, and the equation
  still holds.
* **Node cross-check.** The check that compares the history with something
  else: when the period ends at the tip the history was read at, the closing
  coins must equal the node wallet's `listunspent`. It counts only while the
  node's tip is the history's, before and after the call; otherwise it is
  reported as not run, with the reason, which is not a failure. For an
  output the statement has and `listunspent` lacks, `gettxout TXID VOUT
  false` asks the chain alone: an output still there is spent by an
  unconfirmed transaction (or is a coinbase output not yet mature) and is
  noted; only an output the chain no longer has is a mismatch. For a period
  that ends before the tip there is no such check, and completeness rests
  on the node wallet and the holder's representation.
* **Dust** (`--dust SATS`, off by default). An output of at most the
  threshold that others paid to the wallet, in a transaction spending
  nothing of the wallet's, is left out of every figure and listed in the
  JSON. The wallet's own change and consolidations are never dust. Whether
  an output is dust depends on no height, so a period's figures do not
  change with later blocks and each opening balance is the closing balance
  before it. Dust the wallet does spend is booked in the block that spends
  it, as a movement of kind `swept` standing right before the spending
  transaction, which is itself unchanged: every amount and fee stays exact.
* **Coverage.** A closing coin is covered when a verified bundle carries a
  proof for its address: every signature valid, the stamp block in the main
  chain, no listed output contradicted by the node. A BIP-322 proof is about
  the script behind an address, so it covers every output paid there, before
  or after the proof was made; the report marks proofs that predate their
  output (a change address proven before the spend was broadcast). Among
  several proofs for an address the verified ones come first, then those whose
  snapshot listed the very output, then the latest stamp; the others are
  counted.
* **On the chain.** Each closing UTXO is looked up at the block its proof
  names (`bip322 audit holdings TXID:VOUT --at HEIGHT`). Another
  scriptPubKey or amount than the statement's is a contradiction. A UTXO
  received after its proof, or spent since (after the period; the statement
  names the spending transaction when the history knows it), is shown on
  its page and is not a failure. A lookup that could not run is shown as not run and
  excuses none that did.
* **The verdict.** `RESULT: OK` (exit status 0) when the reconciliation holds,
  every closing coin is covered, no lookup contradicts the statement, and
  the node cross-check either agrees or was not run; otherwise
  `RESULT: ATTENTION` (exit status 1). A proof whose block is before the
  period's end does not change the verdict: the summary counts the proofs
  stamped after it, and each page names its proof's block.
* **The reference.** A hash of the facts stated: label, chain, the two
  blocks, the opening and closing coins, the movements (with their valuation
  when rates are given), and for each closing UTXO the bundle, address and
  signature of its proof. Nothing that changes
  between two runs over the same facts (the tip, command output, the time
  of preparation) enters it.

## Periods

Calendar bounds are instants in UTC. Each maps to the last block whose header
time is before the instant, found by binary search over headers. Header times
are not strictly monotonic, so the boundary is conventional rather than
unique; the report prints the heights it used, and `--from-height/--to-height`
reproduce any report exactly. An end instant in the future means the period
runs to the tip, which is the tip the history was read at. An end instant in
the past that the node has no block after (a node still syncing, or behind)
is an error: the closing block is not settled. A start before the chain's
first block opens on block 0. A cached history is used only against the
chain it was read from: same network, its tip block still in the node's
chain.

## Why a ledger and not a database

A database of balance snapshots and signatures, from which a report picks
the latest snapshot before a date, is only as fresh as the last snapshot
taken. Here the history is recomputed from the
node every time, or reproduced from a cached `history.json`, and the record
of proofs is the ledger: bip322-audit's bundles on disk, each self-contained
and independently verifiable. There is nothing else to keep in sync.

## What is left out on purpose

* No descriptor, xpub or derivation path anywhere in the output: the report
  names addresses and outputs, like the bundles do.
* Nothing about the rest of the ledger: only the bundles cited on a closing
  UTXO's page are named and copied next to the statement; the others are a
  bare count.
* No links to a third party except on mainnet, where transactions and
  scripts link to a block explorer unless `--explorer ''` says otherwise.
* No fetched prices: a fiat valuation uses only rates the user supplies.
* No email, no database, no browser engine: HTML from a template, CSV, and
  optionally a PDF through WeasyPrint.
* No signing: proofs are produced with bip322-audit and the devices.

## Trust

Owner-side tooling: it needs the node wallet (watch-only is enough) and reads
the ledger. What it hands to a reader, the report and the bundles, is
checkable on any node with the chain and no wallet: `bip322-audit verify` for
the bundles, `gettxout` and the transactions by txid for the balances and
movements. The statement's notes name the files that come with it and the
command that checks a proofs file; the commands on the UTXO pages paste as
they stand.
