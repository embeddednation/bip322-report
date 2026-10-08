"""A fake node wallet: a chain of blocks with regular times and whatever transactions a test adds.

Answers every RPC that bip322-report and bip322-audit use, from in-memory state,
so history, balances, snapshots, finalize (with spends) and verify all run
without a bitcoind.
"""

from __future__ import annotations

from bip322audit.rpc import BitcoinCli, RpcError, btc
from embit.script import address_to_scriptpubkey

T0 = 1_700_000_000  # block 0: 2023-11-14T22:13:20Z
SPACING = 600


def block_time(height: int) -> int:
    return T0 + height * SPACING


def fake_hash(height: int) -> str:
    return f"{height:064x}"


class FakeNode(BitcoinCli):
    def __init__(self, tip: int = 1000, wallet_name: str = "watch"):
        super().__init__(["bitcoin-cli", f"-rpcwallet={wallet_name}"])
        self.tip_height = tip
        self.wallet_name = wallet_name
        self.mine: set[str] = set()
        self.txs: dict[str, dict] = {}  # txid -> {height|None, conflicted, vin: [(txid, vout)], vout: [(address, sat)]}
        self.order: list[str] = []
        self.calls: list[tuple] = []
        self.hide_mempool_spent = False  # like a real node wallet: listunspent leaves out what an unconfirmed transaction spends

    # ---- test-side setup --------------------------------------------------- #

    def add(
        self,
        txid: str,
        height: int | None,
        vin: list[tuple[str, int]],
        vout: list[tuple[str | None, int]],
        conflicted: bool = False,
        in_mempool: bool = True,
        **fields,
    ) -> str:
        """``fields`` go into the gettransaction answer as they are (blockindex, walletconflicts, mempoolconflicts, details);
        an address of None makes an output without one; ``in_mempool`` is what getmempoolentry says of an unconfirmed transaction."""
        self.txs[txid] = {"height": height, "vin": list(vin), "vout": list(vout), "conflicted": conflicted}
        self.txs[txid].update(in_mempool=in_mempool, fields=fields)
        self.order.append(txid)
        return txid

    def _outputs(self) -> dict[tuple[str, int], tuple[str, int, int]]:
        out = {}
        for txid, tx in self.txs.items():
            if tx["height"] is None or tx["conflicted"]:
                continue
            for n, (address, sat) in enumerate(tx["vout"]):
                out[(txid, n)] = (address, sat, tx["height"])
        return out

    def _spent_at(self, height: int) -> set[tuple[str, int]]:
        return {
            op
            for tx in self.txs.values()
            if tx["height"] is not None and not tx["conflicted"] and tx["height"] <= height
            for op in tx["vin"]
        }

    def unspent(self, height: int | None = None, mine_only: bool = True) -> list[tuple[str, int, str, int, int]]:
        height = self.tip_height if height is None else height
        spent = self._spent_at(height)
        rows = []
        for (txid, n), (address, sat, h) in self._outputs().items():
            if h <= height and (txid, n) not in spent and (address in self.mine or not mine_only):
                rows.append((txid, n, address, sat, h))
        return rows

    def _spent_in_mempool(self) -> set[tuple[str, int]]:
        return {op for tx in self.txs.values() if tx["height"] is None and not tx["conflicted"] for op in tx["vin"]}

    # ---- the RPC surface --------------------------------------------------- #

    def _decoded(self, txid: str) -> dict:
        tx = self.txs[txid]
        return {
            "txid": txid,
            "vin": [{"txid": t, "vout": v} for t, v in tx["vin"]] or [{"coinbase": "00"}],
            "vout": [
                {"n": n, "value": btc(sat), "scriptPubKey": {"address": address} if address else {"type": "nulldata"}}
                for n, (address, sat) in enumerate(tx["vout"])
            ],
        }

    def call(self, method, *params):  # noqa: C901 - one branch per RPC
        self.calls.append((method, *params))
        if method == "getblockchaininfo":
            return {"chain": "main", "blocks": self.tip_height, "bestblockhash": fake_hash(self.tip_height)}
        if method == "getnetworkinfo":
            return {"subversion": "/Satoshi:31.1.0/"}
        if method == "getwalletinfo":
            return {"walletname": self.wallet_name}
        if method == "getblockhash":
            h = int(params[0])
            if not 0 <= h <= self.tip_height:
                raise RpcError("Block height out of range")
            return fake_hash(h)
        if method == "getblockheader":
            try:
                h = int(params[0], 16)
            except ValueError as exc:
                raise RpcError("Block not found") from exc
            if not 0 <= h <= self.tip_height or params[0] != fake_hash(h):
                raise RpcError("Block not found")
            return {"height": h, "hash": params[0], "time": block_time(h), "confirmations": self.tip_height - h + 1}
        if method == "getaddressinfo":
            return {"address": params[0], "ismine": params[0] in self.mine, "iswatchonly": False}
        if method == "listtransactions":
            count, skip = int(params[1]), int(params[2])
            rows = [{"txid": t, "category": "receive", "confirmations": self._confirmations(t)} for t in self.order]
            return rows[skip : skip + count]
        if method == "gettransaction":
            txid = params[0]
            if txid not in self.txs:
                raise RpcError("Invalid or non-wallet transaction id")
            tx = self.txs[txid]
            h = tx["height"]
            row = {
                "txid": txid,
                "confirmations": self._confirmations(txid),
                "time": block_time(h or self.tip_height),
                "decoded": self._decoded(txid),
            }
            if h is not None and not tx["conflicted"]:
                row.update({"blockheight": h, "blockhash": fake_hash(h), "blocktime": block_time(h)})
            row.update(tx.get("fields", {}))
            return row
        if method == "getmempoolentry":
            tx = self.txs.get(params[0])
            if not tx or tx["height"] is not None or tx["conflicted"] or not tx.get("in_mempool", True):
                raise RpcError("Transaction not in mempool")
            return {"height": self.tip_height}
        if method == "listunspent":
            minconf = int(params[0]) if params else 1
            return [
                {
                    "txid": t,
                    "vout": n,
                    "address": a,
                    "amount": btc(sat),
                    "confirmations": self.tip_height - h + 1,
                    "spendable": False,
                    "solvable": True,
                }
                for t, n, a, sat, h in self.unspent()
                if self.tip_height - h + 1 >= minconf and not (self.hide_mempool_spent and (t, n) in self._spent_in_mempool())
            ]
        if method == "scantxoutset":
            objects = params[1] if len(params) > 1 else []
            wanted = [o["desc"][5:-1] for o in objects if str(o.get("desc", "")).startswith("addr(")]
            unspents = []
            for t, n, a, sat, h in self.unspent(mine_only=False):
                if a not in wanted:
                    continue
                unspents.append(
                    {
                        "txid": t,
                        "vout": n,
                        "scriptPubKey": address_to_scriptpubkey(a).data.hex(),
                        "desc": f"addr({a})#00000000",
                        "amount": btc(sat),
                        "height": h,
                    }
                )
            return {"success": True, "height": self.tip_height, "bestblock": fake_hash(self.tip_height), "unspents": unspents}
        if method == "gettxout":
            txid, vout = params[0], int(params[1])
            include_mempool = params[2] if len(params) > 2 else True
            if self.hide_mempool_spent and include_mempool and (txid, vout) in self._spent_in_mempool():
                return None
            for t, n, a, sat, h in self.unspent(mine_only=False):
                if (t, n) == (txid, vout):
                    return {
                        "bestblock": fake_hash(self.tip_height),
                        "value": btc(sat),
                        "confirmations": self.tip_height - h + 1,
                        "scriptPubKey": {"address": a, "hex": address_to_scriptpubkey(a).data.hex()},
                    }
            return None
        if method == "getrawtransaction":
            txid = params[0]
            blockhash = params[2] if len(params) > 2 else None
            if blockhash is None:
                raise RpcError("No such mempool transaction. Use -txindex or provide a block hash")
            tx = self.txs.get(txid)
            if not tx or tx["height"] is None or fake_hash(tx["height"]) != blockhash:
                raise RpcError("No such transaction found in the provided block")
            return {**self._decoded(txid), "blockhash": blockhash}
        if method == "listsinceblock":
            since = int(params[0], 16)
            rows = [
                {"txid": t, "confirmations": self._confirmations(t), "blockhash": fake_hash(tx["height"]), "blockheight": tx["height"]}
                for t, tx in self.txs.items()
                if tx["height"] is not None and not tx["conflicted"] and tx["height"] > since
            ]
            return {"transactions": rows, "lastblock": fake_hash(self.tip_height)}
        raise RpcError(f"fake node: unsupported {method}")

    def _confirmations(self, txid: str) -> int:
        tx = self.txs[txid]
        if tx["conflicted"]:
            return -1
        if tx["height"] is None:
            return 0
        return self.tip_height - tx["height"] + 1
