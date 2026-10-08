# Changelog

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
