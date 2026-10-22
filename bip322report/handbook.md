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
  it; the statement wants, for every closing UTXO, a proof dated at its
  closing block or after it, since that is control at the period's end.
* **A snapshot** is one round of proofs at one block: `bip322 audit
  snapshot` (every funded address, with the wallet's coins at that block,
  spent since or not) or `prove` (given addresses) writes `snapshot.json`,
  `message.txt` and `to_sign/to_sign-NN.psbt`, one per address; the signers
  fill `signed/`; `finalize` writes the proofs file. The block is the last
  one before an instant (`--before 2027-01-01`: the last block of 2026), a
  named one (`--at`), or by default the last block before today 00:00 UTC,
  the night before, which a statement to date closes on.
* **A proofs file**, `<wallet>-<date>-<height>-proofs.json`, is the only
  thing an auditor needs from a snapshot. It names addresses, proofs and
  outputs, and the policy (`2 of 3`); it carries no descriptor, xpub,
  derivation path or node wallet name. It keeps that one name wherever it
  goes: in the snapshot, beside a statement, on the auditor's disk.
* **The statement** (`bip322 report`) is the document for the
  auditor: a summary from the opening holdings to the closing ones, the
  movements with their transaction ids, the closing UTXOs each backed by a
  verified proof, one page per UTXO with the commands that check it, a
  section on how a proof works, and notes. The statement is the holder's,
  over every wallet under it: a holder of several wallets gets each wallet's
  share on the first page and the movements in one block per wallet.

## Where everything lives

A directory the holder chooses, made once with `bip322 init`: it writes
`bip322.toml`, which marks the directory and holds the defaults, and from
then on every command run in it or below finds it, as git finds its
repository (`--dir DIR` or `BIP322_DIR` name it from elsewhere). Inside, one
directory per holder and, under it, one per wallet; the tools name everything
else. A wallet's directory holds its snapshots and its history, nothing
else; the holder's holds its wallets and its statements:

```
~/bip322/                                 # wherever bip322 init was run
  bip322.toml
  company/                                # a holder
    statements/                           # the holder's statements over its wallets
      2026/
        company-2026.pdf  company-2026.json  company-2026.csv
        treasury-2026-12-31-931871-proofs.json   # the proofs files the statement cites
        company-2026-history.pdf          # the movements for a reader: no proofs, no txids
      2026-10-20/                         # to date: labelled by its closing block's date, the night before
    treasury/                             # a wallet of the holder
      2026-03-02-880412/                  # a prove during the year
      2026-12-31-931871/                  # the year-end snapshot: the last block of 2026
        snapshot.json  message.txt  to_sign/  signed/
        treasury-2026-12-31-931871-proofs.json
      history.json                        # the history last read from the node
    cold/
      2026-12-31-931871/
      history.json
```

Directories are named for their place, so the names stay bare; files that
leave the tree carry wallet (or holder), time and kind in their name. The
tree is the declaration: a directory under a holder is a wallet from the
moment it exists, so `bip322 init -w company/treasury` makes the first and
`mkdir company/cold` adds another, and the statement knows the holder's
wallets before any proof is taken. A wallet's name is unique in the tree, so
`-w NAME` finds it under whichever holder it is; a wallet the audit commands
make themselves goes under the holder `-w KEY/NAME` names, else the holder
whose directory the command runs in, else `holder` in `bip322.toml`, else
the tree's one holder. The statement takes the holder the same way, from
the word after the options, the directory, the file, or the tree.
The snapshots hold the PSBTs, which carry the wallet's xpubs: they stay with
the holder, and are backed up like the wallet's own records. The statements
are what leaves. Versions before bip322-audit 0.22 kept a wallet's snapshots
under `<wallet>/snapshots/` and its own statements beside them; such a
directory is refused with the `mv` that moves the snapshots up, and the old
statements are simply no longer read.

In `bip322.toml`, top-level keys are defaults for every wallet, a
`[holders.KEY]` table overrides them for the holder's wallets, a
`[wallets.NAME]` table for one, and the command line beats all:

```toml
holder = "company"                  # the default holder: a new -w NAME is made in its directory
dust = 1000                         # satoshi; see --dust

[holders.company]
name = "Company AB (556123-4567)"   # the statements' header, and the signed message: Proof of control, Company AB (556123-4567), <date>

[wallets.treasury]
node = "treasury-2of3-main"         # the node wallet, when its name is not the directory's

[addresses]
"bc1q…" = "Exchange"                # an address of theirs: the wallet it belongs to
```

`bip322 init -w company/treasury` writes the file with every key shown, all
but `holder` and the two tables as comments to switch on, and makes the
directories `company/treasury/`.

The holder's `name` is one string used twice: the statement's header, and
the message every proof signs, `Proof of control, <name>, <date>` over the
stamp line, so that what is stated and what is signed cannot drift apart.
It must be printable ASCII for the signing devices; `bip322 audit snapshot
--text` gives one snapshot another message. The keys `wallet` and `text` of
earlier versions are no longer read, and a file that still has them is told.

Other keys: `cli` (how to reach the node), `timeout`, `theme`, `heading`,
`explorer`. The statement's own settings, `dust`, `theme`, `heading` and
`explorer`, are read from `[holders.KEY]` and the top level, not from a
wallet's table, which carries what is read per wallet (`node`, `cli`); the
statement says so when it finds one there. `history` and `balance` take the
wallet from `-w`, else the wallet directory they run in, else the node's one
loaded wallet.
The other side of a movement, the statement's `wallet` column, is named by
its addresses: a send by the addresses it paid, a receive by the addresses it
came from, which the node reads from the block (Bitcoin Core 25 or later, no
`txindex` needed). A wallet of the tree, the holder's own or another
holder's, names its addresses itself: its node is asked which are its, so a
move between two wallets needs no entry anywhere. A movement to or from an
account the holder keeps a record for (below) is named by that account's own
record. `[addresses]` names the rest, a payee's say. An address nothing names
leaves the cell empty, and the summary counts such movements.

### The holder's statement

The statement is the holder's, over every wallet under its directory, and
it asks for the proofs it lacks:

```sh
bip322 report --year 2026             # the statement, or ATTENTION naming what is unproven
bip322 report --year 2026 --prove     # the snapshots the statement lacks, at its closing block, one per wallet; no statement yet
# sign to_sign/*.psbt of each into its signed/
bip322 report --year 2026             # finalizes what has been signed, then -> company/statements/2026/: company-2026.pdf, .json, .csv, -history.pdf
bip322 report company                 # to date: -> company/statements/2026-10-20/, closing on the night before
```

The first run is for looking: which wallets, which movements, what is
proven and what is not. `--prove` takes, for every wallet whose closing
UTXOs have no proof at or after the closing block, the full snapshot at that
block, through `bip322 audit snapshot -w WALLET --at HEIGHT`, and prints the
directories; a wallet with such a snapshot awaiting signatures is left
alone and named, and one the node has not loaded is refused with that
reason. The rerun finalizes every snapshot at the closing block whose
`signed/` holds PSBTs and which has no proofs file yet, as `bip322 audit
finalize` would, then builds the statement. The date is given once; the
block is never typed.

A holder is a directory of the tree, and its wallets are the wallet
directories under it; `[holders.KEY]` in `bip322.toml` gives the name the
statement puts in its header, else the directory's name is it, and any
other key there is a default for the holder's wallets. The holder
may be left off the command inside its directory, with `holder = KEY` in
`bip322.toml`, or when the tree has one holder. The wallets' histories merge: a transaction that moves coins from one of the holder's
wallets to another is one movement of kind `transfer`, its fee the only
cost, and the balance is the holder's total; the first page lists each
wallet's share under the opening holdings and above the closing ones, and the
movements are one block per wallet, a move between two of them sent by the
one and received by the other under the same transaction for the same
amount, the fee with the sender. A holder of one wallet gets the same
document without the split, and so does a holder whose other wallets had
nothing in the period, no movement and nothing held at either end: those are
named in the notes and shown nowhere else. The opening line cites the statement
in the tree that closed at this one's opening block, by label and reference,
so the statements chain. Without a period the statement runs from the block
before the first transaction to the last block before today 00:00 UTC, the
night before, and is labelled by that block's date; a period whose end lies
ahead (`--year` of the current year) closes there too. That is the block
`bip322 audit snapshot` stamps by default, so a snapshot and a statement of
the same day agree on their block. The
CSV has six columns in one block per wallet, the wallet's name on a
line of its own above the header (a holder of one wallet: that wallet's rows
alone): the movements as that wallet sees them, the
amount its net, the balance its own, the `wallet` column the other side, another
wallet of the holder's (`cold`) or what `[addresses]` names (`Exchange`); a move
between two of the holder's wallets is a send in one block and a receive in
the other. The proofs come from every wallet's directory, so each coin held
is also one the holder has proven control of.

Beside every statement goes
`<holder>-<label>-history.pdf`: the same movements for a reader who is not the
auditor, a bank or a partner asked for the history. Each wallet is a section,
oldest first, with the date, the counterparty, the
amount before the fee, the fee and the running balance; no transaction
identifiers, no proofs, no addresses. The statement with the proofs stays with the auditor, and the
two carry the same reference.

Every history read from the node is kept in the wallet's directory as
`history.json`. A wallet the node has not loaded any more, an old one that
was emptied into the next, is read from that file: one statement
made while it was loaded is enough. The file serves only if the wallet held
nothing at the file's tip, since nothing can have left it after that; a
wallet that still held coins has to be loaded. The summary and the statement's
JSON say which wallets came from a file. A deposit into an unloaded wallet is
not seen until it is loaded again.

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

Once the year is over, whenever suits:

```sh
bip322 report --year 2026 --prove     # -> company/treasury/2026-12-31-931871/, and the same for every other wallet of the holder's
```

The stamp is the last block of 2026, the block the statement of 2026 closes
on, and each snapshot lists the wallet's coins at that block, what it has
spent since included (from the node wallet's own history), with one
`to_sign` PSBT per address and the message `Proof of control, <the holder's
name>, 2026-12-31`. This is a **full** snapshot: the statement wants, for
every closing UTXO, a proof dated at the closing block or after it, change
proven earlier in the year included; a proof from earlier in the year shows
control then, not at the year's end. The same snapshot by hand is
`bip322 audit snapshot -w treasury --before 2027-01-01`.

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
finalize of a new device set, `bip322 audit checksigners` verifies each
cosigner's signature alone and every threshold combination over the
snapshot's PSBTs (`bip322 checksigners FILE...` does the same for loose
files).

### Finalize, verify, report

```sh
bip322 report --year 2026             # finalizes the signed snapshots at the closing block, then -> company/statements/2026/
bip322 audit verify -w treasury       # what the auditor will run on the proofs file; should end in OK
```

`bip322 audit finalize -w treasury` does the finalizing by hand, for a
snapshot the statement is not about.

The audit commands take the wallet from `-w`, else from the wallet directory
they run in, else from `bip322.toml`, else from the node; `finalize` and
`verify` take a snapshot's name (`finalize 2026-12-31-931871`) or a path when
the newest is not the one meant. `bip322 report` takes the holder: the word
after the options, else the holder whose directory it runs in, else `holder`
in `bip322.toml`, else the tree's one.

`bip322 report` reads each wallet's history from the node, verifies every
proofs file in the wallets' directories against the node, picks for each
closing UTXO a proof of its script dated at or after the closing block (a
verified one first, then the one at the closing block, then the nearest
after it; a proof from before the closing block is shown but does not
count), looks each UTXO up on the chain at the block of its proof,
cross-checks the node's `listunspent` when nothing of the wallet's moved
after the closing block, and prints a summary ending in `RESULT: OK` (exit
status 0) or `RESULT: ATTENTION` (exit status 1); an error exits 2.
ATTENTION means the reconciliation failed, a closing UTXO has no valid proof
at or after the closing block, a lookup on the chain contradicts the
statement, or the node wallet disagrees with the closing UTXOs; for the
proofs, the summary names the snapshot to take (`bip322 audit snapshot -w
treasury --at 931871`). A UTXO spent after the period is shown on its page
and is not a failed check. On a node that has no block after the period's
end yet (still syncing, or behind), it refuses: the closing block is not
settled.

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

`--snapshots DIR` takes the proofs from elsewhere than the wallets'
directories: a directory of snapshots, one snapshot, or one proofs file, and
may repeat; a path that does not exist or holds no proofs file is an error.

### What to hand over

The statement directory, `company/statements/2026/`: the PDF, the
JSON with every figure, the CSV of movements, the reader's
`company-2026-history.pdf`, and a copy of each proofs
file the statement cites on a UTXO's page, under its own name. No other
proofs file is copied or named. Never the snapshots: their PSBTs carry the
wallet's xpubs. The accounts' records and the exchanges' raw answers stay
too; the statement names them by hash, and they go on request.

## The accounts' own records: the movements file

Coins come into the wallets from somewhere and leave for somewhere, and the
far end of most movements is an account the holder keeps with an exchange.
That account's own record of its deposits and withdrawals, the one a tax
authority asks for, is what names those movements in the statement, not an
address table. The holder keeps it in the tree, one directory per account:

```
  company/
    bitstamp/                             # a custodial account, named as the holder likes
      movements.json                      # the account's deposits and withdrawals, in the movements format
      raw/                                # the exchange's own export or API answers, verbatim, named in movements.json with their hashes
```

`movements.json` is written by a tool of the holder's that asks the
exchange's API or reads its export, keeps the answer verbatim under `raw/`,
and translates it into the format below; the script knows the exchange, the
public packages know only the format. An entry is seen from the account's
side: a `withdrawal` left the account for a wallet, which received it; a
`deposit` arrived at the account from a wallet, which sent it. `amount_sat`
is what the chain moved, `fee_sat` the account's own fee when the record has
it, `address` the wallet's for a withdrawal and the account's for a deposit,
`reference` the record's own id. `currency`, `source` and the three last
fields may be left out; other keys are kept and not read.

```json
{
  "movements": 1,
  "currency": "EUR",
  "source": {"name": "raw/crypto-transactions-2027-01-08.json", "sha256": "…"},
  "entries": [
    {"kind": "withdrawal", "time_utc": "2026-03-02T14:05:11Z", "txid": "…", "amount_sat": 10000000,
     "fee_sat": 5000, "address": "bc1q…", "reference": "W-12345"},
    {"kind": "deposit", "time_utc": "2026-06-10T09:00:00Z", "txid": "…", "amount_sat": 20000000,
     "address": "bc1q…", "reference": "D-67"}
  ]
}
```

The statement matches each entry's txid against the wallets' transactions. A
movement the record matches is named after the account, with the record's
reference under the name, and its amount is checked against the chain. Two
things fail the check "the accounts' own records agree with the movements":
an entry dated in the period that no wallet of the statement saw (coins the
account sent somewhere else, or received from somewhere else), and an amount
that disagrees with the chain. Both are spelt out in section 1. The summary
also counts the movements that have no named far end at all, which is where
an auditor looks next; it is not a failed check, since a payee the holder has
not named is the books' business. The movements files and the raw answers stay
with the holder, like the histories; the statement carries the account's
name, the references and the source's hash, and the auditor asks for the raw
export when they want it, as they would for a bank.

## During the year, for the holder

**Before a spend.** Build the transaction, note its change address, and
prove it before broadcasting:

```sh
bip322 audit prove bc1q…change…       # -> treasury/<date>-<height>/, a recent block
# sign to_sign/*.psbt, put the results in signed/, then
bip322 audit finalize                 # VALID -> broadcast
```

A valid proof means the quorum controls where the change goes: it is the
holder's own check, before the coins move. In a statement it counts only
when its block is at or after the closing block, which it rarely is; the
year-end snapshot gives the change a proof at the closing block, and until
then the statement shows the change UTXO as proven before it.

**Lost keys.** A coin whose keys are gone is on the chain for ever and can
never be proven. Write it off, as the books do, in `bip322.toml`:

```toml
[written_off]
"c87ff7…:1" = { date = "2024-06-01", reason = "keys of the old wallet lost" }
```

The statement of the period that holds the date books it as a row of its
own, `Written off, keys of the old wallet lost`, the amount in parentheses,
in the first block of that day; a line on the first page sums the period's
write-offs. From that block on the coin is no longer a holding and no proof
is asked for it; before it, it was held as it was, and a statement that closed
on it then still shows it. The notes say what was written off and why, and
that the output stays on the chain, unspent: what is declared is a loss, not
a payment. A write-off of a coin the wallets never had, or one already spent
by that date, is refused.

**New deposit addresses.** Prove them the same way, or after the fact:

```sh
bip322 audit snapshot --skip-proven
```

which takes a snapshot over the funded addresses no earlier snapshot of the
wallet proves, and exits non-zero saying so when there are none (in a
script, treat that as "nothing to do").

**A statement at any time.** The same three runs, with no period for a
statement to date, which closes on the last block before today 00:00 UTC:

```sh
bip322 report --prove                 # the snapshots at the night before
# sign
bip322 report                         # finalizes them, then the statement to date
```

For a period, `--from 2026-01-01 --to 2026-07-01` on both runs. The year-end
statement is the same document with the year-end snapshots.

## The auditor's procedure

What arrives: the statement directory. What is needed: a Bitcoin Core node
with the chain (no wallet, no txindex), and `pip install bip322-audit`
(`bip322-audit[kernel]` on Linux x86_64 with Python 3.12 adds Bitcoin Core's
script interpreter as a second engine; bip322-report is not needed to verify).

1. **Read the statement.** Page 1: the summary, from the opening holdings
   through what was received, sent and paid in fees to the closing
   holdings, which are the holdings on the chain; the opening line names
   the statement before, whose closing holdings they were. For a holder,
   each wallet's share is listed under the opening holdings and above the
   closing ones. One sentence says what is proven; any failed check
   appears instead as ATTENTION with a checklist. Then the movements, one
   block per wallet for a holder, each transaction with its id, a
   move between wallets on both sides under the same one, and the closing
   UTXOs with each proof's verdict. A joint
   transaction was funded by the wallet and by others: its fee is unknown
   and shown as such, its amount is the wallet's net.
2. **Re-verify the proofs** on your own node:
   `bip322 audit verify treasury-2026-12-31-931871-proofs.json`.
   This checks every signature with every engine installed, that the
   block named in the message exists at that height with that time, and
   that every output the file lists was held at that block: unspent now,
   or spent after it by the transaction the file records.
3. **Re-run any page.** Each UTXO page prints the commands with their
   output: `bip322 verifymessage <scriptPubKey> <proof> "<message>"` and
   `bip322 audit holdings <txid>:<vout> --at <block>`. Paste them as they
   stand; the backslashes continue the lines. A movement's transaction id
   links to it on the explorer. For a second opinion on a
   proof, the reference checks in the bip322-core repository run other implementations beside the
   verifier.
4. **Read section 4** for what a proof shows: that after the block named,
   the statement's closing block or a later one, the holder could produce
   a valid spend from the scriptPubKey, which is what controlling the
   UTXOs locked to it means; not who owns the coins, not what liabilities
   stand against them, nothing about any later time.

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
  while the closing UTXOs are the present ones: nothing of the wallet's
  moved after the closing block, and the node is still at the block the
  history was read at; otherwise the summary says it was not run, which is
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
