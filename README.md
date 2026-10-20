# bip322-report

The statement of a period, in which every coin is backed by a verified BIP-322
proof of control. Built on [bip322-audit](https://github.com/embeddednation/bip322-audit)
(the snapshots and their proofs files) and [bip322-core](https://github.com/embeddednation/bip322-core)
(the BIP-322 work), for the owner's side: it reads the holder's node wallets'
transaction histories and the wallets' snapshots, and writes the statement of a period.

A statement answers, for a holder and a period:

* what the wallet held at the start and at the end (as of two block heights),
* every movement in between, with the wallet's side of each transaction and the exact fee (unknown only when others funded the transaction too),
* that opening + net = closing,
* and for each closing coin, the proof of control that lists it, re-verified against the node when the report is made.

Two ways of working with it, the same tool for both:

* **Continuously, for yourself.** Before broadcasting a spend, prove its change address (`bip322 audit prove ADDRESS`, sign, finalize): a valid proof means the quorum controls where the change goes. New deposit addresses the same way, or with `snapshot --skip-proven` after the fact. A report can then be produced at any time, with every coin backed by your own signed message; a coin that arrived after its proof is marked as such (the proof shows control, not the holding).
* **On demand, for an auditor.** After the period's end, take one full snapshot (no `--skip-proven`) over every output under a message that names the audit, and produce the report for the year. Every closing coin then has a proof dated after the closing block, and the chain lookup at that block shows the coin held. The auditor verifies the proofs files and the report on their own node with `bip322 audit verify` and `bitcoin-cli`.

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
uses local checkouts, which is the development setup. Nothing here needs a
wallet descriptor: the histories come from the node wallets, the proofs from the
wallets' snapshots.

The PDF needs WeasyPrint's system libraries, then the renderer itself,
hash-pinned like everything else:

```sh
sudo apt install libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b   # Debian/Ubuntu
./setup.sh --pdf                                                   # installs requirements-pdf.lock
```

Without it the statement is one HTML file with the fonts inside it (print it
to PDF from a browser), and `report` says so; `--html` asks for that.

## The flow

```sh
# everything under company/ in the directory bip322 init made: the snapshots bip322-audit writes in company/treasury/,
# the statements this writes in company/statements/.
# before broadcasting a spend: prove its change address
bip322 audit prove bc1q...change...   # -> company/treasury/<date>-<height>/
#   sign to_sign/*.psbt on the cosigners' devices, put the results in signed/, then
bip322 audit finalize                 # the newest snapshot; valid -> broadcast
# anything that arrived on addresses not yet proven (deposits):
bip322 audit snapshot --skip-proven

# the year's statement of the holder, over every wallet under company/
bip322 report --year 2026             # or: bip322 report company --year 2026
#   -> company/statements/2026/: company-2026.pdf (or .html), .json, .csv, the proofs files it cites,
#      and company-2026-history.pdf: the movements for a reader, no proofs, no txids

# the same from the first reception to now, for a source-of-funds question
bip322 report company
#   -> company/statements/2026-10-15/, labelled by the tip's date
```

The holder is the word after the options, else the holder whose directory the
command runs in, else `holder` in `bip322.toml`, else the tree's one; `-o DIR`
writes the statement elsewhere, `--dir DIR` names the data directory from
elsewhere; `-w NAME` names a wallet for `history` and `balance`. The tree and the configuration file are described in bip322-audit's README and the
handbook; the keys this package reads are `holder`, `wallet`, `cli`,
`dust`, `theme`, `heading`, `explorer`, `node` per wallet, `name` per holder, and the
`[addresses]` table: the wallet each address of someone else belongs to
(`"bc1q..." = "Exchange"`), which names the other side of a movement; the
wallets of the tree name their own addresses, so a move between two of them
needs no entry. The statement's own settings, `dust`, `theme`, `heading` and
`explorer`, come from `[holders.KEY]` and the top level, not from a wallet's
table, which carries what is read per wallet (`node`, `cli`); the statement
says so when it finds one there.
A holder is a directory of the tree with its wallets' directories under it;
`[holders.KEY]` gives its `name` for the statement's header, and any other
key there (`dust`, `theme`, …) is a default for the holder's wallets, between
the wallet's own table and the top level. The statement merges the wallets'
histories into one, a transfer between two of them one movement of kind
`transfer`, the balance the holder's total, each wallet's share under the
opening and the closing holdings on the first page and its movements a block
of their own; its CSV is one block per wallet, under the wallet's name, as
that wallet sees the movements. A holder of one wallet gets the same document
without the split, and the CSV that wallet's rows. Every history read is kept
in the wallet's directory as `history.json`, and a wallet the node has not
loaded any more is read from that file, provided it held nothing at the
file's tip.

`bip322 report` prints a summary on stderr and the directory on stdout. For the
year, made in January from the year-end snapshot:

```
treasury: 2026  blocks 878000 -> 931234  ref 009ab805de61d672
opening 1.23400000 BTC  closing 1.03398110 BTC  net -0.20001890 BTC  (3 transactions, fees 0.00001890 BTC)
reconciliation: ok
proof coverage: 2/2 closing outputs, 1.03398110 BTC; stamped after the period's end: 2/2
  proofs treasury-2027-01-08-931871-proofs.json: stamp 931871, signatures 2/2, verified, used for 2
  other proofs files, not used: 2
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
which. An uncovered UTXO is fixed by a new snapshot with `--skip-proven`.
`report` exits 0 when the result is OK, 1 when it is ATTENTION and 2 on an
error.

## Commands

```
bip322 report [--cli CMD] [-w NAME] history [-o history.json]
bip322 report [--cli CMD]           block WHEN
bip322 report [--cli CMD] [-w NAME] balance [--at WHEN | --height H] [--history FILE]
bip322 report [--cli CMD]           [HOLDER] [--year Y | --from WHEN --to WHEN | --from-height H --to-height H]
                                     [--snapshots DIR]...
                                     [--rates CSV | --rate N] [--currency CODE] [--dust SATS [--dust-force]] [--explorer URL] [--html] [--theme journal|light|paper|dark] [--heading STYLE] [-o DIR]
bip322 report help [COMMAND]
```

The statement is the default command (`report` names it explicitly, as in `help report`).

* `history` reads every transaction of a node wallet once and writes it as
  JSON, and keeps it in the wallet's directory when the wallet is one of the
  tree; `balance` accepts it with `--history`, and the statement reads a
  wallet the node has not loaded from the kept file.
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
* The proofs come from the wallets' directories under the holder's. `--snapshots DIR` takes
  them from elsewhere instead: a directory of snapshots, one snapshot or one
  proofs file, and may repeat. A path that does not exist or holds no
  proofs file is an error, so a mistyped path does not read as "no proofs".
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
The holder is named as `[holders.KEY]` says, else by its directory. Sections 1 and 2 are kept whole when they fit on a page.
A holder of several wallets gets each wallet's share on the first page and a
block of movements per wallet; a holder of one wallet the same document
without the split.

* Period: the instants asked for and the two blocks they resolved to.
* First, the summary: the opening holdings, what was received from others,
  sent to others and paid in fees, and the closing holdings, in BTC and, when
  rates are given, in the currency, the two holdings at the rate of their
  day. The opening line cites the statement in the tree that closed at this
  one's opening block, by label and reference, in red when its closing
  holdings are not these opening ones. For a holder, each wallet's share is
  listed under the opening holdings and above the closing ones, which close
  the table. Then one sentence
  saying what is proven, or, when a check failed, an ATTENTION checklist.
* The movements, in the style of a bank statement, one block per wallet for
  a holder: each transaction with its date, its id whole and linked to the
  explorer, the counterparty (an internal move, a joint transaction or dust
  swept named as such instead), the amount before the fee, received or
  (sent), with the currency's value under it, the fee and the wallet's
  running balance; opening and closing rows.
  A move between two of the holder's
  wallets appears in both blocks under the same transaction, sent by the one
  and received by the other for the same amount, the fee with the sender.
* Holdings at the end of the period, on a page of its own: one row per
  UTXO (a holder's grouped by wallet) with its id, `txid:vout`, whole, the
  amount, the proof's verdict and block, and the section
  that shows it; the transaction that created it is among the movements.
  Then one page per UTXO in two steps, each
  a pasteable command with what it printed. *Proof of control of the scriptPubKey*: the address, its
  scriptPubKey, message, proof, and
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
  (SIL Open Font License) come with the package and are embedded in the PDF,
  and in the HTML when that is the statement, so it looks the same on every
  machine and is one file. The commands' output is the finding;
  a badge follows only when something needs saying (INVALID, NO PROOF,
  CONTRADICTED, a proof that predates its UTXO). BIP-322 defines
  the proof for a scriptPubKey ("the key script to be proven"); an address
  is that script encoded, which is why the statement can verify on the
  bytes themselves. A coin that arrived after its proof (a change address
  proven before the spend) is marked so; the year-end snapshot gives it a
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
* Every movement: date, kind, counterparty, amount (received, or sent before the
  fee), fee and the running balance; the total row's amount is the sum of
  the rows. Kinds: `receive` (nothing of the wallet's spent), `send`,
  `internal` (everything came back), and `mixed` for a transaction funded
  by the wallet and by others, whose fee is unknown and shown as such and
  whose amount is the wallet's net. Within a block, movements follow their
  position in the block. The JSON adds the net, the outputs on each side
  (for a payment the address paid, or null for an output that has none),
  the addresses a receive came from when the node can tell, and the other
  side's wallet as `[addresses]` names it. The CSV is the movements for a
  spreadsheet: `time, txid, category, amount, balance, wallet`, the amount
  being the wallet's net with the fee inside, the balance its running sum,
  and the wallet the other side's name (a wallet of the tree, or what
  `[addresses]` says), empty when nothing names it, to fill in by hand;
  `rate` and `amount_fiat` follow when rates are given.
  Unconfirmed transactions are listed apart and count in no balance;
  abandoned and replaced ones are not listed.

The report names no descriptor, no xpub and no derivation path; the proofs
files it cites carry none either. A proofs file is cited by its own name,
`<wallet>-<date>-<height>-proofs.json`, never by a path; a file still named
`proofs.json` (earlier versions) is named after its snapshot directory, and
two files that would share a name are an error. Only the proofs files cited
on a closing UTXO's page are listed; whatever else the wallets' snapshots
hold (older ones, another wallet's given with `--snapshots`) appears as a
bare count.

**What to hand over:** the statement directory,
`<holder>/statements/<label>/` in the data directory. Five kinds of file and nothing
else: `<holder>-<label>.pdf` (or `.html`), `.json`, `.csv`, the reader's
`<holder>-<label>-history.pdf`, and a copy of each proofs file the statement
cites, and no other, under its own name, so a reader can run `bip322 audit
verify` on each. Never hand over the
snapshots: their PSBT files carry the wallet's xpubs. `--no-proofs` skips
the copy.

## Layout

```
bip322report/history.py    the node wallet's transactions; coins and balance at any height
bip322report/period.py     a period resolved to block heights
bip322report/coverage.py   the wallet's proofs files, re-verified; which proof backs which coin
bip322report/fiat.py       optional valuation from user-supplied rates
bip322report/report.py     the report as data, and the text summary
bip322report/render.py     HTML (Jinja2 template), CSV, PDF (WeasyPrint), themes, the fonts
bip322report/cli.py        the bip322-report command
tests/                      a fake node covering both packages' RPCs; an end-to-end test on regtest Core
examples/report_walkthrough.sh   the flow on a throwaway regtest node, into a fresh directory (or a new or empty DIR)
```

The regtest test and the walkthrough need a Bitcoin Core binary: set
`BITCOIN_CORE_DIR`, or run `refcheck/fetch.sh` in the bip322-core checkout the
venv was set up from. CI checks out both dependencies at their pinned tags.
