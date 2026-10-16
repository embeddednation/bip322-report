# Changelog

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
