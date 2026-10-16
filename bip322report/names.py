"""The other side of a movement, named.

``[addresses]`` in bip322.toml names the addresses of others, an exchange's
say.  The wallets of the tree name their own: an address its node says is
one of a wallet's is named after that wallet, so a move to or from another
wallet of the holder's, or of another holder's, needs no entry in the table.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from bip322audit.rpc import BitcoinCli, RpcError

from .history import WalletTx

__all__ = ["counterparts", "name_by_wallets"]


def counterparts(txs: Iterable[WalletTx]) -> set[str]:
    """The addresses on the other side of these movements: the payers of what came in, the payees of what went out."""
    found: set[str] = set()
    for tx in txs:
        found.update(tx.payers)
        found.update(c.address for c in tx.others_out if c.address)
    return found


def name_by_wallets(
    addresses: set[str], known: dict[str, str], wallets: dict[str, BitcoinCli], progress: Callable[[str], None] | None = None
) -> dict[str, str]:
    """``known`` plus, for each address not in it, the wallet whose node says the address is its own (``getaddressinfo``).

    A wallet that cannot be asked, not loaded or its node away, is skipped with a line to ``progress``: its addresses stay unnamed.
    """
    names = dict(known)
    unknown = sorted(a for a in addresses if a not in names)
    if unknown and wallets and progress:
        progress(f"asking {', '.join(wallets)} which of {len(unknown)} addresses are theirs")
    for wallet, cli in wallets.items():
        if not unknown:
            break
        try:
            for address in list(unknown):
                info = cli.call("getaddressinfo", address)
                if isinstance(info, dict) and (info.get("ismine") or info.get("iswatchonly")):
                    names[address] = wallet
                    unknown.remove(address)
        except RpcError as exc:
            if progress:
                progress(f"{wallet}: {exc}; its addresses stay unnamed")
    return names
