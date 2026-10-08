"""The wallet's side of unusual transactions: dust beside real coins, inputs from others, outputs without an address, order in a block."""

import csv
import json

import pytest
from fake_node import FakeNode

from bip322report.fiat import Rates
from bip322report.history import History, fetch_history
from bip322report.period import period_for_heights
from bip322report.render import render_html, write_csv
from bip322report.report import build_report

OTHER = "bc1qexternal0000000000000000000000000000000ext"
A, B, C, D, E = ("a1" * 32, "b2" * 32, "c3" * 32, "d4" * 32, "e5" * 32)


def _wallet_node(wallet, tip=1010) -> tuple[FakeNode, list[str]]:
    node = FakeNode(tip=tip)
    addresses = [wallet.derive(i).address for i in range(4)]
    node.mine |= set(addresses)
    return node, addresses


def _report(node, start=1000, end=1010, **kwargs) -> dict:
    return build_report(fetch_history(node), period_for_heights(node, start, end), label="x", cli=None, **kwargs)


def _rows(report: dict) -> list[tuple]:
    return [(t["txid"][:2], t["kind"], t["amount_sat"], t["fee_sat"], t["balance_after_sat"]) for t in report["transactions"]]


def _exact(report: dict) -> bool:
    """Opening plus the rows' amounts less the fees is the closing balance, and the total row is the sum of the rows."""
    t = report["totals"]
    return (
        report["reconciliation"]["ok"]
        and t["amount_sat"] == sum(x["amount_sat"] for x in report["transactions"])
        and report["opening"]["total_sat"] + t["amount_sat"] - t["fees_sat"] == report["closing"]["total_sat"]
        and all(x["balance_after_sat"] >= 0 for x in report["transactions"])
    )


# ---- dust ---------------------------------------------------------------- #


def test_the_wallets_own_change_is_never_dust(wallet):
    node, (a0, a1, *_) = _wallet_node(wallet)
    node.add(A, 990, [(OTHER, 0)], [(a0, 100_000)])
    node.add(B, 1005, [(A, 0)], [(OTHER, 99_000), (a1, 500)])  # pays 99,000; change 500; fee 500
    for dust in (1000, 0):
        report = _report(node, dust_sat=dust)
        assert _rows(report) == [("b2", "send", -99_000, 500, 500)]
        assert _exact(report)
        assert report["totals"]["sent_sat"] == 99_000 and report["closing"]["total_sat"] == 500 and report["dust"]["count"] == 0
    # and the node's listunspent, which has the change, agrees
    checked = build_report(fetch_history(node), period_for_heights(node, 1000, 1010), label="x", cli=node, onchain=False, dust_sat=1000)
    assert checked["node_check"]["ok"]


def test_dust_the_wallet_spends_is_booked_where_it_is_spent(wallet):
    node, (a0, a1, a2, _) = _wallet_node(wallet)
    node.add(A, 990, [(OTHER, 0)], [(a0, 100_000)])
    node.add(B, 992, [(OTHER, 1)], [(a1, 500)])
    node.add(C, 1005, [(A, 0), (B, 0)], [(a2, 100_300)])  # consolidation, fee 200
    report = _report(node, dust_sat=1000)
    # the opening balance is the closing balance of a statement that ended at block 1000, which left the dust out;
    # the dust enters in the block that spends it, as its own row before the consolidation, whose fee stays exact
    assert report["opening"]["total_sat"] == 100_000
    assert _rows(report) == [("b2", "swept", 500, None, 100_500), ("c3", "internal", 0, 200, 100_300)] and _exact(report)
    assert report["dust"]["count"] == 0 and (report["dust"]["swept_count"], report["dust"]["swept_sat"]) == (1, 500)
    html = render_html(report)
    assert "dust swept" in html and "Dust the wallet spent in the period (1, 0.00000500 in all)" in html
    # a period that closes before the spend leaves the dust out, whatever later blocks hold: its figures never change
    early = _report(node, 980, 1000, dust_sat=1000)
    assert early["closing"]["total_sat"] == 100_000 == report["opening"]["total_sat"] and [c["txid"] for c in early["dust"]["coins"]] == [B]
    assert _rows(early) == [("a1", "receive", 100_000, None, 100_000)] and early["dust"]["swept_count"] == 0 and _exact(early)
    then = FakeNode(tip=1000)
    then.mine, then.txs, then.order = node.mine, {k: v for k, v in node.txs.items() if k != C}, [A, B]
    at_the_time = _report(then, 980, 1000, dust_sat=1000)
    assert at_the_time["closing"]["total_sat"] == 100_000 and at_the_time["transactions"] == early["transactions"]
    assert at_the_time["dust"] == early["dust"] and at_the_time["totals"] == early["totals"]
    # the same spend paying somebody: what was paid out and the fee are exact
    node.txs[C]["vout"] = [(OTHER, 99_000), (a2, 1_300)]
    report = _report(node, dust_sat=1000)
    assert _rows(report) == [("b2", "swept", 500, None, 100_500), ("c3", "send", -99_000, 200, 1_300)]
    assert report["totals"]["sent_sat"] == 99_000 and _exact(report)


def test_two_dust_coins_consolidated_are_two_swept_rows_and_an_internal_move(wallet):
    node, (a0, a1, a2, _) = _wallet_node(wallet)
    node.add(A, 990, [(OTHER, 0)], [(a0, 900)])
    node.add(B, 991, [(OTHER, 1)], [(a1, 900)])
    node.add(C, 1005, [(A, 0), (B, 0)], [(a2, 1_500)])  # fee 300
    report = _report(node, 980, 1010, dust_sat=1000)
    assert _rows(report) == [("a1", "swept", 900, None, 900), ("b2", "swept", 900, None, 1_800), ("c3", "internal", 0, 300, 1_500)]
    assert (report["totals"]["received_sat"], report["totals"]["fees_sat"], report["closing"]["total_sat"]) == (1_800, 300, 1_500)
    assert report["dust"]["count"] == 0 and report["dust"]["swept_count"] == 2 and _exact(report)
    # the output of the consolidation is the wallet's own, never dust, whatever its size
    assert [c["amount_sat"] for c in report["closing"]["coins"]] == [1_500]


def test_dust_received_from_others_and_still_held_is_left_out(wallet):
    node, (a0, a1, a2, _) = _wallet_node(wallet)
    node.add(A, 990, [(OTHER, 0)], [(a0, 100_000)])
    node.add(B, 1003, [(OTHER, 1)], [(a1, 50_000), (a2, 400)])  # a payment with a dust output beside it
    node.add(C, 1004, [(OTHER, 2)], [(a0, 546)])  # nothing but dust: no movement
    node.add(D, None, [(OTHER, 3)], [(a0, 300)])  # unconfirmed dust
    plain = _report(node)
    assert plain["closing"]["total_sat"] == 150_946 and plain["dust"] == {**plain["dust"], "threshold_sat": 0, "count": 0, "coins": []}
    assert [t["txid"] for t in plain["pending_transactions"]] == [D]
    report = _report(node, dust_sat=546)
    assert _rows(report) == [("b2", "receive", 50_000, None, 150_000)] and _exact(report) and report["pending_transactions"] == []
    assert report["closing"]["total_sat"] == 150_000 and {c["txid"] for c in report["closing"]["coins"]} == {A, B}
    dust = report["dust"]
    assert (dust["count"], dust["total_sat"], dust["swept_count"]) == (2, 946, 0)
    assert [(c["txid"], c["vout"]) for c in dust["coins"]] == [(B, 1), (C, 0)] and all(c["created_utc"] for c in dust["coins"])
    assert "received from others are dust and left out of every figure" in render_html(report)
    checked = build_report(fetch_history(node), period_for_heights(node, 1000, 1010), label="x", cli=node, onchain=False, dust_sat=546)
    assert checked["node_check"]["ok"]


def test_nothing_but_dust_and_no_dust_at_all(wallet):
    node, (a0, a1, *_) = _wallet_node(wallet)
    node.add(A, 990, [(OTHER, 0)], [(a0, 600)])
    node.add(B, 1005, [(OTHER, 1)], [(a1, 700)])
    report = _report(node, dust_sat=1000)
    assert report["opening"]["total_sat"] == report["closing"]["total_sat"] == 0 and report["transactions"] == [] and _exact(report)
    assert (report["dust"]["count"], report["dust"]["total_sat"]) == (2, 1_300) and report["coverage"]["complete"] and report["ok"]
    html = render_html(report)
    assert "Nothing but dust" in html and "left out of every figure" in html and "How a proof" not in html
    history = fetch_history(node)
    assert history.without_dust(0) is history and history.without_dust(500) is history and history.dust_at(0, 1010) == []
    assert _rows(_report(node)) == [("b2", "receive", 700, None, 1_300)]
    with pytest.raises(ValueError, match="negative"):
        _report(node, dust_sat=-5)


# ---- inputs from others, outputs without an address ------------------------ #


def test_a_transaction_funded_with_others_is_mixed_and_its_fee_unknown(tmp_path, wallet):
    node, (a0, a1, a2, _) = _wallet_node(wallet)
    node.add(A, 990, [(OTHER, 0)], [(a0, 100_000)])
    node.add(B, 1003, [(A, 0), (OTHER, 5)], [(a1, 150_000), (OTHER, 70_000)])  # a payjoin received: ours 100,000 in, 150,000 back
    node.add(C, 1005, [(B, 0)], [(OTHER, 30_000), (a2, 119_000)])  # an ordinary send, fee 1,000
    history = fetch_history(node)
    assert [(tx.kind, tx.fee_sat, tx.others_in) for tx in history.txs] == [("receive", None, 1), ("mixed", None, 1), ("send", 1_000, 0)]
    report = _report(node)
    assert _rows(report) == [("b2", "mixed", 50_000, None, 150_000), ("c3", "send", -30_000, 1_000, 119_000)] and _exact(report)
    totals = report["totals"]
    assert (totals["received_sat"], totals["sent_sat"], totals["mixed_sat"], totals["mixed_count"]) == (0, 30_000, 50_000, 1)
    assert (totals["amount_sat"], totals["amount_btc"], totals["fees_sat"]) == (20_000, "0.00020000", 1_000)
    html = render_html(report)
    assert '<td>mixed</td><td class="num">0.00050000</td><td class="num"><span class="muted">unknown</span></td>' in html
    assert 'the holdings of section 2</td><td class="num">0.00020000</td><td class="num">(0.00001000)</td>' in html  # the sum of the rows
    assert "mixed 0.00050000, fee unknown" in html and "its fee is unknown" in html
    write_csv(report, tmp_path / "x.csv")
    rows = list(csv.DictReader((tmp_path / "x.csv").open()))
    assert [(r["kind"], r["amount_btc"], r["fee_btc"]) for r in rows] == [
        ("mixed", "0.00050000", "unknown"),
        ("send", "-0.00030000", "0.00001000"),
    ]
    write_csv(_report(node, rates=Rates.constant_rate("SEK", "1000000")), tmp_path / "sek.csv")
    fees = [(r["fee_btc"], r["fee_fiat"]) for r in csv.DictReader((tmp_path / "sek.csv").open())]
    assert fees == [("unknown", "unknown"), ("0.00001000", "10.00")]
    # a history saved before the inputs were counted: a transaction of ours without a fee was funded by others too
    old = history.to_dict()
    for tx in old["transactions"]:
        del tx["others_in"], tx["blockindex"]
    assert [tx.kind for tx in History.from_dict(old).txs] == ["receive", "mixed", "send"]
    # a mixed transaction that pays out: the amount is the wallet's net, the fee is not folded into a stated figure
    node.txs[B]["vout"] = [(OTHER, 120_000), (a1, 30_000)]
    node.txs[C].update(vin=[(B, 1)], vout=[(a2, 29_000)])
    report = _report(node)
    assert _rows(report) == [("b2", "mixed", -70_000, None, 30_000), ("c3", "internal", 0, 1_000, 29_000)] and _exact(report)
    assert (report["totals"]["sent_sat"], report["totals"]["mixed_sat"], report["totals"]["fees_sat"]) == (0, -70_000, 1_000)
    assert "mixed (0.00070000), fee unknown" in render_html(report)
    assert "mixed" not in render_html(_report(node, 1004, 1010))  # exceptions only


def test_a_payment_to_an_output_without_an_address_is_a_send(tmp_path, wallet):
    node, (a0, a1, a2, _) = _wallet_node(wallet)
    node.add(A, 990, [(OTHER, 0)], [(a0, 100_000)])
    node.add(B, 1003, [(A, 0)], [(None, 40_000), (a1, 59_000)])  # 40,000 to a bare script; fee 1,000
    node.add(C, 1005, [(B, 1)], [(None, 0), (a2, 58_500)])  # a move within the wallet that carries an OP_RETURN note; fee 500
    history = fetch_history(node)
    assert [tx.kind for tx in history.txs] == ["receive", "send", "internal"]
    assert [(c.address, c.amount_sat) for c in history.txs[1].others_out] == [(None, 40_000)] and history.txs[2].others_out == []
    assert History.from_dict(json.loads(json.dumps(history.to_dict()))).to_dict() == history.to_dict()
    report = _report(node)
    assert _rows(report) == [("b2", "send", -40_000, 1_000, 59_000), ("c3", "internal", 0, 500, 58_500)] and _exact(report)
    assert report["totals"]["sent_sat"] == 40_000 and report["transactions"][0]["others_out"][0]["address"] is None
    assert "paid out 0.00040000" in render_html(report)
    write_csv(report, tmp_path / "x.csv")
    assert next(csv.DictReader((tmp_path / "x.csv").open()))["others_out"] == "no-address=0.00040000"


# ---- order within a block --------------------------------------------------- #


def test_movements_within_a_block_follow_their_position_in_it(wallet):
    node, (a0, a1, a2, a3) = _wallet_node(wallet)
    late, early = "0a" * 32, "ff" * 32  # by txid the spend would come first
    node.add(early, 1005, [(OTHER, 0)], [(a0, 1_000_000)], blockindex=3)
    node.add(late, 1005, [(early, 0)], [(OTHER, 999_000)], blockindex=7)  # spends it in the same block; fee 1,000
    node.add(B, 1005, [(OTHER, 1)], [(a1, 5_000)], blockindex=1)
    node.add(A, 1005, [(OTHER, 2)], [(a2, 7_000)], blockindex=2)
    history = fetch_history(node)
    assert [tx.txid for tx in history.txs] == [B, A, early, late] and [tx.blockindex for tx in history.txs] == [1, 2, 3, 7]
    report = _report(node)
    assert [r[4] for r in _rows(report)] == [5_000, 12_000, 1_012_000, 12_000] and _exact(report)
    # a history saved without positions: no spend before the receive it spends, then by txid
    old = history.to_dict()
    for tx in old["transactions"]:
        del tx["blockindex"]
    assert [tx.txid for tx in History.from_dict(old).txs] == [A, B, early, late]
    chain = FakeNode(tip=1010)  # a node that gives no position at all
    chain.mine = node.mine
    for txid in (late, early):
        chain.add(txid, 1005, node.txs[txid]["vin"], node.txs[txid]["vout"])
    assert [tx.txid for tx in fetch_history(chain).txs] == [early, late]


# ---- unconfirmed, or given up ------------------------------------------------ #


def test_abandoned_and_replaced_transactions_are_not_unconfirmed(wallet):
    node, (a0, a1, a2, a3) = _wallet_node(wallet)
    node.add(A, 990, [(OTHER, 0)], [(a0, 100_000)])
    node.add(B, 991, [(OTHER, 1)], [(a1, 200_000)])
    node.add(C, None, [(A, 0)], [(OTHER, 99_000)])  # plainly unconfirmed
    node.add(D, None, [(B, 0)], [(OTHER, 199_000)], mempoolconflicts=[E])  # replaced: its replacement is in the mempool
    node.add(E, None, [(B, 0)], [(OTHER, 198_000)], walletconflicts=[D])  # the replacement, live
    node.add("f6" * 32, None, [(B, 0)], [(OTHER, 197_000)], walletconflicts=[E], in_mempool=False)  # an earlier node: no mempoolconflicts
    node.add("07" * 32, None, [(A, 0)], [(OTHER, 98_000)], in_mempool=False, details=[{"category": "send", "abandoned": True}])
    node.add("18" * 32, None, [(OTHER, 2)], [(a2, 5_000)], in_mempool=False)  # dropped from the mempool, not given up: still pending
    history = fetch_history(node)
    assert [tx.txid for tx in history.pending] == [C, E, "18" * 32] and [tx.txid for tx in history.txs] == [A, B]
    report = _report(node, 980, 1010)
    assert [t["txid"] for t in report["pending_transactions"]] == [C, E, "18" * 32] and report["closing"]["total_sat"] == 300_000
