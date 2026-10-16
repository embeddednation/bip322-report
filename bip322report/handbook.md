# The yearly proof of control, end to end

What the holder does, what the auditor does, and what the pieces are. The
command references are the three READMEs ([bip322-core](https://github.com/embeddednation/bip322-core),
[bip322-audit](https://github.com/embeddednation/bip322-audit),
[bip322-report](https://github.com/embeddednation/bip322-report)); this is the flow. `bip322 report handbook` prints this file. The holder's
side of it, from the first snapshot to the statements (one of them with
`--dust`), is exercised by `examples/report_walkthrough.sh` on a throwaway
regtest node; it writes into a fresh directory, or into a new or empty one
given as its argument.

## The pieces

* **The wallet.** A descriptor wallet: a P2WSH multisig quorum whose
  cosigners are hardware signers, or a single-key P2WPKH wallet; the tools
  do not care which. Bitcoin Core
  holds it watch-only (`importdescriptors`), so the node knows the wallet's
  addresses, coins and transactions and can build PSBTs. No private key
  ever touches a machine running these tools.
* **A proof** (BIP-322, simple format, prefix `smp`) is a message signed by
  the keys that control a scriptPubKey. Its message ends with a block line,
  `block: HEIGHT HASH TIME`, which dates it: a proof cannot be older than
  the block it names. Control of a scriptPubKey covers every UTXO locked to
  it, before or after the proof; a UTXO that arrived after the proof is
  covered only by a later proof.
* **A snapshot** is one round of proofs: `bip322 audit snapshot` (every
  funded address) or `prove` (given addresses) writes `snapshot.json`,
  `message.txt` and `to_sign/to_sign-NN.psbt`, one per address; the signers
  fill `signed/`; `finalize` writes the proofs file.
* **A proofs file**, `<wallet>-<date>-<height>-proofs.json`, is the only
  thing an auditor needs from a snapshot. It names addresses, proofs and
  outputs, and the policy (`2 of 3`); it carries no descriptor, xpub,
  derivation path or node wallet name. It keeps that one name wherever it
  goes: in the snapshot, beside a statement, on the auditor's disk.
* **The statement** (`bip322 report`) is the document for the
  auditor: opening and closing balances, the movements, the closing UTXOs
  each backed by a verified proof, one page per UTXO with the two commands
  that check it, a section on how a proof works, and notes.

## Where everything lives

A directory the holder chooses, made once with `bip322 init`: it writes
`bip322.toml`, which marks the directory and holds the defaults, and from
then on every command run in it or below finds it, as git finds its
repository (`--dir DIR` or `BIP322_DIR` name it from elsewhere). Inside, one
directory per holder and, under it, one per wallet; the tools name everything
else. `-w` names the wallet, or the wallet directory the command runs in
does, and the wallet names the rest:

```
~/bip322/                                 # wherever bip322 init was run
  bip322.toml
  company/                                # a holder
    treasury/                             # a wallet of the holder
      snapshots/
        2026-03-02-880412/                # a prove during the year
        2026-10-09-920114/                # the year-end snapshot
          snapshot.json  message.txt  to_sign/  signed/
          treasury-2026-10-09-920114-proofs.json
      statements/
        2026/
          treasury-2026.pdf  treasury-2026.json  treasury-2026.csv
          treasury-2026-10-09-920114-proofs.json
    cold/
      snapshots/
    statements/                           # the holder's ledgers over its wallets
      2026-10-15/                         # to date: labelled by the tip's date
        company-2026-10-15.pdf  .json  .csv  + the proofs files it cites
```

Directories are named for their place, so the names stay bare; files that
leave the tree carry wallet (or holder), time and kind in their name. A
wallet's name is unique in the tree, so `-w NAME` finds it under whichever
holder it is. A second wallet is a second directory, created the first time
a command writes into it: under the holder `-w KEY/NAME` names, else the
holder whose directory the command runs in, else `holder` in `bip322.toml`,
else the tree's one holder.
The snapshots hold the PSBTs, which carry the wallet's xpubs: they stay with
the holder, and are backed up like the wallet's own records. The statements
are what leaves.

In `bip322.toml`, top-level keys are defaults for every wallet, a
`[holders.KEY]` table overrides them for the holder's wallets, a
`[wallets.NAME]` table for one, and the command line beats all:

```toml
holder = "company"                  # the default holder: a new -w NAME is made in its directory
wallet = "treasury"                 # the default -w
text = "Proof of control {date}"    # the message template
dust = 1000                         # satoshi; see --dust

[holders.company]
name = "Company AB"                 # the statements' header (default: the key)
text = "Proof of control, Company AB (556123-4567), {date}"   # for the holder's wallets

[wallets.treasury]
node = "treasury-2of3-main"         # the node wallet, when its name is not the directory's

[addresses]
"bc1q…" = "Exchange"                # an address of theirs: the wallet it belongs to
```

`bip322 init -w company/treasury` writes the file with every key shown, all
but `holder`, `wallet` and their tables as comments to switch on.

Other keys: `cli` (how to reach the node), `timeout`, `theme`, `heading`,
`explorer`. Without `-w` and outside a wallet's directory, `wallet` in the
file names the wallet, else the node's one loaded wallet is it.
The other side of a movement, the statement's `wallet` column, is named by
its addresses: a send by the addresses it paid, a receive by the addresses it
came from, which the node reads from the block (Bitcoin Core 25 or later, no
`txindex` needed). A wallet of the tree, the holder's own or another
holder's, names its addresses itself: its node is asked which are its, so a
move between two wallets needs no entry anywhere. `[addresses]` names the
rest, an exchange's say. An address nothing names leaves the cell empty, to
fill in by hand.

### The holder's ledger

A holder with several wallets gets one ledger over all of them:

```sh
bip322 report holder company          # -> company/statements/2026-10-15/: company-2026-10-15.pdf (or .html), .json, .csv
```

A holder is a directory of the tree, and its wallets are the wallet
directories under it; `[holders.KEY]` in `bip322.toml` gives the name the
statements put in their header, else the directory's name is it, and any
other key there is a default for the holder's wallets. The holder
may be left off the command inside its directory, with `holder = KEY` in
`bip322.toml`, or when the tree has one holder. The wallets' histories merge: a transaction that moves coins from one of the holder's
wallets to another is one movement of kind `transfer`, its fee the only
cost, and the balance is the holder's total, each wallet's beside it in the
JSON and the statement. Without a period the ledger runs from the block
before the first transaction to the tip and is labelled by the tip's date;
`--year` and the other period options work as for a wallet's statement. The
CSV has the same six columns in one block per wallet, the wallet's name on a
line of its own above the header: the movements as that wallet sees them, the
amount its net, the balance its own, the `wallet` column the other side, another
wallet of the holder's (`cold`) or what `[addresses]` names (`Exchange`); a move
between two of the holder's wallets is a `transfer` in both blocks. The proofs
come from every wallet's `snapshots/`, so each coin held is also one the
holder has proven control of.

## Install

One line into a fresh venv brings all three packages and the command
`bip322`, whose `bip322 audit …` and `bip322 report …` run the programs
`bip322-audit` and `bip322-report`, git style:

```sh
python3 -m venv ~/.bip322
~/.bip322/bin/pip install "bip322-report[kernel,pdf]"
```

Python 3.11 or newer is needed (3.12 for `[kernel]`); if `python3 --version`
is older, install one beside it and make the venv from that, e.g. on
Ubuntu 22.04 `sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt install
python3.12 python3.12-venv`, then `python3.12 -m venv ~/.bip322`.

`[kernel]` adds Bitcoin Core's consensus library as a second verification
engine; `[pdf]` adds WeasyPrint for the PDF (it needs Pango on the machine;
see the README). Upgrading is the same line with `--upgrade`. The commands talk to the node
through `bitcoin-cli`; `--cli "bitcoin-cli -signet"` or `cli` in
`bip322.toml` says how.

## The year, for the holder

### At year end: the full snapshot

Some days into January, when the closing block is buried:

```sh
bip322 audit snapshot                 # -> treasury/snapshots/2026-01-08-931871/
```

The stamp is the block six behind the tip; the snapshot lists every funded
address and writes one `to_sign` PSBT per address. This is a **full**
snapshot, not `--skip-proven`: every closing UTXO must have a proof dated
after the closing block, including change proven earlier in the year.

### Signing on the devices

Each PSBT is a BIP-322 `to_sign` transaction: it spends a virtual 0-satoshi
output, and a device that implements BIP-322's PSBT-signer role shows the
message and the address it is for. Sign each PSBT on as many devices as
the threshold asks, each from the unsigned copy, and put the results into
the snapshot's `signed/` directory under any names (`to_sign-01-A.psbt`,
`to_sign-01-C.psbt`, …). Some devices refuse a PSBT that already carries
enough signatures; signing the unsigned copy on each avoids the question.
`finalize` combines them. The message template is checked against what
signing devices can display (`bip322 lint-message`). Before the first
finalize of a new device set, `bip322 checksigners signed/*.psbt` verifies
each cosigner's signature alone and every threshold combination.

### Finalize, verify, report

```sh
bip322 audit finalize                 # the newest snapshot: combines, self-verifies, writes the proofs file
bip322 audit verify                   # what the auditor will run; should end in OK
bip322 report --year 2026             # -> treasury/statements/2026/
```

Each takes the wallet from `-w`, else from the wallet directory it runs in,
else from `bip322.toml`, else from the
node; `finalize` and `verify` take a snapshot's name
(`finalize 2026-01-08-931871`) or a path when the newest is not the one
meant.

`bip322 report` reads the wallet's history from the node, verifies every
proofs file under the wallet's snapshots against the node, picks for each
closing UTXO a proof of its script (a verified one first, then one whose
snapshot lists that very UTXO, then the newest), looks each UTXO up on the
chain at the block of its proof, cross-checks the node's `listunspent` when
the period ends at the tip, and prints a summary ending in `RESULT: OK`
(exit status 0) or `RESULT: ATTENTION` (exit status 1); an error exits 2.
ATTENTION means the reconciliation failed, a closing UTXO has no verified
proof, a lookup on the chain contradicts the statement, or the node wallet
disagrees with the closing UTXOs. A proof whose block is before the period's
end, or a UTXO spent after the period, is shown on the UTXO's page and is
not a failed check. On a node that has no block after the period's end yet
(still syncing, or behind), it refuses: the closing block is not settled.

The statement is the PDF when WeasyPrint is installed (`[pdf]`), else one
HTML file with the fonts inside it; `--html` asks for the HTML regardless.

`--dust N` leaves dust out of every figure: an output of at most N satoshi
that others paid to the wallet in a transaction spending nothing of the
wallet's (unsolicited dust is never spent and never booked). The wallet's
own change and consolidations are never dust. Dust the wallet does spend
is booked in the block that spends it, as a movement of its own, so each
opening balance is the closing balance before it. The notes state the rule and the count,
the JSON lists the outputs. A threshold above 10000 satoshi needs
`--dust-force`. `dust` in `bip322.toml` sets it once.

`--snapshots DIR` takes the proofs from elsewhere than the wallet's own
snapshots: a directory of snapshots, one snapshot, or one proofs file, and
may repeat; a path that does not exist or holds no proofs file is an error.

### What to hand over

The statement directory, `treasury/statements/2026/`: the PDF, the
JSON with every figure, the CSV of movements, and a copy of each proofs
file the statement cites on a UTXO's page, under its own name. No other
proofs file is copied or named. Never the snapshots: their PSBTs carry the
wallet's xpubs.

## During the year, for the holder

**Before a spend.** Build the transaction, note its change address, and
prove it before broadcasting:

```sh
bip322 audit prove bc1q…change…       # -> snapshots/<date>-<height>/
# sign to_sign/*.psbt, put the results in signed/, then
bip322 audit finalize                 # VALID -> broadcast
```

A valid proof means the quorum controls where the change goes. In a
statement produced before the year-end snapshot, that UTXO shows AFTER THE
PROOF (received after the block its proof names); the year-end snapshot
gives it a proof dated after it.

**New deposit addresses.** Prove them the same way, or after the fact:

```sh
bip322 audit snapshot --skip-proven
```

which takes a snapshot over the funded addresses no earlier snapshot of the
wallet proves, and exits non-zero saying so when there are none (in a
script, treat that as "nothing to do").

**A statement at any time.** `bip322 report --from 2026-01-01 --to 2026-07-01`,
or `--year`, or block heights. Everything in it is backed by the holder's own
proofs; the year-end statement is the same document with the year-end
snapshot.

## The auditor's procedure

What arrives: the statement directory. What is needed: a Bitcoin Core node
with the chain (no wallet, no txindex), and `pip install bip322-audit`
(`bip322-audit[kernel]` on Linux x86_64 with Python 3.12 adds Bitcoin Core's
script interpreter as a second engine; bip322-report is not needed to verify).

1. **Read the statement.** Page 1: the movements, whose closing row is the
   holdings on the chain, and the closing UTXOs. Any failed check appears
   on page 1 as ATTENTION with a checklist; otherwise nothing says OK
   beyond the figures. A movement of kind `mixed` was funded by the wallet
   and by others: its fee is unknown and shown as such, its amount is the
   wallet's net.
2. **Re-verify the proofs** on your own node:
   `bip322 audit verify treasury-2026-01-08-931871-proofs.json`.
   This checks every signature with every engine installed, that the
   block named in the message exists at that height with that time, and
   that every output the file lists existed at that block.
3. **Re-run any page.** Each UTXO page prints two commands with their
   output: `bip322 verifymessage <scriptPubKey> <proof> "<message>"`, and
   `bip322 audit holdings <txid>:<vout> --at <block>`. Paste them as they
   stand; the backslashes continue the lines. For a second opinion on a
   proof, the reference checks in the bip322-core repository run other implementations beside the
   verifier.
4. **Read section 4** for what a proof shows: that at the block named the
   holder could produce a valid spend from the scriptPubKey, which is what
   controlling the UTXOs locked to it means; not who owns the coins, not
   what liabilities stand against them, nothing about any later time.

## Privacy

The auditor learns the wallet's funded addresses, their UTXOs and the
movements, which the statement is for. The auditor does not learn the
descriptor or the xpubs, so cannot derive addresses beyond those shown or
follow the wallet after the period: the proofs file and the statement carry
none of that, and the snapshots' PSBTs, which do, are never handed over.
Proofs files the statement does not cite (older ones, or another wallet's
given with `--snapshots`) are neither named nor copied. On mainnet the
statement links transactions and scripts to a public block explorer
(`--explorer ''` for none, `--explorer URL` for another); on other chains
there are no links unless `--explorer` is given.

## Known limitations

* The node cross-check (`listunspent` against the closing UTXOs) runs only
  when the period ends at the tip the history was read at and the node is
  still at that block; otherwise the summary says it was not run, which is
  not a failure. A UTXO that an unconfirmed transaction spends is noted
  there as such, not as a mismatch.
* Opening + net = closing shows that the statement adds up, not that the
  history is complete: all three figures come from the same history.
* Completeness rests on the node wallet: the statement covers the
  addresses the node knows, which is the descriptor's whole range as
  imported. That the wallet is the holder's only one is the holder's
  representation.
* The dust rule is the holder's choice, stated in the notes; an auditor may
  ask for a lower threshold, or for none.
