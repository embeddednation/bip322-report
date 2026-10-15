"""The holder's ledger: several wallets' histories as one.

A holder is a directory of the tree, its wallets the wallet directories under
it; ``[holders.KEY]`` in bip322.toml gives its name.  Their histories merge by txid: a transaction that is a send in one wallet and a
receive in another is one movement between them (kind ``transfer``), its fee
exact, and every coin knows its wallet.  The ledger goes to
``KEY/statements/<label>/`` in the data directory, named for the holder.
"""

from __future__ import annotations

from dataclasses import replace

from .history import Coin, History, Outpoint, WalletTx, chain_order

__all__ = ["merge_histories"]


def merge_histories(histories: dict[str, History]) -> History:
    """One history over the wallets, each coin tagged with its wallet, every transaction once.

    The tip is the lowest of the histories' tips, so a block that arrived
    between two reads is in none of it.  Pending transactions merge the same
    way, by txid.
    """
    if not histories:
        raise ValueError("no wallets to merge")
    chains = {h.chain for h in histories.values()}
    if len(chains) > 1:
        raise ValueError(f"the wallets are on different chains: {', '.join(sorted(chains))}")
    tip = min(histories.values(), key=lambda h: h.tip_height)
    parts: dict[str, dict] = {}
    for name, history in histories.items():
        for tx in history.txs + history.pending:
            if tx.height is not None and tx.height > tip.tip_height:
                continue
            part = parts.setdefault(tx.txid, {"txs": [], "ours_in": {}, "ours_out": {}, "outputs": {}, "payers": []})
            part["txs"].append(tx)
            for c in tx.ours_in:
                part["ours_in"].setdefault(c.outpoint, replace(c, wallet=name))
            for c in tx.ours_out:
                part["ours_out"].setdefault(c.outpoint, replace(c, wallet=name))
            for c in tx.ours_out + tx.others_out:
                part["outputs"].setdefault(c.outpoint, c)
            part["payers"] += [a for a in tx.payers if a not in part["payers"]]
    merged = [_merged(part) for part in parts.values()]
    confirmed = [tx for tx in merged if tx.height is not None]
    pending = [tx for tx in merged if tx.height is None]
    fetched = max(h.fetched_utc for h in histories.values())
    return History(tip.chain, None, tip.tip_height, tip.tip_hash, fetched, chain_order(confirmed), pending)


def _merged(part: dict) -> WalletTx:
    first: WalletTx = part["txs"][0]
    ours_in: list[Coin] = list(part["ours_in"].values())
    ours_out: list[Coin] = list(part["ours_out"].values())
    outputs: dict[Outpoint, Coin] = part["outputs"]
    others_out = [c for op, c in outputs.items() if op not in part["ours_out"]]
    counted = [len(t.ours_in) + t.others_in for t in part["txs"] if t.others_in is not None]  # the input count, from any wallet's view
    others_in = counted[0] - len(ours_in) if counted else None
    if others_in is None:  # histories saved before inputs were counted: a fee any wallet knew means every input was the holder's
        known_fee = next((t.fee_sat for t in part["txs"] if t.fee_sat is not None), None)
        others_in = 0 if known_fee is not None else None
    fee = sum(c.amount_sat for c in ours_in) - sum(c.amount_sat for c in outputs.values()) if others_in == 0 else None
    mine = {c.address for c in ours_in} | {c.address for c in ours_out}
    payers = [a for a in part["payers"] if a not in mine]  # another wallet of the holder paying is a transfer, not a payer
    return WalletTx(
        first.txid,
        first.height,
        first.blockhash,
        first.time,
        ours_in,
        ours_out,
        others_out,
        fee,
        others_in,
        first.blockindex,
        payers=payers,
    )
