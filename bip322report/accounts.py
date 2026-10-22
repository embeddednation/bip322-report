"""A custodial account's own record of what crossed into and out of the holder's wallets: the movements file.

``<holder>/<account>/movements.json``, one per account the holder keeps coins with (an
exchange, say), written by a tool of the holder's from the account's export or API
answer, which is kept beside it as the source and named in the file with its hash.
The statement reads it: each entry's txid is matched against the wallets' transactions,
which names the other side of a movement by the account's own record and checks the
amount against the chain.  An entry no wallet saw, and an amount that disagrees, are
exceptions, and the statement says so.  The file names no exchange; the directory does,
as the holder chose, and bip322-report reads the format and nothing else.

The format, version 1::

    {
      "movements": 1,
      "currency": "EUR",
      "source": {"name": "raw/crypto-transactions-2027-01-08.json", "sha256": "..."},
      "entries": [
        {"kind": "withdrawal", "time_utc": "2026-03-02T14:05:11Z", "txid": "...",
         "amount_sat": 10000000, "fee_sat": 5000, "address": "bc1q...", "reference": "W-12345"},
        {"kind": "deposit", "time_utc": "2026-06-10T09:00:00Z", "txid": "...",
         "amount_sat": 20000000, "address": "bc1q...", "reference": "D-67"}
      ]
    }

An entry is seen from the account's side: a ``withdrawal`` left the account for a wallet,
which received it; a ``deposit`` arrived at the account from a wallet, which sent it.
``amount_sat`` is what the chain moved: the output the wallet received, or the output it
paid the account.  ``fee_sat`` is the account's own fee when the record carries it,
``address`` the wallet's address for a withdrawal and the account's for a deposit,
``reference`` the record's own id; these three, ``currency`` and ``source`` may be left
out.  Keys beyond these are kept in the file and not read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from bip322audit.datadir import ACCOUNT_FILE
from bip322audit.rpc import btc

from .history import History, WalletTx
from .period import Period

__all__ = ["ACCOUNT_FILE", "FORMAT_VERSION", "KINDS", "Account", "AccountError", "Entry", "find_accounts", "match_accounts"]

FORMAT_VERSION = 1
KINDS = ("deposit", "withdrawal")
_HEX = set("0123456789abcdef")


class AccountError(ValueError):
    """A movements file that is not one: the path and what is wrong with it."""


@dataclass(frozen=True)
class Entry:
    kind: str  # deposit or withdrawal, from the account's side
    time_utc: str
    txid: str
    amount_sat: int
    fee_sat: int | None = None
    address: str | None = None
    reference: str | None = None

    @property
    def time(self) -> int:
        return int(datetime.fromisoformat(self.time_utc.replace("Z", "+00:00")).astimezone(UTC).timestamp())

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "time_utc": self.time_utc,
            "txid": self.txid,
            "amount_sat": self.amount_sat,
            "amount_btc": btc(self.amount_sat),
            "fee_sat": self.fee_sat,
            "address": self.address,
            "reference": self.reference,
        }


@dataclass
class Account:
    name: str  # the directory's name: what the holder calls the account
    path: Path
    entries: list[Entry] = field(default_factory=list)
    currency: str | None = None
    source: dict | None = None  # {"name", "sha256"}: the export or API answer the entries were translated from

    @classmethod
    def load(cls, path: Path, name: str) -> Account:
        """The movements file read and checked; :class:`AccountError` names the first thing wrong with it."""
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise AccountError(f"{path}: cannot be read as JSON ({exc})") from exc
        if not isinstance(data, dict) or data.get("movements") != FORMAT_VERSION:
            raise AccountError(f'{path}: not a movements file of version {FORMAT_VERSION} ("movements": {FORMAT_VERSION})')
        rows = data.get("entries")
        if not isinstance(rows, list):
            raise AccountError(f"{path}: 'entries' is missing or not a list")
        entries = [_entry(path, n, row) for n, row in enumerate(rows, 1)]
        currency = data.get("currency")
        source = data.get("source")
        if currency is not None and not isinstance(currency, str):
            raise AccountError(f"{path}: 'currency' is not a string")
        if source is not None and (not isinstance(source, dict) or not isinstance(source.get("name"), str)):
            raise AccountError(f"{path}: 'source' is not an object with a 'name'")
        return cls(name, path, entries, currency, source)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "file": str(self.path.name),
            "currency": self.currency,
            "source": self.source,
            "entries": len(self.entries),
        }


def _entry(path: Path, n: int, row) -> Entry:
    where = f"{path}: entry {n}"
    if not isinstance(row, dict):
        raise AccountError(f"{where} is not an object")
    kind = row.get("kind")
    if kind not in KINDS:
        raise AccountError(f"{where}: 'kind' must be one of {', '.join(KINDS)}, not {kind!r}")
    txid = row.get("txid")
    if not isinstance(txid, str) or len(txid) != 64 or set(txid.lower()) - _HEX:
        raise AccountError(f"{where}: 'txid' is not a transaction id (64 hex characters)")
    amount = row.get("amount_sat")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
        raise AccountError(f"{where}: 'amount_sat' is not a whole number of satoshi")
    fee = row.get("fee_sat")
    if fee is not None and (not isinstance(fee, int) or isinstance(fee, bool) or fee < 0):
        raise AccountError(f"{where}: 'fee_sat' is not a whole number of satoshi")
    when = row.get("time_utc")
    try:
        datetime.fromisoformat(str(when).replace("Z", "+00:00"))
    except ValueError as exc:
        raise AccountError(f"{where}: 'time_utc' is not an ISO 8601 instant ({when!r})") from exc
    for key in ("address", "reference"):
        if row.get(key) is not None and not isinstance(row[key], str):
            raise AccountError(f"{where}: '{key}' is not a string")
    return Entry(kind, str(when), txid.lower(), amount, fee, row.get("address"), row.get("reference"))


def find_accounts(holder: Path) -> list[Account]:
    """The accounts under a holder's directory, by name: every ``<holder>/<account>/movements.json``."""
    if not holder.is_dir():
        return []
    found = []
    for child in sorted(holder.iterdir()):
        if child.name.startswith(".") or child.name == "statements" or not (child / ACCOUNT_FILE).is_file():
            continue
        found.append(Account.load(child / ACCOUNT_FILE, child.name))
    return found


def match_accounts(accounts: list[Account], history: History, period: Period) -> dict:
    """Each account's entries against the wallets' transactions, for the statement.

    Returns ``by_txid``, what a matched transaction's other side is by the account's record (account, kind from the
    account's side, reference, amount, fee); ``accounts``, per account the entries in the period, how many a wallet
    transaction matches, the ``unmatched`` (dated in the period, seen by no wallet: coins that went somewhere else, or
    came from somewhere else) and the ``mismatched`` (a movement of the period whose amount on the chain is not the
    record's); and ``ok``, true when nothing is unmatched or mismatched.
    """
    txs: dict[str, WalletTx] = {tx.txid: tx for tx in [*history.txs, *history.pending]}
    by_txid: dict[str, dict] = {}
    rows = []
    for account in accounts:
        groups: dict[str, list[Entry]] = {}
        for e in account.entries:
            groups.setdefault(e.txid, []).append(e)
        in_period = matched = 0
        unmatched: list[dict] = []
        mismatched: list[dict] = []
        for txid, group in groups.items():
            tx = txs.get(txid)
            if tx is None:
                dated = [e for e in group if period.start.time < e.time <= period.end.time]
                in_period += len(dated)
                unmatched.extend(e.to_dict() for e in dated)
                continue
            moved = tx.height is not None and period.start.height < tx.height <= period.end.height
            if moved:
                in_period += len(group)
                matched += len(group)
                mismatched.extend(_disagreements(tx, group))
            by_txid[txid] = _endpoint(account.name, group)
        rows.append({**account.to_dict(), "in_period": in_period, "matched": matched, "unmatched": unmatched, "mismatched": mismatched})
    return {"by_txid": by_txid, "accounts": rows, "ok": all(not a["unmatched"] and not a["mismatched"] for a in rows)}


def _disagreements(tx: WalletTx, group: list[Entry]) -> list[dict]:
    """The record's amounts against the chain's: a withdrawal is what the wallets received in the transaction, a deposit what
    they paid the account's address (or paid others at all, when the record names no address)."""
    out = []
    withdrawals = [e for e in group if e.kind == "withdrawal"]
    if withdrawals:
        chain = sum(c.amount_sat for c in tx.ours_out)
        claimed = sum(e.amount_sat for e in withdrawals)
        if chain != claimed:
            out.extend({**e.to_dict(), "chain_sat": chain, "chain_btc": btc(chain), "claimed_sat": claimed} for e in withdrawals)
    for e in group:
        if e.kind != "deposit":
            continue
        chain = sum(c.amount_sat for c in tx.others_out if e.address is None or c.address == e.address)
        if chain != e.amount_sat:
            out.append({**e.to_dict(), "chain_sat": chain, "chain_btc": btc(chain), "claimed_sat": e.amount_sat})
    return out


def _endpoint(account: str, group: list[Entry]) -> dict:
    kinds = sorted({e.kind for e in group})
    fees = [e.fee_sat for e in group if e.fee_sat is not None]
    return {
        "account": account,
        "kind": kinds[0] if len(kinds) == 1 else "deposit, withdrawal",
        "reference": ", ".join(dict.fromkeys(e.reference for e in group if e.reference)) or None,
        "amount_sat": sum(e.amount_sat for e in group),
        "fee_sat": sum(fees) if fees else None,
        "entries": len(group),
    }
