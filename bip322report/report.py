"""Assemble the report: balances, movements, reconciliation, proof coverage."""

from __future__ import annotations

import hashlib
import json
import shlex
from datetime import UTC, datetime
from pathlib import Path

from bip322audit.holdings import format_holdings, holdings, holdings_command
from bip322audit.rpc import BitcoinCli, RpcError, btc
from bip322core.core import BIP322Error
from bip322core.engines import EngineError, available_engines
from bip322core.report import describe_address, format_verify_text
from bip322core.verify import verify_message

from . import TOOL
from .coverage import cover_coins, load_ledger, pending_bundles, pending_name
from .fiat import Rates
from .history import Coin, History, WalletTx
from .period import Period


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_report(
    history: History,
    period: Period,
    *,
    label: str,
    holder: str | None = None,
    ledger_roots=(),
    cli: BitcoinCli | None = None,
    rates: Rates | None = None,
    engines=None,
    progress=None,
    onchain: bool = True,
    dust_sat: int = 0,
) -> dict:
    """The report as a plain dict (what report.json holds and the templates render).

    ``cli`` is needed to re-verify the ledger's proofs against the chain and to
    cross-check the closing balance with ``listunspent`` when the period runs
    to the tip; without it the proofs are verified for their signatures only.
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
    history = history.without_dust(dust_sat)
    opening = history.coins_at(period.start.height)
    closing = history.coins_at(period.end.height)
    movements = history.between(period.start.height, period.end.height)
    opening_sat = sum(c.amount_sat for c in opening)
    closing_sat = sum(c.amount_sat for c in closing)

    bundles = load_ledger(cli, ledger_roots, engines=engines, progress=progress) if ledger_roots else []
    closing_rows, used = _closing_rows(history, closing, bundles, created, period)
    closing_addresses = _by_address(closing_rows)
    _verify_addresses(closing_addresses, engines)
    scripts = {a["address"]: a["script"]["script_pubkey"] for a in closing_addresses}
    onchain_result = _onchain(cli, closing_rows, period, scripts, progress) if cli is not None and onchain and closing_rows else None
    tx_rows, totals = _movement_rows(movements, opening_sat, rates)
    net_sat = totals["net_sat"]

    report = {
        "tool": TOOL,
        "generated_utc": _now(),
        "label": label,
        "holder": holder,
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
        "coverage": _coverage(closing_rows, closing_addresses, closing_sat),
        "bundles": [
            {**b.to_dict(), "after_period": int(b.stamp["height"]) > period.end.height, "used_for": used[b.path]}
            for b in bundles
            if b.path in used
        ],
        "bundles_not_used": sum(
            1 for b in bundles if b.path not in used
        ),  # a bare count: what else the ledger holds is not the statement's business
        "policy": _single({b.document.get("policy") for b in bundles if b.path in used} - {None}),
        "pending_bundles": [pending_name(p, ledger_roots) for p in pending_bundles(ledger_roots, {c.address for c in closing})],
        "pending_transactions": [tx.to_dict() for tx in history.pending],
        "onchain": onchain_result,
        "fiat": rates.to_dict() if rates else None,
        "node_check": _node_check(cli, history, period, closing, dust) if cli is not None else None,
    }
    report["report_id"] = _report_id(report)
    report["ok"] = _verdict(report)
    return report


def _closing_rows(
    history: History, closing: list[Coin], bundles: list, created: dict, period: Period
) -> tuple[list[dict], dict[Path, int]]:
    """The closing coins as rows, each with the best proof the ledger has for its address; and, per bundle file, the rows citing it."""
    covers = cover_coins(closing, bundles)
    spenders: dict[tuple, WalletTx] = {}  # outpoint -> the confirmed transaction that spends it: one pass, not History.spender_of per coin
    for tx in history.txs:
        for c in tx.ours_in:
            spenders.setdefault(c.outpoint, tx)
    used: dict[Path, int] = {}  # the bundles the statement relies on
    rows = []
    for coin in closing:
        best = covers[coin.outpoint][0] if covers[coin.outpoint] else None
        proof = best.to_dict() if best else None
        if best:
            used[best.bundle.path] = used.get(best.bundle.path, 0) + 1
            proof["after_period"] = int(proof["stamp"]["height"]) > period.end.height
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
        cited = (a["proof"]["bundle"], a["proof"]["signature"])
        for o in a["outputs"]:
            if o["proof"] and (o["proof"]["bundle"], o["proof"]["signature"]) == cited:
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


def _movement_rows(movements: list[WalletTx], opening_sat: int, rates: Rates | None) -> tuple[list[dict], dict]:
    """The period's transactions as rows with a running balance, and the totals of the period."""
    rows = []
    received = sent = fees = mixed = mixed_count = 0
    running = opening_sat
    for tx in movements:
        row = tx.to_dict()
        running += tx.net_sat
        row["balance_after_sat"] = running
        row["balance_after_btc"] = btc(running)
        # what moved between the wallet and others, before the fee: received, or (sent).  A mixed transaction's fee is
        # unknown (others funded it too), so its amount is the wallet's net and no fee is stated for it.
        amount = tx.net_sat + (tx.fee_sat or 0)
        row["amount_sat"] = amount
        row["amount_btc"] = btc(amount)
        if tx.kind in ("receive", "swept"):
            row["others_out"] = []  # a payer's own change is not the wallet's business
        if rates:
            row["fiat"] = _fiat(rates, tx, amount)
        rows.append(row)
        if tx.kind in ("receive", "swept"):  # swept: dust the wallet spent, booked where it was spent
            received += amount
        elif tx.kind == "send":
            sent += -amount
        elif tx.kind == "mixed":
            mixed += amount
            mixed_count += 1
        if tx.fee_sat:
            fees += tx.fee_sat
    amount_sat = received - sent + mixed  # the sum of the rows' amounts: an internal move's is zero
    net_sat = running - opening_sat
    totals = {
        "received_sat": received,
        "received_btc": btc(received),
        "sent_sat": sent,
        "sent_btc": btc(sent),
        "mixed_sat": mixed,
        "mixed_btc": btc(mixed),
        "mixed_count": mixed_count,
        "amount_sat": amount_sat,
        "amount_btc": btc(amount_sat),
        "fees_sat": fees,
        "fees_btc": btc(fees),
        "net_sat": net_sat,
        "net_btc": btc(net_sat),
        "transactions": len(rows),
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


def _covered(row: dict) -> bool:
    return bool(row["proof"] and row["proof"]["verified"])


def _coverage(rows: list[dict], addresses: list[dict], closing_sat: int) -> dict:
    """How much of the closing balance a verified proof backs, counted by output and by address (an address: all of its outputs)."""
    uncovered = [r for r in rows if not _covered(r)]
    covered_sat = closing_sat - sum(r["amount_sat"] for r in uncovered)
    whole = [a for a in addresses if all(_covered(o) for o in a["outputs"])]
    return {
        "covered_sat": covered_sat,
        "covered_btc": btc(covered_sat),
        "uncovered_sat": closing_sat - covered_sat,
        "uncovered_btc": btc(closing_sat - covered_sat),
        "covered_count": len(rows) - len(uncovered),
        "covered_after_period_count": sum(1 for r in rows if _covered(r) and r["proof"]["after_period"]),
        "addresses_total": len(addresses),
        "addresses_covered": len(whole),
        "addresses_after_period": sum(1 for a in whole if a["proof"]["after_period"]),
        "total_count": len(rows),
        "complete": not uncovered,
        "uncovered": [{k: r[k] for k in ("txid", "vout", "address", "amount_sat", "amount_btc")} for r in uncovered],
    }


def _verdict(report: dict) -> bool:
    """OK when the balances reconcile, every closing UTXO has a verified proof, and neither the node nor the chain contradicts the statement."""
    node, chain = report["node_check"], report["onchain"]
    node_ok = node is None or node.get("ok") is not False  # None: could not be checked, not a failure
    chain_ok = chain is None or chain["contradicted"] == 0  # a lookup that could not run excuses none that did
    return bool(report["reconciliation"]["ok"] and report["coverage"]["complete"] and node_ok and chain_ok)


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

    The bundle's own verification used the address; BIP-322 proves the
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
    """The confirmed transaction that has spent a closing coin since, as far as the history knows; what the SPENT SINCE note names."""
    return {"txid": tx.txid, "height": tx.height, "time_utc": tx.iso_time} if tx else None


def _by_address(rows: list[dict]) -> list[dict]:
    """The closing coins grouped per address, in order of first appearance: the statement's unit, since proofs are per address.

    The address's proof is its first output's, as a copy: the statement adds to it what the page of the address quotes
    (``before_coins`` here, the verify command and its output in ``_verify_by_script``).
    """
    groups: dict[str, dict] = {}
    for row in rows:
        g = groups.setdefault(row["address"], {"address": row["address"], "total_sat": 0, "outputs": [], "proof": row["proof"]})
        g["total_sat"] += row["amount_sat"]
        g["outputs"].append(row)  # the same dict as in closing["coins"]: the on-chain result lands on it later
    out = []
    for g in groups.values():
        g["total_btc"] = btc(g["total_sat"])
        times = sorted(o["created_utc"] for o in g["outputs"] if o["created_utc"])
        g["received_first"], g["received_last"] = (times[0], times[-1]) if times else (None, None)
        if g["proof"]:
            stamp = int(g["proof"]["stamp"]["height"])
            heights = [o["height"] for o in g["outputs"] if o["height"] is not None]
            before = "all" if heights and stamp < min(heights) else ("some" if heights and stamp < max(heights) else None)
            g["proof"] = {**g["proof"], "before_coins": before}
        out.append(g)
    return out


def _single(values: set) -> str | None:
    return next(iter(values)) if len(values) == 1 else (" / ".join(sorted(values)) if values else None)


def _report_id(report: dict) -> str:
    """A short identifier of the facts stated: the same facts give the same id, whenever and against whatever tip the report is made.

    The facts are the wallet's label and chain, the opening and closing
    blocks, the coins at each, the movements between them (with their
    valuation when rates were given) and, for each closing UTXO, the proof
    chosen: its bundle, address and signature.  Nothing a later run would
    print differently goes in: no command output, no tip, no time of
    generation, no verifier version, nothing that happened after the period.
    """

    def coin(c: dict) -> list:
        return [c["txid"], c["vout"], c.get("address"), c["amount_sat"], c.get("height")]

    def proof(p: dict | None) -> list | None:
        return [p["bundle"], p["address"], p["signature"]] if p else None

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


def _node_check(cli: BitcoinCli, history: History, period: Period, closing: list[Coin], dust: list[Coin] = ()) -> dict | None:
    """When the period ends at the history's tip, the node's listunspent must agree with the closing coins (``dust`` left out on both sides).

    listunspent tells the present only, so the check counts only when the
    node's tip is the history's, before and after it: otherwise it is not
    run (``ok`` None, with the reason), which is not a failure.  A node
    wallet also leaves out of listunspent what an unconfirmed transaction
    spends, and a coinbase output until it matures: for every output the
    statement has and listunspent lacks, ``gettxout TXID VOUT false`` asks
    the chain alone, and only an output the chain no longer has is a mismatch.
    """
    if period.end.height != history.tip_height:
        return None
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


def format_summary(report: dict) -> str:
    """The terse text summary printed on stderr."""
    p, r, c = report["period"], report["reconciliation"], report["coverage"]
    lines = [
        f"{report['label']}: {p['label']}  blocks {p['start']['height']} -> {p['end']['height']}"
        + ("  (to tip)" if p["to_tip"] else "")
        + f"  ref {report['report_id']}",
        f"opening {report['opening']['total_btc']} BTC  closing {report['closing']['total_btc']} BTC  net {report['totals']['net_btc']} BTC  "
        f"({report['totals']['transactions']} transactions, fees {report['totals']['fees_btc']} BTC)",
        f"reconciliation: {'ok' if r['ok'] else 'FAILED (diff ' + btc(r['diff_sat']) + ' BTC)'}",
        f"proof coverage: {c['covered_count']}/{c['total_count']} closing outputs, {c['covered_btc']} BTC; "
        f"stamped after the period's end: {c['covered_after_period_count']}/{c['total_count']}"
        + ("" if c["complete"] else f"; UNCOVERED {c['uncovered_btc']} BTC"),
    ]
    d = report.get("dust") or {}
    if d.get("count"):
        lines.append(f"dust: {d['count']} outputs of at most {d['threshold_sat']} sat, {d['total_btc']} BTC, left out of every figure")
    for b in report["bundles"]:
        lines.append(
            f"  bundle {b['bundle']}: stamp {b['stamp']['height']}, signatures {b['signatures']}, {'verified' if b['verified'] else 'NOT VERIFIED'}, used for {b['used_for']}"
        )
    if report.get("bundles_not_used"):
        lines.append(f"  other bundles in the ledger, not used: {report['bundles_not_used']}")
    if report["pending_bundles"]:
        lines.append("  awaiting signatures: " + ", ".join(report["pending_bundles"]))
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
