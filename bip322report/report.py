"""Assemble the report: balances, movements, reconciliation, proof coverage."""

from __future__ import annotations

import hashlib
import json
import shlex
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from bip322audit.holdings import format_holdings, holdings, holdings_command
from bip322audit.rpc import BitcoinCli, RpcError, btc
from bip322core.core import BIP322Error
from bip322core.engines import EngineError, available_engines
from bip322core.report import describe_address, format_verify_text
from bip322core.verify import verify_message

from . import TOOL
from .accounts import Account, match_accounts
from .coverage import cover_coins, load_proofs_files, pending_snapshots
from .fiat import Rates
from .history import Coin, History, WalletTx, WriteOff
from .period import Period


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_report(
    history: History,
    period: Period,
    *,
    label: str,
    proofs_roots=(),
    cli: BitcoinCli | None = None,
    rates: Rates | None = None,
    engines=None,
    progress=None,
    onchain: bool = True,
    dust_sat: int = 0,
    addresses: dict[str, str] | None = None,
    wallets: Sequence[str] | None = None,
    sources: dict[str, dict] | None = None,
    node_check: bool = True,
    node_clis: Mapping[str, BitcoinCli] | None = None,
    accounts: Sequence[Account] | None = None,
    written_off: Sequence[WriteOff] = (),
) -> dict:
    """The report as a plain dict (what report.json holds and the templates render).

    ``written_off`` are the coins the holder has declared lost (:meth:`History.written_off`): each leaves the books at its block
    by a row of kind ``written_off``, is held no more from then on and needs no proof; the period that holds the row lists it
    under ``written_off`` and in the summary.

    ``accounts`` are the holder's custodial accounts' own records (:mod:`bip322report.accounts`): a movement whose txid an
    account recorded is named after the account, with the record's reference, and its amount is checked against the
    record; an entry of the period no wallet saw, or an amount that disagrees, fails the ``endpoints`` check.

    ``addresses`` is ``[addresses]`` of ``bip322.toml``: the wallet each address of
    someone else belongs to, which names the other side of a movement (``counterpart``).
    ``wallets`` names a holder's wallets when ``history`` is their merged one
    (``holder.merge_histories``); one with no movement in the period and
    nothing held at either end is listed under ``idle_wallets`` and left out
    of ``wallets``, unless every wallet is so.  Each movement then names the wallet of the
    holder's it touched (``wallet``) beside the other side (``counterpart``),
    and the report carries each wallet's opening and closing balance, what it
    received and sent (a move between the holder's wallets is sent by one and
    received by the other, the fee with the sender), paid in fees, and the
    movements as that wallet sees them under ``wallets``, oldest wallet
    first, with where its history came from when ``sources`` says (node, or the file
    kept for a wallet the node has not loaded).  ``node_clis``, for a holder, is each loaded wallet's node wallet: the
    listunspent cross-check then runs per wallet over its own closing coins, merged into one ``node_check``
    (:func:`_node_checks`).  ``node_check`` False leaves out the listunspent cross-check,
    which is per node wallet.

    ``cli`` is needed to re-verify the proofs files under ``proofs_roots`` against the chain and to
    cross-check the closing balance with ``listunspent`` while the closing
    coins are the present ones; without it the proofs are verified for their signatures only.
    Outputs of at most ``dust_sat`` satoshi that others paid to the wallet
    are dust and left out of every figure, as if they had never been the
    wallet's (``History.without_dust``; the wallet's own change is never
    dust); those held at the closing block are listed in the report's
    ``dust`` block for the record.  Spending them would cost more than they
    hold, so a holder leaves them; and it is control that is proven, not its
    absence.  Dust the wallet does spend is booked in the block that spends
    it, as a movement of kind ``swept`` (``dust.swept_count``), so an
    opening balance always equals the closing balance of the statement before.
    """
    if dust_sat < 0:
        raise ValueError(f"the dust threshold cannot be negative: {dust_sat}")
    dust = history.dust_at(dust_sat, period.end.height)
    created = {tx.txid: tx.iso_time for tx in history.txs}  # when each coin was received: the time of its creating transaction's block
    history = history.without_dust(dust_sat).written_off(list(written_off))
    # written off by the closing block: still in the node wallet's listunspent, no longer a holding; left out of the node check as dust is
    lost = [tx.ours_in[0] for tx in history.txs if tx.kind == "written_off" and tx.height is not None and tx.height <= period.end.height]
    opening = history.coins_at(period.start.height)
    closing = history.coins_at(period.end.height)
    movements = history.between(period.start.height, period.end.height)
    opening_sat = sum(c.amount_sat for c in opening)
    closing_sat = sum(c.amount_sat for c in closing)

    files = load_proofs_files(cli, proofs_roots, engines=engines, progress=progress) if proofs_roots else []
    closing_rows, used = _closing_rows(history, closing, files, created, period)
    closing_addresses = _by_address(closing_rows)
    _verify_addresses(closing_addresses, engines)
    scripts = {a["address"]: a["script"]["script_pubkey"] for a in closing_addresses}
    onchain_result = _onchain(cli, closing_rows, period, scripts, progress) if cli is not None and onchain and closing_rows else None
    order = _wallet_order(history, wallets) if wallets is not None else None
    endpoints = match_accounts(list(accounts), history, period) if accounts else None
    by_txid = endpoints["by_txid"] if endpoints else {}
    tx_rows, totals = _movement_rows(movements, opening_sat, rates, addresses or {}, label, holder=wallets is not None, endpoints=by_txid)
    net_sat = totals["net_sat"]

    report = {
        "tool": TOOL,
        "generated_utc": _now(),
        "label": label,
        "chain": history.chain,
        "history_fetched_utc": history.fetched_utc,
        "period": period.to_dict(),
        "opening": {
            "height": period.start.height,
            "total_sat": opening_sat,
            "total_btc": btc(opening_sat),
            "coins": [{**c.to_dict(), "created_utc": created.get(c.txid)} for c in opening],
        },
        "closing": {
            "height": period.end.height,
            "total_sat": closing_sat,
            "total_btc": btc(closing_sat),
            "coins": closing_rows,
            "addresses": closing_addresses,
        },
        "transactions": tx_rows,
        "totals": totals,
        "reconciliation": {
            "opening_sat": opening_sat,
            "net_sat": net_sat,
            "closing_sat": closing_sat,
            "diff_sat": closing_sat - opening_sat - net_sat,
            "ok": closing_sat == opening_sat + net_sat,
        },
        "dust": _dust_block(dust, dust_sat, movements, created),
        "written_off": [
            {
                **tx.ours_in[0].to_dict(),
                "created_utc": created.get(tx.ours_in[0].txid),
                "height": tx.height,
                "time_utc": tx.iso_time,
                "reason": tx.reason,
            }
            for tx in movements
            if tx.kind == "written_off"
        ],
        "coverage": _coverage(closing_rows, closing_sat, period.end.height),
        "proofs_files": [
            {**f.to_dict(), "timing": _timing(int(f.stamp["height"]), period.end.height), "used_for": used[f.path]}
            for f in files
            if f.path in used
        ],
        "proofs_files_not_used": sum(
            1 for f in files if f.path not in used
        ),  # a bare count: what else the wallet's snapshots hold is not the statement's business
        "policy": _single({f.document.get("policy") for f in files if f.path in used} - {None}),
        "pending_snapshots": [p.name for p in pending_snapshots(proofs_roots, {c.address for c in closing})],
        "pending_transactions": [tx.to_dict() for tx in history.pending],
        "endpoints": {k: v for k, v in endpoints.items() if k != "by_txid"} if endpoints else None,
        "unnamed_movements": sum(1 for r in tx_rows if r["kind"] in ("receive", "send", "mixed", "swept") and not r["counterpart"]),
        "onchain": onchain_result,
        "fiat": (
            {
                **rates.to_dict(),
                "opening": _valued(rates, opening_sat, period.start.time),
                "closing": _valued(rates, closing_sat, period.end.time),
            }
            if rates
            else None
        ),
        "node_check": (
            _node_checks(node_clis, history, period, closing, dust + lost)
            if node_clis is not None  # a holder: per loaded wallet; none loaded means no check, not the chain-level node's
            else _node_check(cli, history, period, closing, dust + lost)
            if cli is not None and node_check
            else None
        ),
    }
    blocks = (
        [_wallet_block(w, opening, closing, movements, rates, addresses or {}, (sources or {}).get(w), by_txid) for w in order]
        if order is not None
        else None
    )
    # a wallet with nothing in the period, no movement and nothing held at either end, is named in the notes and shown nowhere
    # else, so long as some wallet of the holder's has something; with nothing anywhere, every wallet stays, there is nothing to hide
    idle = [b["name"] for b in blocks or [] if not b["transactions"] and not b["opening_sat"] and not b["closing_sat"]]
    report["idle_wallets"] = idle if blocks and len(idle) < len(blocks) else []
    report["wallets"] = [b for b in blocks if b["name"] not in report["idle_wallets"]] if blocks is not None else None
    report["wallets_totals"] = _wallets_totals(report["wallets"]) if report["wallets"] else None
    report["previous"] = None  # the statement before this one, when the tree has it (previous_statement); not a fact of this one
    report["report_id"] = _report_id(report)
    report["ok"] = _verdict(report)
    return report


def _closing_rows(history: History, closing: list[Coin], files: list, created: dict, period: Period) -> tuple[list[dict], dict[Path, int]]:
    """The closing coins as rows, each with the best proof there is for its address; and, per proofs file, the rows citing it."""
    covers = cover_coins(closing, files, period.end.height)
    spenders: dict[tuple, WalletTx] = {}  # outpoint -> the confirmed transaction that spends it: one pass, not History.spender_of per coin
    for tx in history.txs:
        for c in tx.ours_in:
            spenders.setdefault(c.outpoint, tx)
    used: dict[Path, int] = {}  # the proofs files the statement relies on
    rows = []
    for coin in closing:
        best = covers[coin.outpoint][0] if covers[coin.outpoint] else None
        proof = best.to_dict() if best else None
        if best:
            used[best.proofs.path] = used.get(best.proofs.path, 0) + 1
        rows.append(
            {
                **coin.to_dict(),
                "created_utc": created.get(coin.txid),
                "proof": proof,
                "onchain": None,  # the lookup at the block of the proof, when a node is at hand (_onchain)
                "spent_by": _spent_by(spenders.get(coin.outpoint)),
            }
        )
    return rows, used


def _verify_addresses(addresses: list[dict], engines) -> None:
    """Open each closing address into its script and verify its proof against that script.

    An address has one proof on the statement, its first output's, and one
    verdict: every output citing that proof carries it too, so an address
    counts as covered exactly when its outputs do.
    """
    for a in addresses:
        a["script"] = _address_step(a["address"])
        if not a["proof"]:
            continue
        _verify_by_script(a, engines)
        cited = (a["proof"]["file"], a["proof"]["signature"])
        for o in a["outputs"]:
            if o["proof"] and (o["proof"]["file"], o["proof"]["signature"]) == cited:
                o["proof"]["verified"] = a["proof"]["verified"]


def _fiat(rates: Rates, tx: WalletTx, amount: int) -> dict:
    """A movement valued at the rate in force on its day."""
    rate = rates.rate_on(tx.time)
    return {
        "currency": rates.currency,
        "rate": str(rate) if rate is not None else None,
        "amount": rates.value(amount, tx.time),
        "net": rates.value(tx.net_sat, tx.time),
        "fee": rates.value(tx.fee_sat, tx.time) if tx.fee_sat is not None else None,
    }


def _valued(rates: Rates, sat: int, when: int) -> dict:
    """A holding valued at the rate in force on a day: the opening or closing holdings at the period's edge."""
    rate = rates.rate_on(when)
    return {"rate": str(rate) if rate is not None else None, "value": rates.value(sat, when)}


def _wallet_order(history: History, wallets: Sequence[str]) -> list[str]:
    """The holder's wallets oldest first, by their first transaction in the history; one with none last, by name."""
    first: dict[str, int] = {}
    for tx in history.txs:  # chain order
        for w in sorted({c.wallet for c in tx.ours_in + tx.ours_out if c.wallet}):
            first.setdefault(w, len(first))
    return sorted(wallets, key=lambda w: (first.get(w, len(first)), w))


def _wallet_block(
    wallet: str,
    opening: list[Coin],
    closing: list[Coin],
    movements: list[WalletTx],
    rates: Rates | None,
    names: dict[str, str],
    source: dict | None,
    endpoints: Mapping[str, dict] | None = None,
) -> dict:
    """One wallet of a holder: its opening and closing balance, what it received and sent (from and to others, and from and
    to the holder's other wallets: ``moved_in`` and ``moved_out`` say how much of each), paid in fees and netted in joint
    transactions, the sum of its rows' amounts, the period's movements as it sees them, and its history's source.
    Opening + received - sent - fees + mixed = closing, for each wallet."""
    o = sum(c.amount_sat for c in opening if c.wallet == wallet)
    s = sum(c.amount_sat for c in closing if c.wallet == wallet)
    rows, sums = _wallet_rows(movements, wallet, o, rates, names, endpoints or {})
    block = {"name": wallet, "opening_sat": o, "opening_btc": btc(o)}
    sums["amount"] = sums["received"] - sums["sent"] + sums["mixed"] - sums["written_off"]
    for key, sat in sums.items():
        block[f"{key}_sat"], block[f"{key}_btc"] = sat, btc(sat)
    return {**block, "closing_sat": s, "closing_btc": btc(s), "transactions": rows, "history": source}


SUMS = ("received", "sent", "fees", "mixed", "moved_in", "moved_out", "written_off")


def _wallets_totals(blocks: list[dict]) -> dict:
    """The holder's wallets added up.  Received and sent include what moved between the wallets (``moved_in``, which equals
    ``moved_out``), sent by one and received by another, so they exceed the statement's own received and sent by that much."""
    totals = {}
    for key in ("opening", *SUMS, "amount", "closing"):
        sat = sum(b[f"{key}_sat"] for b in blocks)
        totals[f"{key}_sat"], totals[f"{key}_btc"] = sat, btc(sat)
    return totals


def _share(total: int, weights: dict[str, int], who: str) -> int:
    """``who``'s part of ``total`` pro rata to the weights (what each wallet put in), in whole satoshi; the remainder goes to the
    largest weight, so the parts add up to the total exactly."""
    whole = sum(weights.values())
    if who not in weights:
        return 0
    if len(weights) == 1 or not whole:
        return total
    parts = {w: total * v // whole for w, v in weights.items()}
    parts[max(weights, key=lambda w: (weights[w], w))] += total - sum(parts.values())
    return parts[who]


def _wallet_share(tx: WalletTx, wallet: str, got: int, net: int) -> tuple[int, int | None, dict[str, int]]:
    """One of the holder's wallets' side of a movement: its amount (what moved between it and the other side, before the
    fee; negative when it left), its fee (None when it paid none, or it is unknown) and what the movement adds to the
    wallet's sums (SUMS).

    A wallet that only received books what arrived as received, and as
    ``moved_in`` too when another wallet of the holder's paid.  A wallet that
    spent books what left to others and the fee, pro rata to what it put in
    when several of the holder's wallets did; the rest of its amount moved
    from or to the holder's other wallets (``moved_in``/``moved_out``, inside
    received/sent), so that a move between two wallets is sent by one and
    received by the other for the same amount, the fee with the sender.  In a
    joint transaction (others' inputs beside the holder's) the fee is unknown,
    and every wallet in it books its net as mixed.
    """
    if tx.mixed:
        return net, None, {"mixed": net}
    if tx.kind == "written_off":
        return net, None, {"written_off": -net}  # the coin leaves the books: its amount, negative, nothing paid to anyone
    if tx.kind == "swept" or not any(c.wallet == wallet for c in tx.ours_in):
        from_wallets = bool(tx.ours_in) and tx.kind != "swept"
        return got, None, {"received": got, **({"moved_in": got} if from_wallets else {})}  # no fee of its own: the payer's
    spenders: dict[str, int] = {}
    for c in tx.ours_in:
        spenders[c.wallet or ""] = spenders.get(c.wallet or "", 0) + c.amount_sat
    sent = _share(sum(c.amount_sat for c in tx.others_out), spenders, wallet)
    fee = _share(tx.fee_sat or 0, spenders, wallet)
    amount = net + fee
    between = amount + sent  # what is not explained by others: moved from (positive) or to (negative) the holder's other wallets
    sums = {"sent": sent, "fees": fee}
    if between > 0:
        sums.update(received=between, moved_in=between)
    elif between < 0:
        sums.update(sent=sent - between, moved_out=-between)
    return amount, fee, sums


def _wallet_rows(
    movements: list[WalletTx],
    wallet: str,
    opening_sat: int,
    rates: Rates | None,
    names: dict[str, str],
    endpoints: Mapping[str, dict] | None = None,
) -> tuple[list[dict], dict[str, int]]:
    """A holder's movements as one of its wallets sees them: the wallet's net (fee inside), a running balance of its own, and
    the other side plain: another wallet of the holder's, or what ``[addresses]`` names.  A move between two of the holder's
    wallets is a send on one side and a receive on the other for the same amount (``amount_sat``, before the fee, which the
    sender's row carries) under the same txid, as in each wallet's own statement.  With the rows, the wallet's sums by SUMS
    (:func:`_wallet_share`)."""
    rows = []
    running = opening_sat
    sums = dict.fromkeys(SUMS, 0)
    for tx in movements:
        spent = sum(c.amount_sat for c in tx.ours_in if c.wallet == wallet)
        got = sum(c.amount_sat for c in tx.ours_out if c.wallet == wallet)
        if not any(c.wallet == wallet for c in tx.ours_in + tx.ours_out):
            continue
        net = got - spent
        running += net
        amount, fee, share = _wallet_share(tx, wallet, got, net)
        for key, sat in share.items():
            sums[key] += sat
        kind, counterpart = _wallet_side(tx, wallet, any(c.wallet == wallet for c in tx.ours_in), net, names)
        endpoint = (endpoints or {}).get(tx.txid)
        if endpoint and kind in ("receive", "send", "mixed"):
            counterpart = _by_record(counterpart, endpoint["account"], kind)
        row = {
            "txid": tx.txid,
            "height": tx.height,
            "time": tx.time,
            "time_utc": tx.iso_time,
            "kind": kind,
            "amount_sat": amount,
            "amount_btc": btc(amount),
            "fee_sat": fee,
            "fee_btc": btc(fee) if fee is not None else None,
            "net_sat": net,
            "net_btc": btc(net),
            "balance_after_sat": running,
            "balance_after_btc": btc(running),
            "counterpart": counterpart,
            "endpoint": endpoint,
            "reason": tx.reason,
        }
        if rates:
            rate = rates.rate_on(tx.time)
            row["fiat"] = {
                "currency": rates.currency,
                "rate": str(rate) if rate is not None else None,
                "amount": rates.value(amount, tx.time),
                "net": rates.value(net, tx.time),
                "fee": rates.value(fee, tx.time) if fee is not None else None,
            }
        rows.append(row)
    return rows, sums


def _wallet_side(tx: WalletTx, wallet: str, spent: bool, net: int, names: dict[str, str]) -> tuple[str, str]:
    """The kind of a holder's movement for one of its wallets, and the other side as that wallet sees it."""
    others = sorted({c.wallet for c in tx.ours_in + tx.ours_out if c.wallet and c.wallet != wallet})  # the holder's, involved
    theirs = _named(names, [c.address for c in tx.others_out if c.address])
    if tx.kind == "swept":
        return "swept", ", ".join(_named(names, tx.payers))
    if tx.kind == "written_off":
        return "written_off", ""  # a loss, not a payment: no other side
    if not spent:  # paid by another wallet of the holder's, or by others
        return "receive", ", ".join(sorted({c.wallet for c in tx.ours_in if c.wallet}) if tx.ours_in else _named(names, tx.payers))
    if tx.mixed:
        return "mixed", ", ".join(dict.fromkeys(theirs + _named(names, tx.payers) + others))  # a wallet on both sides, named once
    if not tx.others_out:  # within the holder: its own wallet, or another of the holder's on the other side
        return ("internal", wallet) if not others else ("receive" if net > 0 else "send", ", ".join(others))
    return "send", ", ".join(dict.fromkeys(theirs + others))


def _named(names: dict[str, str], addresses) -> list[str]:
    """The wallets ``[addresses]`` names for these addresses, each once, in order."""
    found: list[str] = []
    for address in addresses:
        name = names.get(address)
        if name is not None and name not in found:
            found.append(name)
    return found


def _sides(tx: WalletTx, names: dict[str, str]) -> tuple[str, str]:
    """A holder's movement from the holder's side: the wallet of the holder's it touched, and the other side as ``[addresses]``
    names it ("" when it names none).  A transfer reads ``treasury -> cold`` and has no other side; a move within one wallet is
    that wallet."""
    wallets_in = sorted({c.wallet for c in tx.ours_in if c.wallet})
    wallets_out = sorted({c.wallet for c in tx.ours_out if c.wallet})
    if tx.kind in ("receive", "swept"):
        return ", ".join(wallets_out), ", ".join(_named(names, tx.payers))
    if tx.kind in ("send", "mixed"):
        theirs = [c.address for c in tx.others_out if c.address] + (list(tx.payers) if tx.kind == "mixed" else [])
        return ", ".join(wallets_in), ", ".join(_named(names, theirs))
    if tx.kind == "transfer":
        source = [w for w in wallets_in if w not in wallets_out] or wallets_in
        target = [w for w in wallets_out if w not in wallets_in] or wallets_out
        return f"{', '.join(source)} -> {', '.join(target)}", ""
    return ", ".join(wallets_in or wallets_out), ""


def _counterpart(tx: WalletTx, names: dict[str, str], own: str) -> str:
    """The wallet on the other side: the wallet itself for an internal move, else the names ``[addresses]`` gives the
    addresses there (paid by a send, paying a receive; both for a mixed one), "" when it names none."""
    if tx.kind == "internal":
        return own
    theirs = [c.address for c in tx.others_out if c.address] if tx.kind in ("send", "mixed") else []
    theirs += tx.payers if tx.kind in ("receive", "swept", "mixed") else []
    found: list[str] = []
    for address in theirs:
        name = names.get(address)
        if name is not None and name not in found:
            found.append(name)
    return ", ".join(found)


def _by_record(counterpart: str, account: str, kind: str) -> str:
    """The other side of a movement an account's record matches: the account, which is authoritative for its own
    transactions; a joint transaction keeps whoever else is named beside it."""
    others = [n for n in counterpart.split(", ") if n and n != account] if kind == "mixed" else []
    return ", ".join([account, *others])


def _movement_rows(
    movements: list[WalletTx],
    opening_sat: int,
    rates: Rates | None,
    names: dict[str, str],
    own: str,
    holder: bool = False,
    endpoints: Mapping[str, dict] | None = None,
) -> tuple[list[dict], dict]:
    """The period's transactions as rows with a running balance and the other side named, and the totals of the period:
    received from others, sent to others, fees, the net of joint transactions, each in fiat too when rates were given
    (``totals["fiat"]``; None for a sum some rate was missing for).  For a holder (``holder``) each row also names the
    wallet of the holder's it touched."""
    rows = []
    received = sent = fees = mixed = mixed_count = lost = lost_count = 0
    valued = dict.fromkeys(("received", "sent", "fees", "mixed", "written_off"), Decimal(0))
    unvalued = False
    running = opening_sat
    for tx in movements:
        row = tx.to_dict()
        running += tx.net_sat
        row["balance_after_sat"] = running
        row["balance_after_btc"] = btc(running)
        if holder:
            row["wallet"], row["counterpart"] = _sides(tx, names)
        else:
            row["counterpart"] = _counterpart(tx, names, own)
        row["endpoint"] = (endpoints or {}).get(tx.txid)
        if row["endpoint"] and tx.kind in ("receive", "send", "mixed"):
            row["counterpart"] = _by_record(row["counterpart"], row["endpoint"]["account"], tx.kind)
        # what moved between the wallet and others, before the fee: received, or (sent).  A mixed transaction's fee is
        # unknown (others funded it too), so its amount is the wallet's net and no fee is stated for it.
        amount = tx.net_sat + (tx.fee_sat or 0)
        row["amount_sat"] = amount
        row["amount_btc"] = btc(amount)
        if tx.kind in ("receive", "swept"):
            row["others_out"] = []  # a payer's own change is not the wallet's business
        if rates:
            row["fiat"] = _fiat(rates, tx, amount)
            if row["fiat"]["rate"] is None:
                unvalued = True
        rows.append(row)
        key = {"receive": "received", "swept": "received", "send": "sent", "mixed": "mixed", "written_off": "written_off"}.get(
            tx.kind
        )  # swept: dust the wallet spent, booked where it was spent; written_off: a coin declared lost, leaving the books
        sign = -1 if key in ("sent", "written_off") else 1
        if key == "received":
            received += amount
        elif key == "sent":
            sent += -amount
        elif key == "mixed":
            mixed += amount
            mixed_count += 1
        elif key == "written_off":
            lost += -amount
            lost_count += 1
        if key and rates and row["fiat"]["amount"] is not None:
            valued[key] += sign * Decimal(row["fiat"]["amount"])
        if tx.fee_sat:
            fees += tx.fee_sat
            if rates and row["fiat"]["fee"] is not None:
                valued["fees"] += Decimal(row["fiat"]["fee"])
    amount_sat = received - sent + mixed - lost  # the sum of the rows' amounts: an internal move's is zero
    net_sat = running - opening_sat
    totals = {
        "received_sat": received,
        "received_btc": btc(received),
        "sent_sat": sent,
        "sent_btc": btc(sent),
        "mixed_sat": mixed,
        "mixed_btc": btc(mixed),
        "mixed_count": mixed_count,
        "written_off_sat": lost,
        "written_off_btc": btc(lost),
        "written_off_count": lost_count,
        "amount_sat": amount_sat,
        "amount_btc": btc(amount_sat),
        "fees_sat": fees,
        "fees_btc": btc(fees),
        "net_sat": net_sat,
        "net_btc": btc(net_sat),
        "transactions": len(rows),
        "fiat": {k: (None if unvalued else str(v)) for k, v in valued.items()} if rates else None,
    }
    return rows, totals


def _dust_block(dust: list[Coin], threshold_sat: int, movements: list[WalletTx], created: dict) -> dict:
    """The dust left out of every figure: what the wallet's addresses hold of it at the closing block, and what the period swept."""
    total_sat = sum(c.amount_sat for c in dust)
    swept = [tx.net_sat for tx in movements if tx.kind == "swept"]
    return {
        "threshold_sat": threshold_sat,
        "count": len(dust),
        "total_sat": total_sat,
        "total_btc": btc(total_sat),
        "swept_count": len(swept),
        "swept_sat": sum(swept),
        "swept_btc": btc(sum(swept)),
        "coins": [{**c.to_dict(), "created_utc": created.get(c.txid), "script_pubkey": _script_of(c.address)} for c in dust],
    }


def _timing(height: int, closing_height: int) -> str:
    """Where a proof's block lies against the closing block: ``at``, ``after`` or ``before``."""
    return "at" if height == closing_height else "after" if height > closing_height else "before"


def _covered(row: dict) -> bool:
    """A verified proof stamped at or after the closing block: control at the period's end."""
    return bool(row["proof"] and row["proof"]["verified"] and row["proof"]["timing"] in ("at", "after"))


def _coverage(rows: list[dict], closing_sat: int, closing_height: int) -> dict:
    """How much of the closing balance a verified proof at or after the closing block backs, by UTXO; and what the rest lacks:
    a proof before the closing block only (``before_count``), or none that verifies (``missing_count``)."""
    uncovered = [r for r in rows if not _covered(r)]
    covered_sat = closing_sat - sum(r["amount_sat"] for r in uncovered)
    before = [r for r in uncovered if r["proof"] and r["proof"]["verified"]]  # verified, but stamped before the closing block
    return {
        "closing_height": closing_height,
        "covered_sat": covered_sat,
        "covered_btc": btc(covered_sat),
        "uncovered_sat": closing_sat - covered_sat,
        "uncovered_btc": btc(closing_sat - covered_sat),
        "covered_count": len(rows) - len(uncovered),
        "at_closing_count": sum(1 for r in rows if _covered(r) and r["proof"]["timing"] == "at"),
        "before_count": len(before),
        "missing_count": len(uncovered) - len(before),
        "total_count": len(rows),
        "complete": not uncovered,
        "uncovered": [
            {
                **{k: r[k] for k in ("txid", "vout", "address", "amount_sat", "amount_btc")},
                "wallet": r.get("wallet"),
                "proof_height": int(r["proof"]["stamp"]["height"]) if r in before else None,
            }
            for r in uncovered
        ],
    }


def _verdict(report: dict) -> bool:
    """OK when the balances reconcile, every closing UTXO has a verified proof, and neither the node nor the chain contradicts the statement."""
    node, chain = report["node_check"], report["onchain"]
    node_ok = node is None or node.get("ok") is not False  # None: could not be checked, not a failure
    chain_ok = chain is None or chain["contradicted"] == 0  # a lookup that could not run excuses none that did
    ends_ok = report.get("endpoints") is None or report["endpoints"]["ok"]  # the accounts' records agree with the movements
    return bool(report["reconciliation"]["ok"] and report["coverage"]["complete"] and node_ok and chain_ok and ends_ok)


WIDTH = 84  # characters per printed command line: fits an A4 page in the statement's monospace size, and a mainnet block line


def _pieces(word: str) -> list[str]:
    """One word as shell text in pieces of at most WIDTH characters, each quoted by itself.

    Printed on lines of their own with a backslash-newline between them, the
    pieces are one word again: bash removes that pair between quoted strings,
    but keeps it inside single quotes.  A piece also ends after a newline in
    the word, so that no printed line starts with the word's own text, which
    an interactive bash would search for history characters first.
    """
    quoted = shlex.quote(word)
    if len(quoted) <= WIDTH and "\n" not in word:
        return [quoted]
    if quoted == word:  # nothing in it needs quoting: cut anywhere
        return [word[i : i + WIDTH] for i in range(0, len(word), WIDTH)]
    pieces: list[str] = []
    current = ""
    for ch in word:
        if current and (current.endswith("\n") or len(shlex.quote(current + ch)) > WIDTH):
            pieces.append(shlex.quote(current))
            current = ""
        current += ch
    return [*pieces, shlex.quote(current)]


def _wrapped(words: list[str]) -> list[str]:
    """Words on lines of at most WIDTH characters, every line but the last ending in its ``\\`` continuation (long words split too)."""
    lines: list[str] = []
    current = ""
    for *head, word in (_pieces(w) for w in words):
        if head:
            if current:
                lines.append(current + " \\")
            lines.extend(p + "\\" for p in head)
            current = word
        elif current and len(current) + 1 + len(word) > WIDTH:
            lines.append(current + " \\")
            current = word
        else:
            current = f"{current} {word}" if current else word
    return [*lines, current]


def shell_words(argv: list[str]) -> str:
    """A command of plain words, printed over lines of at most WIDTH characters with ``\\`` continuations (long words split too)."""
    return "\n".join(_wrapped(argv))


def _split_atoms(atoms: list[str]) -> list[str]:
    """Lines of at most WIDTH characters; an atom (a character, or the several that stand for one) is never cut."""
    chunks: list[str] = []
    while sum(map(len, atoms)) > WIDTH:
        size = fit = space = 0
        for n, atom in enumerate(atoms, 1):
            size += len(atom)
            if size > WIDTH + 1:
                break
            if size <= WIDTH:
                fit = n
            if (
                atom == " " and size > WIDTH // 2
            ):  # a line of the message: break after a space, so the block line keeps its hash and time whole
                space = n
        cut = space or fit
        chunks.append("".join(atoms[:cut]))
        atoms = atoms[cut:]
        if atoms and atoms[0] == "^":
            atoms = ['""^', *atoms[1:]]
    return [*chunks, "".join(atoms)]


def shell_command(argv: list[str]) -> str:
    """A command a reader pastes into a shell, printed over several lines with ``\\`` continuations.

    Bash removes a backslash-newline pair anywhere outside single quotes, so a
    long token can be split across lines too, as long as the continuation
    line is not indented.  The last argument (the message) is double quoted
    with the characters that matter to the shell escaped; the newline inside
    the message stays a real newline, which double quotes allow.  An
    interactive bash expands history before it reads quotes, and double
    quotes do not stop it: a ``!`` is therefore put outside them as ``\\!``,
    and a line that would start with ``^`` starts with an empty ``""``.
    What the shell reassembles is exactly ``argv``.
    """
    *lines, current = _wrapped(argv[:-1])
    if current:
        lines.append(current + " \\")
    escapes = {"\\": "\\\\", '"': '\\"', "$": "\\$", "`": "\\`", "!": '"\\!"'}
    raws = argv[-1].split("\n")
    body: list[str] = []
    for n, raw in enumerate(raws):
        atoms = [escapes.get(ch, ch) for ch in raw]
        if n == 0:
            atoms.insert(0, '"')
        elif atoms and atoms[0] == "^":
            atoms[0] = '""^'
        if n == len(raws) - 1:
            atoms.append('"')
        *head, tail = _split_atoms(atoms)
        body.extend(c + "\\" for c in head)
        body.append(tail)
    return "\n".join(lines + body)


def _describe(address: str) -> dict | None:
    """What an address encodes (its scriptPubKey among it); None when the address cannot be decoded."""
    try:
        return describe_address(address)
    except (BIP322Error, ValueError):
        return None


def _script_of(address: str) -> str | None:
    """The scriptPubKey an address encodes, as hex; None when the address cannot be decoded."""
    return (_describe(address) or {}).get("scriptPubKey")


def _address_step(address: str) -> dict:
    """The address opened into its scriptPubKey: the script the proof is for and the outputs are locked to."""
    info = _describe(address) or {}
    return {"script_pubkey": info.get("scriptPubKey"), "type": info.get("type")}


def _verify_by_script(a: dict, engines) -> None:
    """Verify the proof against the scriptPubKey bytes, and keep that command and its output for the statement.

    The proofs file's own verification used the address; BIP-322 proves the
    scriptPubKey, so the statement runs the verifier on the bytes the chain
    reports for the UTXO and quotes exactly that.
    """
    spk = a["script"]["script_pubkey"] or a["address"]
    proof = a["proof"]
    message = bytes.fromhex(proof["message_hex"]) if proof.get("message_hex") else proof["message"].encode("utf-8")
    try:
        result = verify_message(spk, proof["signature"], message, engines=engines or available_engines())
        verdict = result.to_dict()
        proof["state"], proof["verified"] = verdict["state"], verdict["state"] == "valid" and proof["verified"]
    except EngineError as exc:
        verdict = None
        proof["output"] = f"(not verified: {exc})"
    dashes = ["--"] if proof["message"].startswith("-") else []  # or the message would be read as an option
    proof["command"] = shell_command(["bip322", "verifymessage", spk, proof["signature"], *dashes, proof["message"]])
    if verdict:
        proof["output"] = format_verify_text(verdict)
        proof["verifier"] = verdict.get("tool")
        proof["engines"] = [f"{e['engine']} {e.get('version') or ''}".strip() for e in verdict.get("engines", []) if e.get("ok")]
    proof["script_pubkey"] = spk


def _lookup_command(row: dict, at: int) -> str:
    return shell_words(holdings_command([f"{row['txid']}:{row['vout']}"], at).split())


def _lookup_status(o: dict, row: dict, script: str | None) -> str:
    """What the chain says of a closing UTXO: ``o`` is its output as looked up, ``script`` the scriptPubKey its address encodes."""
    same = (
        o["amount_sat"] == row["amount_sat"]
        and o["address"] == row["address"]
        and (not o.get("script") or not script or o["script"] == script)
    )
    if not o["unspent"]:
        return "spent_since"
    if not same:
        return "contradicted"
    return "matches" if o["counted"] else "after_proof"


def _onchain(cli: BitcoinCli, closing_rows: list[dict], period: Period, scripts: dict[str, str | None], progress=None) -> dict:
    """Run the reader's on-chain step now, per UTXO, at the block named in its proof, and keep the command with its output.

    ``holdings TXID:VOUT --at <proof block>`` is a direct lookup.  A UTXO
    confirmed by that block and unspent now was held at that block: together
    with the proof dated the same block that is one claim with no gap, and
    "still unspent" carries it to the closing block, whichever side of the
    proof it lies on.  A UTXO confirmed after its proof's block is not a
    contradiction; the proof predates it and a later proof is needed.  A
    different scriptPubKey (``scripts``, by address) or amount is a
    contradiction.  UTXOs without a proof are looked up at the closing block.
    """
    if progress:
        progress("looking up the closing UTXOs on chain at the blocks of their proofs (bip322 audit holdings)")
    counts = {"matches": 0, "spent_since": 0, "after_proof": 0, "contradicted": 0, "not_run": 0}
    groups: dict[int, list[dict]] = {}
    for r in closing_rows:
        at = int(r["proof"]["stamp"]["height"]) if r.get("proof") else period.end.height
        groups.setdefault(at, []).append(r)
    error = None
    for at, rows in groups.items():
        try:
            result = holdings(cli, [f"{r['txid']}:{r['vout']}" for r in rows], at=at)
        except (RpcError, ValueError) as exc:
            error = error or str(exc)  # the first reason is the one the summary gives
            for r in rows:
                r["onchain"] = {"command": _lookup_command(r, at), "output": f"(not run: {exc})", "status": "not run", "at": at}
            counts["not_run"] += len(rows)
            continue
        found = {(o["txid"], o["vout"]): o for o in result["outputs"]}
        for r in rows:
            o = found[(r["txid"], r["vout"])]
            status = _lookup_status(o, r, scripts.get(r["address"]))
            counts[status] += 1
            counted_sat = o["amount_sat"] if o["counted"] else 0
            r["onchain"] = {
                "command": _lookup_command(r, at),
                "output": format_holdings({**result, "outputs": [o], "total_sat": counted_sat, "total_btc": btc(counted_sat)}),
                "status": status,
                "at": at,
                "tip": result["tip"]["height"],
            }
    return {**counts, "outputs": len(closing_rows), "error": error}


def _spent_by(tx: WalletTx | None) -> dict | None:
    """The confirmed transaction that has spent a closing coin since, as far as the history knows, or the write-off that took it
    out of the books; what the SPENT SINCE note names."""
    if tx is None:
        return None
    return {"txid": tx.txid, "height": tx.height, "time_utc": tx.iso_time, "written_off": tx.written_off, "reason": tx.reason}


def _by_address(rows: list[dict]) -> list[dict]:
    """The closing coins grouped per address, in order of first appearance: where the proof is verified, since proofs are per
    address; the statement's unit stays the UTXO, each page quoting its address's proof.

    The address's proof is its first output's, as a copy: ``_verify_by_script`` adds to it the verify command and its output.
    """
    groups: dict[str, dict] = {}
    for row in rows:
        g = groups.setdefault(
            row["address"], {"address": row["address"], "wallet": row.get("wallet"), "total_sat": 0, "outputs": [], "proof": row["proof"]}
        )
        g["total_sat"] += row["amount_sat"]
        g["outputs"].append(row)  # the same dict as in closing["coins"]: the on-chain result lands on it later
    for g in groups.values():
        g["total_btc"] = btc(g["total_sat"])
        if g["proof"]:
            g["proof"] = dict(g["proof"])
    return list(groups.values())


def _single(values: set) -> str | None:
    return next(iter(values)) if len(values) == 1 else (" / ".join(sorted(values)) if values else None)


def _report_id(report: dict) -> str:
    """A short identifier of the facts stated: the same facts give the same id, whenever and against whatever tip the report is made.

    The facts are the wallet's label and chain, the opening and closing
    blocks, the coins at each, the movements between them (with their
    valuation when rates were given) and, for each closing UTXO, the proof
    chosen: its proofs file, address and signature.  Nothing a later run would
    print differently goes in: no command output, no tip, no time of
    generation, no verifier version, nothing that happened after the period.
    """

    def coin(c: dict) -> list:
        return [c["txid"], c["vout"], c.get("address"), c["amount_sat"], c.get("height")]

    def proof(p: dict | None) -> list | None:
        return [p["file"], p["address"], p["signature"]] if p else None

    facts = {
        "label": report["label"],
        "chain": report["chain"],
        "blocks": [[report["period"][k]["height"], report["period"][k]["hash"]] for k in ("start", "end")],
        "opening": [coin(c) for c in report["opening"]["coins"]],
        "closing": [[*coin(c), proof(c.get("proof"))] for c in report["closing"]["coins"]],
        "movements": [
            {
                "txid": t["txid"],
                "height": t["height"],
                "fee_sat": t["fee_sat"],
                "fiat": t.get("fiat"),
                **{k: [coin(c) for c in t[k]] for k in ("ours_in", "ours_out", "others_out")},
            }
            for t in report["transactions"]
        ],
    }
    return hashlib.sha256(json.dumps(facts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _node_checks(clis: Mapping[str, BitcoinCli], history: History, period: Period, closing: list[Coin], dust: list[Coin]) -> dict | None:
    """A holder's node check: each loaded wallet's node wallet against that wallet's own closing coins, the results as one.

    None when no wallet's check runs; ``ok`` None with every reason when a wallet's could not run; else ``ok`` when each
    wallet's is, the counts added up, the lists joined, and ``wallets`` the ones checked.
    """
    checks = {
        w: _node_check(c, history, period, [x for x in closing if x.wallet == w], [x for x in dust if x.wallet == w], wallet=w)
        for w, c in clis.items()
    }
    found = {w: c for w, c in checks.items() if c is not None}
    if not found:
        return None
    not_run = {w: c["reason"] for w, c in found.items() if c["ok"] is None}
    if not_run:
        return {"ok": None, "reason": "; ".join(f"{w}: {reason}" for w, reason in not_run.items())}
    merged: dict = {"ok": all(c["ok"] for c in found.values()), "wallets": sorted(found)}
    merged.update({k: sum(c[k] for c in found.values()) for k in ("listunspent_count", "closing_count")})
    merged.update(
        {
            k: sorted(x for c in found.values() for x in c[k])
            for k in ("missing_from_report", "not_in_listunspent", "spent_in_mempool", "immature")
        }
    )
    return merged


def _node_check(
    cli: BitcoinCli, history: History, period: Period, closing: list[Coin], dust: list[Coin] = (), *, wallet: str | None = None
) -> dict | None:
    """The node's listunspent must agree with the closing coins (``dust``, and coins written off, left out on both sides).

    listunspent tells the present only, so the check counts only while the
    closing coins are the present ones: no transaction of the wallet's (of
    ``wallet``'s, for a holder) confirmed after the closing block, and the
    node's tip the history's, before and after the call.  Otherwise it is not
    run (``ok`` None, with the reason), which is not a failure.  A node
    wallet also leaves out of listunspent what an unconfirmed transaction
    spends, and a coinbase output until it matures: for every output the
    statement has and listunspent lacks, ``gettxout TXID VOUT false`` asks
    the chain alone, and only an output the chain no longer has is a mismatch.
    """
    moved = [
        tx
        for tx in history.txs
        if tx.height is not None
        and tx.height > period.end.height
        and (wallet is None or any(c.wallet == wallet for c in tx.ours_in + tx.ours_out))
    ]
    if moved:
        n = len(moved)
        return {"ok": None, "reason": f"coins moved after the closing block {period.end.height} ({n} transaction{'s' if n != 1 else ''})"}
    try:
        if cli.tip()[1] != history.tip_hash:
            return {"ok": None, "reason": f"the node's tip is not the history's (block {history.tip_height})"}
        rows = cli.call("listunspent", 1, 9999999) or []
        node = {(r["txid"], int(r["vout"])) for r in rows if int(r.get("confirmations", 0)) > 0} - {c.outpoint for c in dust}
        ours = {c.outpoint for c in closing}
        absent, in_mempool, immature = [], [], []
        for txid, vout in sorted(ours - node):
            out = cli.call("gettxout", txid, vout, False)  # false: without the mempool
            if out is None:
                absent.append(f"{txid}:{vout}")
            elif out.get("coinbase") and int(out.get("confirmations", 0)) <= 100:
                immature.append(f"{txid}:{vout}")
            else:
                in_mempool.append(f"{txid}:{vout}")
        if cli.tip()[1] != history.tip_hash:
            return {"ok": None, "reason": "a block arrived during the check"}
    except RpcError as exc:
        return {"ok": None, "reason": str(exc)}
    return {
        "ok": not absent and node <= ours,
        "listunspent_count": len(node),
        "closing_count": len(ours),
        "missing_from_report": sorted(f"{t}:{v}" for t, v in node - ours),
        "not_in_listunspent": absent,
        "spent_in_mempool": in_mempool,
        "immature": immature,
    }


def previous_statement(archive: Path | None, report: dict) -> dict | None:
    """The statement in ``archive`` (the wallet's or the holder's ``statements/``) that closed at this one's opening block: its
    period's label, reference and closing holdings, and whether those are this one's opening holdings; the newest when several,
    None when there is none.  Not a fact of the statement, so not in its reference: set on the report after it is built."""
    if archive is None or not archive.is_dir():
        return None
    found = []
    for path in sorted(archive.glob("*/*.json")):
        try:
            d = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        try:
            if d["period"]["end"]["height"] != report["period"]["start"]["height"]:
                continue
            found.append((str(d.get("generated_utc") or ""), d["period"]["label"], d["report_id"], int(d["closing"]["total_sat"]), path))
        except (KeyError, TypeError, ValueError):
            continue
    if not found:
        return None
    _, label, ref, closing_sat, path = max(found)
    return {
        "label": label,
        "report_id": ref,
        "file": path.name,
        "closing_sat": closing_sat,
        "closing_btc": btc(closing_sat),
        "agrees": closing_sat == report["opening"]["total_sat"],
    }


def _entry_text(e: dict) -> str:
    return f"{e['kind']} {e['amount_btc']} BTC {e['time_utc'][:10]}" + (f" ({e['reference']})" if e.get("reference") else "")


def format_summary(report: dict) -> str:
    """The terse text summary printed on stderr."""
    p, r, c = report["period"], report["reconciliation"], report["coverage"]
    lacking = [f"{c['before_count']} proven before it"] if c["before_count"] else []
    lacking += [f"{c['missing_count']} without proof"] if c["missing_count"] else []
    lines = [
        f"{report['label']}: {p['label']}  blocks {p['start']['height']} -> {p['end']['height']}"
        + ("  (to date)" if p["to_date"] else "")
        + f"  ref {report['report_id']}",
        f"opening {report['opening']['total_btc']} BTC  closing {report['closing']['total_btc']} BTC  net {report['totals']['net_btc']} BTC  "
        f"({report['totals']['transactions']} transactions, fees {report['totals']['fees_btc']} BTC)",
        f"reconciliation: {'ok' if r['ok'] else 'FAILED (diff ' + btc(r['diff_sat']) + ' BTC)'}",
        f"proof coverage: {c['covered_count']}/{c['total_count']} closing outputs, {c['covered_btc']} BTC, proven at or after the closing "
        f"block {c['closing_height']}"
        + (
            " (after it)"
            if c["covered_count"] and not c["at_closing_count"]
            else f" ({c['at_closing_count']} at it)"
            if c["at_closing_count"] != c["covered_count"]
            else ""
        )
        + ("" if c["complete"] else f"; UNPROVEN {c['uncovered_btc']} BTC: " + ", ".join(lacking)),
    ]
    if not c["complete"] and c["total_count"]:
        wallets = sorted({u["wallet"] for u in c["uncovered"] if u.get("wallet")})
        lines += [f"  to prove: bip322 audit snapshot -w {w} --at {c['closing_height']}" for w in wallets] or [
            f"  to prove: bip322 audit snapshot --at {c['closing_height']}"
        ]
    prev = report.get("previous")
    if prev:
        lines.append(
            f"opening: the closing of {prev['label']} (ref {prev['report_id']})"
            if prev["agrees"]
            else f"opening: NOT the closing of {prev['label']} (ref {prev['report_id']}), which was {prev['closing_btc']} BTC"
        )
    if report.get("wallets"):
        lines.append("wallets: " + ", ".join(f"{w['name']} {w['closing_btc']} BTC" for w in report["wallets"]))
    if report.get("idle_wallets"):
        lines.append("  nothing in the period: " + ", ".join(report["idle_wallets"]))
    d = report.get("dust") or {}
    if d.get("count"):
        lines.append(f"dust: {d['count']} outputs of at most {d['threshold_sat']} sat, {d['total_btc']} BTC, left out of every figure")
    if report.get("written_off"):
        lines.append(
            "written off in the period: "
            + ", ".join(f"{w['amount_btc']} BTC {w['time_utc'][:10]} ({w['reason']})" for w in report["written_off"])
        )
    for f in report["proofs_files"]:
        lines.append(
            f"  proofs {f['file']}: stamp {f['stamp']['height']}, signatures {f['signatures']}, {'verified' if f['verified'] else 'NOT VERIFIED'}, used for {f['used_for']}"
        )
    if report.get("proofs_files_not_used"):
        lines.append(f"  other proofs files, not used: {report['proofs_files_not_used']}")
    if report["pending_snapshots"]:
        lines.append("  awaiting signatures: " + ", ".join(report["pending_snapshots"]))
    for a in (report.get("endpoints") or {}).get("accounts", []):
        line = f"  account {a['name']}: {a['matched']}/{a['in_period']} entries of the period matched"
        if a.get("between"):
            moves = ", ".join(
                f"{'from' if b['kind'] == 'deposit' else 'to'} {b['other']} {b['amount_btc']} BTC {b['time_utc'][:10]}"
                + (f" (which says {btc(b['other_amount_sat'])})" if b["other_amount_sat"] != b["amount_sat"] else "")
                for b in a["between"]
            )
            line += f"; {len(a['between'])} between the holder's accounts: {moves}"
        if a["unmatched"]:
            line += f"; UNMATCHED {len(a['unmatched'])}: " + "; ".join(_entry_text(e) + " seen by no wallet" for e in a["unmatched"])
        if a["mismatched"]:
            line += f"; MISMATCH {len(a['mismatched'])}: " + "; ".join(
                f"{_entry_text(e)}, the chain {e['chain_btc']} BTC" for e in a["mismatched"]
            )
        lines.append(line)
    if report.get("unnamed_movements"):
        lines.append(f"  movements without a named far end: {report['unnamed_movements']}")
    oc = report.get("onchain")
    if oc:
        lines.append(
            "on chain (holdings by UTXO at the block of its proof): "
            f"{oc['matches']}/{oc['outputs']} held when proven"
            + (f", {oc['after_proof']} received after their proof" if oc["after_proof"] else "")
            + (f", {oc['spent_since']} spent since" if oc["spent_since"] else "")
            + (f", {oc['contradicted']} CONTRADICTED" if oc["contradicted"] else "")
            + (f", {oc['not_run']} not run ({oc['error']})" if oc["not_run"] else "")
        )
    nc = report.get("node_check")
    if nc and nc["ok"] is None:
        lines.append(f"node check (listunspent vs closing coins): not run ({nc['reason']})")
    elif nc:
        wrong = [
            f"{what} {', '.join(nc[key])}"
            for key, what in (("not_in_listunspent", "not in listunspent"), ("missing_from_report", "not in the statement"))
            if nc[key]
        ]
        notes = [
            f", {len(nc[key])} {what}"
            for key, what in (("spent_in_mempool", "spent by an unconfirmed transaction"), ("immature", "immature coinbase"))
            if nc[key]
        ]
        lines.append(
            "node check (listunspent vs closing coins): " + ("ok" if nc["ok"] else "MISMATCH: " + "; ".join(wrong)) + "".join(notes)
        )
    lines.append("RESULT: " + ("OK" if report["ok"] else "ATTENTION"))
    return "\n".join(lines)
