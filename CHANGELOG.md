# Changelog

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
