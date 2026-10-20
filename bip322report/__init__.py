"""bip322-report: the statement of a period, in which every coin is backed by a verified BIP-322 proof.

Owner-side tooling on top of bip322-audit.  It reads the node wallet's
transaction history, resolves a reporting period to block heights, and
combines the wallet's coins at the period's end with the proofs files of the
snapshots bip322-audit produced, so a reader gets: opening and closing
balances, every movement in between, a reconciliation, and for each closing
coin the proof of control that covers it, re-verified at report time.

* ``history``  the wallet's transactions from the node, cached as JSON
* ``block``    the last block before a UTC date or time
* ``balance``  the coins and balance at a height or date
* ``report``   the holder's statement over the wallets under it, for a period or to date: PDF (or HTML), JSON, CSV, into
               ``<holder>/statements/`` in the data directory; the default
"""

from ._version import __version__

TOOL = f"bip322-report {__version__}"
