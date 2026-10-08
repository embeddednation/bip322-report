# bip322-report

The statement of a period, in which every coin is backed by a verified BIP-322
proof of control. Built on [bip322-audit](https://github.com/embeddednation/bip322-audit)
(the proof bundles) and [bip322-core](https://github.com/embeddednation/bip322-core)
(the BIP-322 work), for the owner's side: it reads the node wallet's transaction
history and the ledger of proof bundles, and writes the statement of a period.

A statement answers, for a wallet and a period:

* what the wallet held at the start and at the end (as of two block heights),
* every movement in between, with the wallet's side of each transaction and the exact fee (unknown only when others funded the transaction too),
* that opening + net = closing,
* and for each closing coin, the proof of control that lists it, re-verified against the node when the report is made.

Two ways of working with it, the same tool for both:

* **Continuously, for yourself.** Before broadcasting a spend, prove its change address (`bip322 audit prove ADDRESS --ledger LEDGER`, sign, finalize): a valid proof means the quorum controls where the change goes. New deposit addresses the same way, or with `snapshot --skip-proven LEDGER` after the fact. A report can then be produced at any time, with every coin backed by your own signed message; a coin that arrived after its proof is marked as such (the proof shows control, not the holding).
* **On demand, for an auditor.** After the period's end, take one full bundle (no `--skip-proven`) over every output under a message that names the audit, and produce the report for the year. Every closing coin then has a proof dated after the closing block, and the chain lookup at that block shows the coin held. The auditor verifies the bundles and the report on their own node with `bip322 audit verify` and `bitcoin-cli`.

The whole flow, for the holder and for the auditor, is in the [handbook](bip322report/handbook.md); `bip322 report handbook` prints it.

## Install

One line, into a fresh venv; bip322-audit and bip322-core come along, and the
three commands land in the venv's `bin`:

```sh
python3 -m venv ~/.bip322 && ~/.bip322/bin/pip install "bip322-report[kernel,pdf]"
export PATH="$HOME/.bip322/bin:$PATH"
bip322 engines && bip322 audit help && bip322 report help
```

Python 3.11 or newer (on Ubuntu 22.04: `apt install python3.12 python3.12-venv`
from the deadsnakes PPA, then `python3.12 -m venv ~/.bip322`). Leave out
`[kernel]` on anything but CPython 3.12 / Linux x86_64 (btclib remains as the
verifier). For a reproducible, hash-pinned install, clone and
use the setup script:

```sh
git clone https://github.com/embeddednation/bip322-report.git && cd bip322-report
./setup.sh                    # venv, hash-pinned dependencies, bip322-core and bip322-audit at their pinned tags, tests
export PATH="$PWD/.venv/bin:$PATH"
```

`setup.sh` installs the two packages from git at the tags named in the script
(`CORE_REF`, `AUDIT_REF`); `./setup.sh --core ../bip322-core --audit ../bip322-audit`
uses local checkouts, which is the development setup. Nothing here needs the
wallet descriptor: the history comes from the node wallet, the proofs from the
ledger.

PDF output is optional. It needs WeasyPrint's system libraries, then the
renderer itself, hash-pinned like everything else:

```sh
sudo apt install libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b   # Debian/Ubuntu
./setup.sh --pdf                                                   # installs requirements-pdf.lock
```

Without it `report` still writes the HTML (print it to PDF from a browser)
plus the JSON and the CSV; `--pdf` then says what is missing.

## The flow

```sh
# a ledger: any directory where the bip322-audit bundles accumulate.
# before broadcasting a spend: prove its change address
bip322 audit -w treasury prove bc1q...change... --text "Proof of control {date}" --ledger ledger
#   sign to_sign/*.psbt on the cosigners' devices, put the results in signed/, then
bip322 audit finalize ledger/snapshot-<date>-<height>     # valid -> broadcast
# anything that arrived on addresses not yet proven (deposits):
bip322 audit -w treasury snapshot --text "Proof of control {date}" --skip-proven ledger

# the year's statement
bip322 report -w treasury --year 2026 --ledger ledger
#   -> treasury-2026/treasury-2026.json, .html, .csv (and .pdf with --pdf); -o DIR names both
```

`bip322 report` prints a summary on stderr and the directory on stdout. For the
year, made in January from the year-end bundle:

```
treasury: 2026  blocks 878000 -> 931234  ref 009ab805de61d672
opening 1.23400000 BTC  closing 1.03398110 BTC  net -0.20001890 BTC  (3 transactions, fees 0.00001890 BTC)
reconciliation: ok
proof coverage: 2/2 closing outputs, 1.03398110 BTC; stamped after the period's end: 2/2
  bundle 2026-year-end/proofs.json: stamp 931871, signatures 2/2, verified, used for 2
  other bundles in the ledger, not used: 2
on chain (holdings by UTXO at the block of its proof): 2/2 held when proven
RESULT: OK
```

`ref` identifies the facts stated (heights, coins, movements, the proofs
chosen): the same facts give the same reference whenever the report is made.
A period whose end is still in the future says `(to tip)`; when the period
ends at the tip the history was read at, a line
`node check (listunspent vs closing coins): ok` follows.

`RESULT: ATTENTION` means the reconciliation failed, a closing UTXO has no
verified proof, a lookup on the chain contradicts the statement, or the node
wallet disagrees with the closing UTXOs; the summary and the report say
which. An uncovered UTXO is fixed by a new bundle with `--skip-proven`.
`report` exits 0 when the result is OK, 1 when it is ATTENTION and 2 on an
error.

## Commands

```
bip322 report [--cli CMD] [-w NAME] history [-o history.json]
bip322 report [--cli CMD]           block WHEN
bip322 report [--cli CMD] [-w NAME] balance [--at WHEN | --height H] [--history FILE]
bip322 report [--cli CMD] [-w NAME] (--year Y | --from WHEN --to WHEN | --from-height H --to-height H)
                                     [--ledger DIR]... [--holder NAME] [--history FILE]
                                     [--rates CSV | --rate N] [--currency CODE] [--dust SATS [--dust-force]] [--explorer URL] [--pdf] [--theme journal|light|paper|dark] [--heading STYLE] [-o DIR]
bip322 report help [COMMAND]
```

The statement is the default command (`report` names it explicitly, as in `help report`).

* `history` reads every transaction of the node wallet once and writes it as
  JSON; `balance` and `report` accept it with `--history`, so reports can be
  regenerated without the wallet, or exactly reproduced from the same data.
  A history of another chain, or one whose tip block is not in the node's
  chain (a reorganisation, another network), is refused.
* `block` resolves a UTC date or instant to the last block before it, the
  rule every period uses. `--year 2026` opens on the last block before
  2026-01-01 and closes on the last block before 2027-01-01, or on the tip when
  that is still in the future (the tip the history was read at). The heights
  used are printed in the report, and `--from-height/--to-height` reproduce a
  report exactly. The period is given one way only. When the end is in the
  past and the node has no block after it yet (still syncing, or behind), the
  closing block is not settled and `report` refuses.
* `--ledger` takes a directory tree of bundles, one bundle directory or one
  `proofs.json`, and may repeat. A path that does not exist or holds no
  bundle is an error, so a mistyped path does not read as "no proofs".
* `--rates date,rate.csv` or `--rate N` with `--currency` adds a fiat value
  per movement (the rate in force is the latest dated row on or before the
  transaction's day). Nothing is fetched; the rates are yours. `--rates`
  and `--rate` exclude each other, `--currency` needs one of them, and a
  rate or a row that cannot be read is an error naming the file and line.
* `--explorer URL` sets the block explorer behind the links in the HTML and
  PDF. The default is https://mempool.space on mainnet and no links on any
  other chain; `''` for none.

## What is in the report

The report is laid out as a statement the holder prepares for examination,
following the conventions accountants expect: units declared once in the
header, negatives in parentheses, a single rule above and a double rule under
totals, right-aligned tabular figures with all eight decimals, a running
balance in the movements table, a reference number on every page, and a
section on the basis of preparation and its limitations (point in time,
control is not title, completeness rests on the holder's representation).
The wallet is named by the node wallet's name; `--holder NAME` puts the holder's name in the header. Sections 1 and 2 are kept whole when they fit on a page.

* Period: the instants asked for and the two blocks they resolved to.
* First, in the style of a bank statement, the movements: opening balance,
  each transaction with its running balance, closing balance, which is the
  holdings on the chain; a difference from the running balance is printed
  in red, agreement in one quiet sentence.
* Holdings at the end of the period: the wallet's UTXOs with the
  scriptPubKey each is locked to, then one page per UTXO in two steps, each
  a pasteable command with what it printed. *Proof of control of the scriptPubKey*: message, proof, and
  `bip322 verifymessage <scriptPubKey> ...` with its verdict. *On the
  chain*: `bip322 audit holdings TXID:VOUT --at <block of the proof>`, the
  block named in the proof's message, which reports the same scriptPubKey as
  the UTXO's lock, the amount, and that it was held at that block (confirmed
  by then and unspent now). Control and holding are thus shown at one block.
  The values a reader matches by eye are coloured alike wherever they appear
  on the page (scriptPubKey, block, proof, UTXO, amount; the theme's
  palette), and the commands and their output are set in monospace.
  `--theme` picks `journal` (the default): a warm canvas,
  greys, ruled tables, the values in the palette's base colours with red
  for the block, set in a serif for reading and a sans for labels and
  tables; or `light`, `dark` (Solarized) or `paper` (white). `--heading`
  picks how section headings are set: `number` (the number in red), `tab`
  (a red tab over a grey rule), `redrule`, or `underline`.
* Typefaces: Adobe's Source Serif 4 and Source Sans 3 and JetBrains Mono
  (SIL Open Font License) are bundled and embedded in the PDF, so the
  statement looks the same on every machine; `report` copies them to
  `<out>/fonts/` for the HTML. The commands' output is the finding;
  a badge follows only when something needs saying (INVALID, NO PROOF,
  CONTRADICTED, a proof that predates its UTXO). BIP-322 defines
  the proof for a scriptPubKey ("the key script to be proven"); an address
  is that script encoded, which is why the statement can verify on the
  bytes themselves. A coin that arrived after its proof (a change address
  proven before the spend) is marked so; the year-end bundle gives it a
  proof dated after it.
* `--dust SATS`: an output of at most SATS satoshi is dust when others
  paid it to the wallet in a transaction that spends nothing of the
  wallet's. Dust is left out of every figure, as if it had never been the
  wallet's: balances, movements, reconciliation, coverage and the node
  cross-check all ignore it, so the statement agrees with books that never
  recorded it. The wallet's own change and consolidations are never dust,
  whatever their size. Dust the wallet does spend is booked in the block
  that spends it, as a movement of its own (`dust swept`) just before the
  spending transaction, whose amounts and fee stay exact; an opening
  balance is therefore always the closing balance of the statement
  before, whatever happens to dust later. The dust held at
  the closing block is listed in the JSON and counted in a note at the
  end; spending it would cost more in fees than it holds, so a holder
  leaves it (and spending unsolicited dust links addresses). What is
  proven is control, not its absence. A negative threshold is refused,
  and one above 10000 satoshi needs `--dust-force`.
* Exceptions only: nothing in the statement says OK beyond the figures.
  Four checks decide the result: opening + net = closing; every closing
  UTXO has a verified proof; no lookup on the chain contradicts the
  statement (another scriptPubKey or amount); and, when the period ends
  at the tip the history was read at, the node wallet's `listunspent`
  agrees with the closing UTXOs. When one fails, an ATTENTION mark and a
  checklist with the failing item appear on the first page. Shown on the
  UTXO's page but not failed checks: a proof whose block is before the
  period's end (the page names the proof's block), a UTXO received after
  its proof, and a UTXO spent after the period (SPENT SINCE, with the
  spending transaction; `spent_by` in the JSON). A lookup or a node check
  that could not run is shown as not run, with the reason.
* The node cross-check counts only while the node's tip is the history's,
  before and after `listunspent`; otherwise it is not run. An output that
  an unconfirmed transaction spends, or a coinbase output not yet mature,
  is missing from `listunspent` but still in the chain (`gettxout`): it is
  noted, not a mismatch.
* At the end, on a page of its own, section 4: how a BIP-322 proof of
  control works, for any kind of scriptPubKey, with a figure of the two
  virtual transactions field by field: the message hashed into to_spend's
  input, to_spend's txid consumed by to_sign's input, the proof as its
  witness, the UTXO on the chain under the same scriptPubKey, and the
  verifier's checks; the prose to match, with this wallet's script as the
  example, and what a proof shows and does not; then
  brief notes: units, wallet and blocks, preparation, the files alongside
  and the tools that check them.
* Every movement: date, txid, kind, amount (received, or sent before the
  fee), fee and the running balance; the total row's amount is the sum of
  the rows. Kinds: `receive` (nothing of the wallet's spent), `send`,
  `internal` (everything came back), and `mixed` for a transaction funded
  by the wallet and by others, whose fee is unknown and shown as such and
  whose amount is the wallet's net. Within a block, movements follow their
  position in the block. The CSV and the JSON add the net and the outputs
  on each side: for a payment the address paid, or no address for an
  output that has none (`no-address` in the CSV, null in the JSON).
  Unconfirmed transactions are listed apart and count in no balance;
  abandoned and replaced ones are not listed.

The report names no descriptor, no xpub and no derivation path; the bundles
it cites carry none either. Bundle names are relative to the ledger, never
absolute paths: `<bundle dir>/proofs.json` also when `--ledger` is itself a
bundle directory or a `proofs.json`, with the ledger directory's name in
front when two ledgers hold bundles of the same name (an error if that
does not tell them apart). Only the bundles cited on a closing UTXO's page
are listed; whatever else the ledger holds (another wallet's bundles, older
ones) appears as a bare count.

**What to hand over:** the report directory. Besides the HTML, JSON and
CSV (and the PDF), all named after the directory, it holds `ledger/` with
a copy of each `proofs.json` the statement cites, and no other, under the
name the report uses, so a reader can run `bip322 audit verify` on each.
Never hand over the ledger itself: the bundles' PSBT files carry the
wallet's xpubs. `--no-proofs` skips the copy.

## Layout

```
bip322report/history.py    the node wallet's transactions; coins and balance at any height
bip322report/period.py     a period resolved to block heights
bip322report/coverage.py   the ledger's bundles, re-verified; which proof backs which coin
bip322report/fiat.py       optional valuation from user-supplied rates
bip322report/report.py     the report as data, and the text summary
bip322report/render.py     HTML (Jinja2 template), CSV, PDF (WeasyPrint), themes, bundled fonts
bip322report/cli.py        the bip322-report command
tests/                      a fake node covering both packages' RPCs; an end-to-end test on regtest Core
examples/report_walkthrough.sh   the flow on a throwaway regtest node, into a fresh directory (or a new or empty DIR)
```

The regtest test and the walkthrough need a Bitcoin Core binary: set
`BITCOIN_CORE_DIR`, or run `refcheck/fetch.sh` in the bip322-core checkout the
venv was set up from. CI checks out both dependencies at their pinned tags.
