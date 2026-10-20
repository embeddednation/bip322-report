# Changelog

## 0.29.0 (2026-10-20)

The date leads a movement's row, and the transaction id follows it whole,
in its own column, wrapping once between its eight-character groups into
the two lines the date and time take.

## 0.28.0 (2026-10-20)

A movement names its transaction by the first sixteen characters of its id,
linked to the explorer with the whole id as the link's title; the JSON and
the CSV carry it whole. The Kind column is gone: received and sent show in
the amount's sign, and an internal move, a joint transaction and dust swept
are named as such in the counterparty column, in the statement and in the
reader's document alike. The holdings lose their Received column; the
UTXO's page keeps the date.

## 0.27.1 (2026-10-20)

The movements and the holdings are set in 8pt, their ids in 7pt as before,
and the counterparty column is wider: a wallet name of forty characters
takes two lines of the row instead of three cramped ones.

## 0.27.0 (2026-10-20)

One category per column. A movement's transaction id has a column of its
own, in a smaller type, wrapping between its eight-character groups and
copying whole; the currency's value sits under the BTC amount instead of in
a column; the date keeps its two lines. The holdings start on a page of
their own, with the UTXO's id in its own column and the last column named
Section. Requires bip322-audit 0.21, whose `checksigners` takes a snapshot
as `finalize` does; the handbook says so.

## 0.26.0 (2026-10-20)

The UTXO is the unit of sections 2 and 3 again, and a movement carries its
transaction's id whole. Each movement is a row of two lines: the date over
the time, the figures, and under them the full txid in small type, linked
to the explorer; the counterparty has the width the txid column took. The
holdings are one row per UTXO (a holder's grouped by wallet): when it
arrived, the amount, the proof's verdict and block, its page, and under
them its id, `txid:vout`, whole, so the transaction that created it is
found among the movements by eye. Section 3 is one page per UTXO: the
address, its scriptPubKey, the proof with the verify command, then the
lookup of that UTXO; two UTXOs at one address each get a page, under the
same proof. The checklist and the closing holdings count UTXOs. In the
JSON, `coverage` loses its counts by address and `closing.addresses[]` its
`received_first`, `received_last` and `proof.before_coins`.

## 0.25.0 (2026-10-19)

The closing holdings close the first page's table, each wallet's share
listed above them under "At the end of the period". A movement is named by
the first eight characters of its transaction id, linked to the explorer,
instead of a number; `n` is gone from the JSON.

## 0.24.0 (2026-10-19)

One table on the first page: the opening holdings with each wallet's share
under them, received, sent, fees, and the closing holdings with each
wallet's share under them; the table of wallets with its sums is gone (the
figures stay in the JSON). The number of a movement links to its transaction
on the explorer and names it in the JSON; the list of transactions is gone,
and the explainer is section 4 again.

## 0.23.0 (2026-10-19)

The holder's movements one block per wallet, as in the reader's history,
and no Transfers column anywhere. A move between two of the holder's
wallets is sent by the one and received by the other for the same amount,
the fee with the sender, and carries one number in both blocks; section 4
lists the transactions by that number. The wallets table on the first page
has opening, received, sent, fees and closing, a note under it saying how
much moved between the wallets. The reader's history shows the amount
before the fee and the fee apart, so the two sides of a move match there
too. In the JSON: `wallets[].transactions[]` carry `n`, `amount_sat` and
`fee_sat`; `wallets[]` carry `moved_in_sat`, `moved_out_sat` and
`amount_sat` instead of `transfers_sat`; every movement carries `n`.

## 0.22.1 (2026-10-19)

`bip322 report company` is `bip322 report holder company`: a bare word that
names a holder of the tree means its ledger.

## 0.22.0 (2026-10-19)

The statement for the auditor, laid out like the reader's history. A summary
first: the opening holdings, received, sent, fees, the closing holdings, in
BTC and in the currency when rates are given (the holdings at the rate of
their day), the statement in the tree that closed at this one's opening block
cited by label and reference, and, for a holder, a table of its wallets
(opening, received, sent, transfers between them, fees, closing); then one
sentence saying what is proven, or the ATTENTION checklist. The movements are
numbered and carry the kind in words, the wallet (a holder's ledger) and the
counterparty instead of the transaction identifier; section 4 lists the
transactions by number. The holdings are one row per address, grouped by
wallet, with the proof's verdict, and section 3 is one page per address
(the proof once, a lookup per UTXO) instead of one per UTXO. The explainer is
section 5. In the JSON: `wallets[].received_sat`, `sent_sat`,
`transfers_sat`, `fees_sat`, `mixed_sat` and `wallets_totals`, `previous`,
`fiat.opening` and `fiat.closing`, `totals.fiat`, and a holder's movements
carry `wallet` with a plain `counterpart` instead of `from -> to`.

## 0.21.0 (2026-10-16)

A document for the reader who is not the auditor: beside every statement,
`<name>-history.pdf` (or `.html`) carries the movements alone, each wallet
a section, oldest first, with the date, the kind in plain words, the
counterparty, the amount and the running balance; no transaction
identifiers, no addresses, no proofs. The statement and the history share
the reference and the theme.

## 0.20.0 (2026-10-16)

Old wallets need not stay loaded. Every history read from the node is kept
in the wallet's directory as `history.json` (by `report`, `holder` and
`history`). When the node says a wallet is not loaded, its statement and the
holder's ledger read that file instead, provided the wallet held nothing at
the file's tip; one that still held coins has to be loaded. The ledger's
merged history no longer stops at an emptied wallet's tip. The summary, the
JSON (`wallets[].history`) and the statement say which wallets came from a
file.

## 0.19.0 (2026-10-16)

In the holder's CSV a move between two of the holder's wallets is a `send`
in one block and a `receive` in the other, as in each wallet's own
statement; the wallet column already names the other wallet. `transfer`
remains the kind of the merged movement in the statement's table.

## 0.18.0 (2026-10-16)

The wallets of the tree name their own addresses: for every address on the
other side of a movement that `[addresses]` does not name, each other wallet
of the tree is asked, on its node, whether the address is its, and the
wallet column says so. A move between two wallets of the holder's, or to
another holder's wallet, needs no entry. A wallet that cannot be asked is
skipped with a line on stderr. The holder's wallets come oldest first, by
their first transaction, so the wallet holding the coins now is last.

## 0.17.0 (2026-10-16)

The holder's CSV is one block per wallet: the wallet's name on a line of its
own, the six-column header, then the movements as that wallet sees them, the
amount its net (fee inside), the balance its own running sum, the wallet
column the other side plain: another wallet of the holder's, or what
`[addresses]` names. A move between two of the holder's wallets is a
`transfer` in both blocks. The JSON carries the same rows under each entry
of `wallets` (`transactions`). The statement's merged table is as before.

## 0.16.0 (2026-10-16)

Needs bip322-audit 0.20: settings per holder. A key in a `[holders.KEY]`
table of `bip322.toml` (`dust`, `theme`, `heading`, `explorer`, `cli`) applies
to the holder's wallets, between the wallet's own table and the top level;
`bip322 report holder KEY` reads the holder's table too.

## 0.15.0 (2026-10-15)

Needs bip322-audit 0.19: holders above wallets. A holder is a directory of
the tree and its wallets are the directories under it, so the grouping is
the path; `[holders.KEY]` in `bip322.toml` gives only the name. The wallet's
statement goes to `<holder>/<wallet>/statements/<label>/`, the holder's ledger
to `<holder>/statements/<label>/`. `bip322 report holder [KEY]`: KEY may be
left off inside the holder's directory, with `holder = KEY` in `bip322.toml`,
or when the tree has one holder. `-w KEY/NAME` names the holder a new wallet
goes under. `report --holder NAME` is gone: the header is the holder's name
from the tree and its table.

## 0.14.0 (2026-10-15)

Needs bip322-audit 0.18: a holder is a `[holders.KEY]` table in
`bip322.toml`, `name` and `wallets`, the one place the grouping is written.
`bip322 report holder [KEY]` (KEY may be left off when there is one holder)
replaces `--holder NAME`, and the ledger goes to `holders/KEY/<label>/` as
`KEY-<label>`. A wallet's own statement takes the holder's name from the
table that lists it, else from the `holder` key as before.

## 0.13.0 (2026-10-15)

Needs bip322-audit 0.17.

- `bip322 report holder`: one ledger for a holder with several wallets. The
  holder is `holder` in `bip322.toml` (or `--holder NAME`) and its wallets
  are the wallet directories whose holder it is, so nothing is listed twice.
  Their histories merge by transaction: a move between two of the holder's
  wallets is one movement of kind `transfer`, its fee exact, every coin
  knows its wallet, and the balance is the holder's total (each wallet's
  under `wallets` in the JSON). Without a period the ledger runs from the
  first transaction to the tip, labelled by the tip's date. It goes to
  `holders/<holder>/<label>/`, named `<holder>-<label>`; the CSV's wallet
  column shows both sides (`Exchange -> treasury`, `treasury -> cold`), and
  the statement adds a from/to column and the wallet of each coin held.
- `history.chain_order` is public; a `Coin` carries `wallet` in a merged
  history; the JSON always has a `wallets` key (null for one wallet).

## 0.12.0 (2026-10-15)

Needs bip322-audit 0.16.

- The CSV is the movements for a spreadsheet, six columns: `time`, `txid`,
  `category`, `amount` (the wallet's net, fee inside), `balance` (the
  running sum) and `wallet` (the other side), then `rate` and
  `amount_fiat` when rates are given. The fee, heights and outputs stay in
  the JSON.
- `[addresses]` in `bip322.toml` names the other side: a send by the
  addresses it paid, a receive by the addresses it came from, which the
  history now records (`payers`, from the block's undo data; Bitcoin Core
  25 or later). The JSON rows carry it as `counterpart`.

## 0.11.1 (2026-10-15)

Needs bip322-core 0.12 and bip322-audit 0.15.1: `bip322 checksigners
signed/*.psbt` checks every address of a snapshot at once, as the handbook
says.

## 0.11.0 (2026-10-13)

Needs bip322-audit 0.15: the data directory is the one `bip322 init` made at
or above the current directory, marked by its `bip322.toml` (was `config.toml`
in `~/bip322`); inside a wallet's directory that wallet is the one meant.

## 0.10.2 (2026-10-13)

- Needs bip322-audit 0.14, whose `init` writes `config.toml` with every key
  shown; the handbook says so.

## 0.10.1 (2026-10-12)

- `--dir DIR` puts the tree elsewhere than `~/bip322` (as `BIP322_DIR` does).
- `-w`, `--dir` and `--cli` go before or after any command; `block -w NAME`
  was refused. Needs bip322-audit 0.13.1.

## 0.10.0 (2026-10-12)

The statement goes to `~/bip322/<wallet>/statements/<label>/` and its proofs
come from `~/bip322/<wallet>/snapshots/`, the tree bip322-audit 0.13 writes
(`BIP322_DIR` moves it); `-w` names the wallet, else `~/bip322/config.toml`,
else the node's one loaded wallet.

- The directory is named by the period alone; the files carry the wallet:
  `<wallet>-<label>.pdf`, `.json`, `.csv`. `-o DIR` still writes elsewhere.
- One document: the PDF when WeasyPrint is installed, otherwise one HTML
  file with the fonts embedded; `--html` asks for the HTML. `--pdf` and the
  `fonts/` copy are gone.
- The proofs files the statement cites are copied flat beside it under
  their own names, `<wallet>-<date>-<height>-proofs.json`, and cited by
  those names; `ledger/` is gone. `--ledger` is `--snapshots`, an override.
- `config.toml` supplies `holder`, `dust`, `theme`, `heading`, `explorer`,
  `cli`, `wallet`, and `node` per wallet.
- In the JSON: `proofs_files`, `proofs_files_not_used`, `pending_snapshots`,
  and `file` in place of `bundle` on each proof; the summary says
  "proofs FILE". The statement's footer names `bip322 audit verify`.
- Requires bip322-audit 0.13 and bip322-core 0.11.

## 0.9.1 (2026-10-06)

First public release.

- The statement of a period (the command's default action): holdings and movements as JSON,
  HTML, CSV and PDF: the movements with a running balance, the closing
  UTXOs, one page per UTXO with the proof of control of its scriptPubKey
  and the chain's record, each as a command with its output, and an
  account of how a proof works.
- Proofs come from a ledger of bip322-audit bundles; the statement copies
  the `proofs.json` files it relies on next to itself.
- Checks are exceptions only: an ATTENTION mark and a checklist when one
  fails; exit status 0, 1 or 2.
- `--dust SATS` leaves unsolicited dust out of every figure.
- `history`, `block`, `balance`, `handbook`.
- Themes `journal` (default), `light`, `dark`, `paper`; bundled typefaces.
- Requires bip322-audit 0.12 and bip322-core 0.11.
