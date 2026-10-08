"""The node wallet's transaction history, and the wallet's coins at any block height.

Everything comes from the node wallet (``listtransactions``, ``gettransaction``,
``getaddressinfo``); no index is needed, because every transaction that spends
one of the wallet's outputs is itself a wallet transaction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from bip322audit.rpc import BitcoinCli, RpcError, btc, to_sat

from . import TOOL

Outpoint = tuple[str, int]
PAGE = 1000


@dataclass(frozen=True)
class Coin:
    """An output: one paying an address of the wallet, or one paying somebody else."""

    txid: str
    vout: int
    address: str | None  # None for an output of somebody else's that has no address (a bare script, OP_RETURN with a value)
    amount_sat: int
    height: int | None = None  # the block that created it

    @property
    def outpoint(self) -> Outpoint:
        return (self.txid, self.vout)

    def to_dict(self) -> dict:
        return {
            "txid": self.txid,
            "vout": self.vout,
            "address": self.address,
            "amount_sat": self.amount_sat,
            "amount_btc": btc(self.amount_sat),
            "height": self.height,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Coin:
        return cls(d["txid"], int(d["vout"]), d.get("address"), int(d["amount_sat"]), d.get("height"))


@dataclass
class WalletTx:
    """One transaction that touches the wallet: what it took from the wallet, what it gave, what left."""

    txid: str
    height: int | None  # None while unconfirmed
    blockhash: str | None
    time: int  # block time when confirmed, else the wallet's first-seen time
    ours_in: list[Coin] = field(default_factory=list)  # the wallet's outputs this transaction spent
    ours_out: list[Coin] = field(default_factory=list)  # outputs to the wallet's addresses
    others_out: list[Coin] = field(default_factory=list)  # outputs to addresses outside the wallet
    fee_sat: int | None = None  # exact, when every input was the wallet's
    others_in: int | None = None  # inputs that were not the wallet's; None in a history saved before this was recorded
    blockindex: int | None = None  # position in its block; None while unconfirmed, and in a history saved before this was recorded
    swept: bool = False  # not a transaction: dust entering the books in the block where the wallet spent it (History.without_dust)

    @property
    def net_sat(self) -> int:
        return sum(c.amount_sat for c in self.ours_out) - sum(c.amount_sat for c in self.ours_in)

    @property
    def mixed(self) -> bool:
        """Funded by this wallet and by others together: what the wallet paid of the fee cannot be known."""
        if not self.ours_in:
            return False
        return self.fee_sat is None if self.others_in is None else self.others_in > 0  # the fee is computed only when every input is ours

    @property
    def kind(self) -> str:
        """receive (nothing of ours spent), mixed (others' inputs beside ours), internal (everything came back), send; swept for dust booked late."""
        if self.swept:
            return "swept"
        if not self.ours_in:
            return "receive"
        if self.mixed:
            return "mixed"
        if not self.others_out:
            return "internal"
        return "send"

    @property
    def iso_time(self) -> str:
        return datetime.fromtimestamp(self.time, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def to_dict(self) -> dict:
        return {
            "txid": self.txid,
            "height": self.height,
            "blockhash": self.blockhash,
            "time": self.time,
            "time_utc": self.iso_time,
            "kind": self.kind,
            "net_sat": self.net_sat,
            "net_btc": btc(self.net_sat),
            "fee_sat": self.fee_sat,
            "fee_btc": btc(self.fee_sat) if self.fee_sat is not None else None,
            "others_in": self.others_in,
            "blockindex": self.blockindex,
            "ours_in": [c.to_dict() for c in self.ours_in],
            "ours_out": [c.to_dict() for c in self.ours_out],
            "others_out": [c.to_dict() for c in self.others_out],
        }

    @classmethod
    def from_dict(cls, d: dict) -> WalletTx:
        return cls(
            d["txid"],
            d.get("height"),
            d.get("blockhash"),
            int(d["time"]),
            [Coin.from_dict(c) for c in d.get("ours_in", [])],
            [Coin.from_dict(c) for c in d.get("ours_out", [])],
            [Coin.from_dict(c) for c in d.get("others_out", [])],
            d.get("fee_sat"),
            d.get("others_in"),
            d.get("blockindex"),
        )


@dataclass
class History:
    chain: str
    wallet: str | None
    tip_height: int
    tip_hash: str
    fetched_utc: str
    txs: list[WalletTx]  # confirmed, oldest first
    pending: list[WalletTx]  # unconfirmed at fetch time

    # ---- queries ----------------------------------------------------------- #

    def coins_at(self, height: int) -> list[Coin]:
        """The wallet's unspent outputs after every block up to and including ``height``."""
        spent = {c.outpoint for tx in self.txs if tx.height is not None and tx.height <= height for c in tx.ours_in}
        return [c for tx in self.txs if tx.height is not None and tx.height <= height for c in tx.ours_out if c.outpoint not in spent]

    def balance_at(self, height: int) -> int:
        return sum(c.amount_sat for c in self.coins_at(height))

    def between(self, start_height: int, end_height: int) -> list[WalletTx]:
        """Confirmed transactions with ``start_height < height <= end_height``."""
        return [tx for tx in self.txs if tx.height is not None and start_height < tx.height <= end_height]

    def _dust(self, threshold_sat: int) -> set[Outpoint]:
        """The outputs that are dust: at most ``threshold_sat``, paid by others in a transaction spending nothing of the wallet's."""
        if threshold_sat <= 0:
            return set()
        return {c.outpoint for tx in (*self.txs, *self.pending) if not tx.ours_in for c in tx.ours_out if c.amount_sat <= threshold_sat}

    def dust_at(self, threshold_sat: int, height: int) -> list[Coin]:
        """The dust held after block ``height``: what ``without_dust`` leaves out of the coins at that block."""
        dust = self._dust(threshold_sat)
        return [c for c in self.coins_at(height) if c.outpoint in dust]

    def without_dust(self, threshold_sat: int) -> History:
        """This history as if its dust had not been the wallet's until the wallet spent it.

        Dust is an output of at most ``threshold_sat`` that others paid to
        the wallet in a transaction spending nothing of the wallet's.  The
        wallet's own change and consolidations are never dust, whatever their
        size: they are what is left of coins already in the books.  Dust is
        left out where it was received, and a transaction that received
        nothing else disappears.

        Dust the wallet does spend enters the books in the block that spends
        it: a row of kind ``swept``, named after the output, stands right
        before the spending transaction, which itself is unchanged, so what it
        paid and its fee stay exact.  Whether an output is dust does not
        depend on any height: a statement's opening balance is always the
        closing balance of the statement before it.  With a threshold of 0
        the history is returned unchanged.
        """
        dust = self._dust(threshold_sat)
        if not dust:
            return self

        def rewrite(txs: list[WalletTx]) -> list[WalletTx]:
            out: list[WalletTx] = []
            for tx in txs:
                kept = replace(tx, ours_out=[c for c in tx.ours_out if c.outpoint not in dust])
                # booked now, in the spender's block and just before it; the coin keeps its outpoint, so the spend finds it
                out += [
                    WalletTx(c.txid, tx.height, tx.blockhash, tx.time, ours_out=[c], blockindex=tx.blockindex, swept=True)
                    for c in tx.ours_in
                    if c.outpoint in dust
                ]
                if kept.ours_in or kept.ours_out:
                    out.append(kept)
            return out

        return History(self.chain, self.wallet, self.tip_height, self.tip_hash, self.fetched_utc, rewrite(self.txs), rewrite(self.pending))

    def spender_of(self, outpoint: Outpoint) -> WalletTx | None:
        for tx in self.txs:
            if any(c.outpoint == outpoint for c in tx.ours_in):
                return tx
        return None

    # ---- (de)serialization ------------------------------------------------- #

    def to_dict(self) -> dict:
        return {
            "tool": TOOL,
            "chain": self.chain,
            "wallet": self.wallet,
            "tip_height": self.tip_height,
            "tip_hash": self.tip_hash,
            "fetched_utc": self.fetched_utc,
            "transactions": [tx.to_dict() for tx in self.txs],
            "pending": [tx.to_dict() for tx in self.pending],
        }

    @classmethod
    def from_dict(cls, d: dict) -> History:
        return cls(
            d["chain"],
            d.get("wallet"),
            int(d["tip_height"]),
            d["tip_hash"],
            d.get("fetched_utc", ""),
            _chain_order([WalletTx.from_dict(t) for t in d.get("transactions", [])]),
            [WalletTx.from_dict(t) for t in d.get("pending", [])],
        )

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n")

    @classmethod
    def load(cls, path: Path) -> History:
        return cls.from_dict(json.loads(Path(path).read_text()))


# --------------------------------------------------------------------------- #
# fetching
# --------------------------------------------------------------------------- #


def fetch_history(cli: BitcoinCli, *, progress=None) -> History:
    """Every transaction of the node wallet, with the wallet's side of each resolved exactly.

    Two passes: first every output paying a wallet address (``getaddressinfo``
    says which), then every input that spends one of those.  The fee is
    computed from the inputs and outputs when all inputs are the wallet's,
    so it does not depend on the wallet's own bookkeeping.  Transactions that
    are neither in the chain nor on their way into it (conflicted, abandoned,
    replaced) are left out.
    """
    try:
        wallet_name = str(cli.call("getwalletinfo")["walletname"])
    except RpcError as exc:
        raise RpcError(f"a node wallet is needed for the transaction history: {exc}") from exc
    chain = cli.chain()
    tip_height, tip_hash = cli.tip()
    txids = _wallet_txids(cli)
    if progress:
        progress(f"{len(txids)} wallet transactions to read")
    mine: dict[str, bool] = {}

    def is_mine(address: str) -> bool:
        if address not in mine:
            info = cli.call("getaddressinfo", address)
            mine[address] = bool(info.get("ismine") or info.get("iswatchonly"))
        return mine[address]

    raw: list[tuple[dict, list[Coin], list[Coin]]] = []
    known: dict[Outpoint, Coin] = {}
    for txid in txids:
        tx = cli.call("gettransaction", txid, True, True)
        confirmations = int(tx.get("confirmations", 0))
        if confirmations < 0 or (confirmations == 0 and _given_up(cli, tx)):
            continue  # conflicted, abandoned or replaced: not part of the chain, and not unconfirmed either
        height = int(tx["blockheight"]) if tx.get("blockheight") is not None else None
        ours_out: list[Coin] = []
        others_out: list[Coin] = []
        for v in tx["decoded"]["vout"]:
            address = v.get("scriptPubKey", {}).get("address")
            if not address and not to_sat(v["value"]):
                continue  # OP_RETURN carrying data only: nothing was paid to it
            coin = Coin(txid, int(v["n"]), address, to_sat(v["value"]), height)  # without an address it is never the wallet's
            (ours_out if address and is_mine(address) else others_out).append(coin)
        for c in ours_out:
            known[c.outpoint] = c
        raw.append((tx, ours_out, others_out))

    txs: list[WalletTx] = []
    pending: list[WalletTx] = []
    for tx, ours_out, others_out in raw:
        vin = [v for v in tx["decoded"]["vin"] if "coinbase" not in v]
        ours_in = [known[(v["txid"], int(v["vout"]))] for v in vin if (v["txid"], int(v["vout"])) in known]
        fee = None
        if vin and len(ours_in) == len(vin):
            fee = sum(c.amount_sat for c in ours_in) - sum(to_sat(v["value"]) for v in tx["decoded"]["vout"])
        height = int(tx["blockheight"]) if tx.get("blockheight") is not None else None
        entry = WalletTx(
            tx["txid"],
            height,
            tx.get("blockhash"),
            int(tx.get("blocktime") or tx.get("time") or 0),
            ours_in,
            ours_out,
            others_out,
            fee,
            len(vin) - len(ours_in),
            int(tx["blockindex"]) if height is not None and tx.get("blockindex") is not None else None,
        )
        (txs if height is not None else pending).append(entry)
    return History(chain, wallet_name, tip_height, tip_hash, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), _chain_order(txs), pending)


def _given_up(cli: BitcoinCli, tx: dict) -> bool:
    """An unconfirmed wallet transaction that will not confirm as things stand: abandoned, or displaced by a conflicting one.

    ``mempoolconflicts`` (Bitcoin Core 28 and later) names a conflicting
    transaction that is in the mempool, so this one is not.  An earlier node
    only lists ``walletconflicts``, for both sides of a replacement: there
    the mempool says which side is live.
    """
    if any(d.get("abandoned") for d in tx.get("details") or []):
        return True
    if tx.get("mempoolconflicts"):
        return True
    if tx.get("walletconflicts"):
        try:
            cli.call("getmempoolentry", tx["txid"])
        except RpcError:
            return True
    return False


def _chain_order(txs: list[WalletTx]) -> list[WalletTx]:
    """Confirmed transactions in the chain's order: by block, and within a block by position.

    Where a position is missing (a history saved before it was recorded) the
    block's transactions are put so that none precedes one whose output it
    spends, then by txid: a running balance never dips below what was held.
    """
    blocks: dict[int, list[WalletTx]] = {}
    for tx in txs:
        blocks.setdefault(tx.height, []).append(tx)
    out: list[WalletTx] = []
    for height in sorted(blocks):
        block = sorted(blocks[height], key=lambda t: t.txid)
        if all(t.blockindex is not None for t in block):
            out.extend(sorted(block, key=lambda t: t.blockindex))
            continue
        here = {t.txid for t in block}
        placed: set[str] = set()
        while block:
            ready = next((t for t in block if all(c.txid not in here or c.txid in placed for c in t.ours_in)), block[0])
            block.remove(ready)
            placed.add(ready.txid)
            out.append(ready)
    return out


def _wallet_txids(cli: BitcoinCli) -> list[str]:
    """Every txid the wallet knows, oldest first, paged through listtransactions."""
    seen: dict[str, None] = {}  # a dict for its order: one entry per transaction, whatever number of rows it has
    skip = 0
    while True:
        rows = cli.call("listtransactions", "*", PAGE, skip, True) or []
        seen.update(dict.fromkeys(row["txid"] for row in rows))
        if len(rows) < PAGE:
            return list(seen)
        skip += PAGE
