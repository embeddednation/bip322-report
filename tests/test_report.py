"""bip322-report against a fake node: history, balances, periods, proof coverage, rendering, CLI."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from bip322audit.audit import finalize_bundle
from bip322audit.ledger import proven_addresses
from bip322audit.snapshot import take_snapshot, write_bundle
from bip322core.dev.signing import sign_psbt
from bip322core.psbt import parse_psbt
from fake_node import T0, FakeNode, block_time, fake_hash

from bip322report.coverage import cover_coins, load_ledger, pending_bundles
from bip322report.fiat import Rates
from bip322report.history import History, fetch_history
from bip322report.period import block, last_block_before, parse_when, period_between, period_for_heights, period_for_year
from bip322report.render import render_html, write_csv
from bip322report.report import build_report, format_summary

BTC = 100_000_000
EXTERNAL = "bc1qexternal0000000000000000000000000000000ext"
EXTERNAL2 = "bc1qexternal1111111111111111111111111111111ext"


def _node(wallet, tip=1000) -> FakeNode:
    """Coins arriving at heights 980-993, a spend and an internal move after 1000, a pending and a conflicted tx."""
    node = FakeNode(tip=tip)
    a0, a1, a2, a3 = wallet.derive(0).address, wallet.derive(0, 1).address, wallet.derive(1, 1).address, wallet.derive(1).address
    node.mine |= {a0, a1, a2, a3}
    node.add("aa" * 32, 990, [(EXTERNAL, 0)], [(a0, 50_000_000)])
    node.add("bb" * 32, 993, [(EXTERNAL2, 0)], [(a0, 25_000_000)])
    node.add("cc" * 32, 980, [(EXTERNAL, 1)], [(a1, 10_000_000)])
    if tip >= 1010:
        node.add("dd" * 32, 1005, [("aa" * 32, 0)], [(EXTERNAL, 20_000_000), (a2, 29_000_000)])  # send: fee 0.01
        node.add("ee" * 32, 1010, [("cc" * 32, 0)], [(a3, 9_900_000)])  # internal: fee 0.001
        node.add("ff" * 32, None, [("bb" * 32, 0)], [(EXTERNAL, 24_990_000)])  # pending
        node.add("99" * 32, 1011, [("bb" * 32, 0)], [(EXTERNAL2, 24_000_000)], conflicted=True)
    return node


def test_history_resolves_the_wallets_side_of_every_transaction(wallet):
    node = _node(wallet, tip=1012)
    history = fetch_history(node)
    assert history.wallet == "watch" and history.chain == "main" and history.tip_height == 1012
    assert [tx.txid[:2] for tx in history.txs] == ["cc", "aa", "bb", "dd", "ee"] and [tx.txid[:2] for tx in history.pending] == ["ff"]
    kinds = {tx.txid[:2]: tx.kind for tx in history.txs}
    assert kinds == {"cc": "receive", "aa": "receive", "bb": "receive", "dd": "send", "ee": "internal"}
    dd = next(tx for tx in history.txs if tx.txid.startswith("dd"))
    assert dd.fee_sat == 1_000_000 and dd.net_sat == -21_000_000 and [c.amount_sat for c in dd.others_out] == [20_000_000]
    assert history.spender_of(("aa" * 32, 0)) is dd and history.spender_of(("bb" * 32, 0)) is None  # pending spends do not count
    assert history.balance_at(1000) == 85_000_000 and history.balance_at(1012) == 63_900_000 and history.balance_at(979) == 0
    assert {c.outpoint for c in history.coins_at(1012)} == {("bb" * 32, 0), ("dd" * 32, 1), ("ee" * 32, 0)}
    assert [tx.txid[:2] for tx in history.between(1000, 1012)] == ["dd", "ee"]
    # exact JSON round trip
    again = History.from_dict(json.loads(json.dumps(history.to_dict())))
    assert again.to_dict() == history.to_dict() and again.balance_at(1012) == 63_900_000


def test_periods_resolve_to_the_last_block_before_an_instant(wallet):
    node = _node(wallet)
    at = datetime.fromtimestamp(block_time(500), UTC)
    assert last_block_before(node, at).height == 499  # strictly before
    assert last_block_before(node, datetime.fromtimestamp(block_time(500) + 1, UTC)).height == 500
    assert last_block_before(node, datetime.fromtimestamp(T0 - 5, UTC)).height == 0  # chain starts inside the period
    assert last_block_before(node, datetime(2099, 1, 1, tzinfo=UTC)).height == 1000
    p = period_between(node, datetime.fromtimestamp(block_time(100), UTC), datetime.fromtimestamp(block_time(900), UTC))
    assert (p.start.height, p.end.height, p.to_tip) == (99, 899, False) and p.start.hash == fake_hash(99)
    p = period_between(node, datetime.fromtimestamp(block_time(100), UTC), datetime(2099, 1, 1, tzinfo=UTC))
    assert (p.end.height, p.to_tip) == (1000, True)
    with pytest.raises(ValueError, match="closing block is not settled"):  # every fake block lies in November 2023: none after the year
        period_for_year(node, 2023)
    p = period_for_year(FakeNode(tip=7000), 2023)  # block 6779 is the first of 2024
    assert (p.label, p.start.height, p.end.height, p.to_tip) == ("2023", 0, 6778, False)
    assert period_for_heights(node, 10, 20).end == block(node, 20)
    with pytest.raises(ValueError):
        period_for_heights(node, 20, 10)
    with pytest.raises(ValueError):
        period_for_heights(node, 10, 5000)
    assert parse_when("2026-01-01") == datetime(2026, 1, 1, tzinfo=UTC)
    assert parse_when("2026-01-01T12:00:00Z") == datetime(2026, 1, 1, 12, tzinfo=UTC)
    assert parse_when("2026-01-01T12:00:00+02:00") == datetime(2026, 1, 1, 10, tzinfo=UTC)


def _bundle(directory: Path, node: FakeNode, wallet, signers, template="Proof of control {date}", skip=None, addresses=None) -> dict:
    """snapshot (or prove given addresses), sign with two cosigners, finalize (recording spends from the fake wallet)."""
    snapshot, psbts = take_snapshot(node, wallet, template, skip_addresses=skip, addresses=addresses)
    write_bundle(directory, snapshot, psbts)
    for entry in snapshot.addresses:
        psbt = parse_psbt((directory / entry["file"]).read_text())
        for signer in signers[:2]:
            assert sign_psbt(psbt, signer) == 1
        (directory / "signed" / Path(entry["file"]).name.replace(".psbt", "-part.psbt")).write_text(psbt.to_string())
    document = finalize_bundle(directory, cli=node)
    (directory / "proofs.json").write_text(json.dumps(document, indent=2))
    return document


def test_report_backs_every_closing_coin_with_a_verified_proof(tmp_path, wallet, signer_expressions):
    ledger = tmp_path / "ledger"
    node = _node(wallet, tip=1000)
    first = _bundle(ledger / "snapshot-1", node, wallet, signer_expressions, "Owner proof {date}")
    assert {(u["txid"][:2], u["vout"]) for p in first["proofs"] for u in p["utxos"]} == {("aa", 0), ("bb", 0), ("cc", 0)}

    node = _node(wallet, tip=1020)  # time passes: a spend, an internal move, and enough blocks for a depth-6 stamp to cover them
    history = fetch_history(node)
    period = period_for_heights(node, 1000, 1020, label="test")
    # only the first bundle: the two new outputs have no proof yet
    report = build_report(history, period, label="Treasury", ledger_roots=[ledger], cli=node)
    assert report["opening"]["total_sat"] == 85_000_000 and report["closing"]["total_sat"] == 63_900_000
    assert report["reconciliation"]["ok"] and report["totals"] == {
        "received_sat": 0, "received_btc": "0.00000000", "sent_sat": 20_000_000, "sent_btc": "0.20000000",
        "mixed_sat": 0, "mixed_btc": "0.00000000", "mixed_count": 0, "amount_sat": -20_000_000, "amount_btc": "-0.20000000",
        "fees_sat": 1_100_000, "fees_btc": "0.01100000", "net_sat": -21_100_000, "net_btc": "-0.21100000", "transactions": 2,
    }  # fmt: skip
    cov = report["coverage"]
    assert (cov["covered_count"], cov["total_count"], cov["covered_sat"], cov["complete"]) == (1, 3, 25_000_000, False)
    assert {(u["txid"][:2], u["vout"]) for u in cov["uncovered"]} == {("dd", 1), ("ee", 0)}
    assert report["bundles"][0]["verified"] and report["bundles"][0]["signatures"] == "2/2" and report["bundles"][0]["used_for"] == 1
    assert report["node_check"]["ok"] and not report["ok"]
    assert [t["txid"][:2] for t in report["pending_transactions"]] == ["ff"]
    assert "ATTENTION" in format_summary(report) and "UNCOVERED 0.38900000 BTC" in format_summary(report)

    # a second bundle for the outputs no bundle proves yet; now the closing balance is fully covered
    second = _bundle(ledger / "snapshot-2", node, wallet, signer_expressions, skip=proven_addresses([ledger]))
    assert {(u["txid"][:2], u["vout"]) for p in second["proofs"] for u in p["utxos"]} == {("dd", 1), ("ee", 0)}
    (ledger / "snapshot-3").mkdir()
    (ledger / "snapshot-3" / "snapshot.json").write_text(
        json.dumps({"addresses": [{"address": wallet.derive(0).address}]})
    )  # not yet signed
    report = build_report(history, period, label="Treasury", ledger_roots=[ledger], cli=node, rates=Rates.constant_rate("SEK", "1000000"))
    cov = report["coverage"]
    assert cov["complete"] and cov["covered_sat"] == 63_900_000 and report["ok"]
    sends = [x for x in report["transactions"] if x["kind"] == "send"]
    assert all(x["amount_sat"] == x["net_sat"] + x["fee_sat"] for x in sends)  # amount = what the receivers got, (sent); fee beside it
    # dust: outputs at or below the threshold that others paid and the wallet has not spent are left out of every figure
    dusty = build_report(history, period, label="Treasury", ledger_roots=[ledger], cli=node, dust_sat=1000)
    assert dusty["dust"]["count"] == 0 and dusty["coverage"] == report["coverage"] and dusty["closing"]["total_sat"] == 63_900_000
    # at any threshold: bb:0 was received and is held, so it is dust; aa:0 and cc:0 were received as dust and are booked where
    # the wallet spends them; dd:1 and ee:0 are the wallet's own change
    dusty = build_report(history, period, label="Treasury", ledger_roots=[ledger], cli=node, dust_sat=63_900_000)
    assert (
        [c["txid"][:2] for c in dusty["dust"]["coins"]] == ["bb"]
        and dusty["dust"]["total_sat"] == 25_000_000
        and (dusty["dust"]["swept_count"], dusty["dust"]["swept_sat"]) == (2, 60_000_000)
        and (dusty["opening"]["total_sat"], dusty["closing"]["total_sat"]) == (0, 38_900_000)
        and [(t["kind"], t["amount_sat"], t["fee_sat"]) for t in dusty["transactions"]]
        == [("swept", 50_000_000, None), ("send", -20_000_000, 1_000_000), ("swept", 10_000_000, None), ("internal", 0, 100_000)]
        and dusty["coverage"]["total_count"] == 2
        and dusty["coverage"]["complete"]
        and dusty["reconciliation"]["ok"]
        and dusty["node_check"]["ok"]
        and dusty["ok"]
        and "dust: 1 outputs" in format_summary(dusty)
    )
    assert "left out of every figure" in render_html(dusty)
    by_out = {(c["txid"][:2], c["vout"]): c["proof"] for c in report["closing"]["coins"]}
    assert by_out[("bb", 0)]["message"].startswith("Owner proof") and by_out[("bb", 0)]["verified"]
    assert by_out[("dd", 1)]["bundle"].endswith("snapshot-2/proofs.json") and by_out[("dd", 1)]["stamp"]["height"] == 1014
    assert [b["used_for"] for b in report["bundles"]] == [1, 2] and report["pending_bundles"] == ["snapshot-3"]
    dd = next(t for t in report["transactions"] if t["txid"].startswith("dd"))
    assert dd["fiat"] == {"currency": "SEK", "rate": "1000000", "amount": "-200000.00", "net": "-210000.00", "fee": "10000.00"}

    html = render_html(report, explorer="https://mempool.space")
    assert (
        "Treasury" in html
        and "pill bad" not in html
        and "0.63900000" in html
        and "Owner proof" in html
        and 'href="https://mempool.space/tx/' in html
    )
    assert "VALID" in html and "no proof" not in html and "Attention" not in html
    assert report["report_id"] and len(report["report_id"]) == 16 and report["report_id"] in html and report["holder"] is None
    again = build_report(history, period, label="Treasury", ledger_roots=[ledger], cli=node, rates=Rates.constant_rate("SEK", "1000000"))
    assert again["report_id"] == report["report_id"]  # the same facts give the same reference, whenever generated
    assert [t["balance_after_sat"] for t in report["transactions"]] == [64_000_000, 63_900_000]  # a running balance
    assert "(0.20000000)" in html and "(0.01000000)" in html  # the amount sent and the fee, both in accounting parentheses
    with_holder = render_html(build_report(history, period, label="Treasury", holder="Demo Holdings AB", ledger_roots=[ledger], cli=node))
    assert "Demo Holdings AB" in with_holder
    assert "href=" not in render_html(report, explorer="") and "href=" not in render_html(report)  # links only when an explorer is named
    csv_path = tmp_path / "t.csv"
    write_csv(report, csv_path)
    lines = csv_path.read_text().splitlines()
    assert (
        lines[0] == "time_utc,height,txid,kind,amount_btc,fee_btc,net_btc,rate,amount_fiat,fee_fiat,net_fiat,ours_in,ours_out,others_out"
        and len(lines) == 3
    )
    assert "send,-0.20000000,0.01000000,-0.21000000,1000000,-200000.00,10000.00,-210000.00" in lines[1]

    # coverage prefers a verified proof with the latest stamp; the ledger can be given as its bundles too
    bundles = load_ledger(node, [ledger / "snapshot-1", ledger / "snapshot-2"])
    covers = cover_coins(history.coins_at(1020), bundles)
    assert [c.bundle.path.parent.name for c in covers[("bb" * 32, 0)]] == ["snapshot-1"]
    assert pending_bundles([ledger]) == [ledger / "snapshot-3"]


def test_report_without_a_node_verifies_signatures_only(tmp_path, wallet, signer_expressions):
    ledger = tmp_path / "ledger"
    node = _node(wallet, tip=1000)
    _bundle(ledger / "b", node, wallet, signer_expressions)
    history = fetch_history(node)
    period = period_for_heights(node, 979, 1000)
    report = build_report(history, period, label="x", ledger_roots=[ledger], cli=None, engines=["btclib"])
    assert (
        report["opening"]["total_sat"] == 0
        and report["closing"]["total_sat"] == 85_000_000
        and report["totals"]["received_sat"] == 85_000_000
    )
    assert report["node_check"] is None and report["coverage"]["complete"] and report["bundles"][0]["stamp_ok"] is None
    assert all(c["proof"]["verified"] for c in report["closing"]["coins"])


def test_rates_from_csv_pick_the_latest_dated_row(tmp_path):
    csv_file = tmp_path / "rates.csv"
    csv_file.write_text("date,rate\n2023-11-10,100\n2023-11-16,200\n# comment\n")
    rates = Rates.from_csv("EUR", csv_file)
    assert rates.rate_on(block_time(0)) == 100  # 2023-11-14
    assert rates.rate_on(block_time(300)) == 200  # 2023-11-17
    assert rates.value(150_000_000, block_time(300)) == "300.00"
    assert Rates.from_csv("EUR", csv_file).rate_on(0) is None  # 1970: before the table
    (tmp_path / "empty.csv").write_text("date,rate\n")
    with pytest.raises(ValueError, match="no date,rate rows"):
        Rates.from_csv("EUR", tmp_path / "empty.csv")


def test_cli_end_to_end_with_fake_node(tmp_path, wallet, signer_expressions, monkeypatch, capsys):
    import bip322report.cli as cli_module

    node = _node(wallet, tip=1012)
    ledger = tmp_path / "ledger"
    _bundle(ledger / "one", _node(wallet, tip=1000), wallet, signer_expressions)
    monkeypatch.setattr(cli_module, "BitcoinCli", lambda command: node)
    monkeypatch.chdir(tmp_path)

    assert cli_module.main(["block", "2023-11-15"]) == 0
    assert json.loads(capsys.readouterr().out)["height"] == last_block_before(node, parse_when("2023-11-15")).height
    assert cli_module.main(["-w", "watch", "history", "-o", "history.json"]) == 0
    out, err = capsys.readouterr()
    assert (
        out == ""
        and json.loads(err.strip().splitlines()[-1])["transactions"] == 5
        and History.load(Path("history.json")).balance_at(1012) == 63_900_000
    )
    assert cli_module.main(["balance", "--history", "history.json", "--height", "1000"]) == 0
    assert json.loads(capsys.readouterr().out)["total_btc"] == "0.85000000"
    assert cli_module.main(["balance", "--at", "2023-11-21T10:00:00Z"]) == 0
    assert json.loads(capsys.readouterr().out)["block"]["height"] == last_block_before(node, parse_when("2023-11-21T10:00:00Z")).height

    assert (
        cli_module.main(
            [
                "report",
                "--history",
                "history.json",
                "--from-height",
                "1000",
                "--to-height",
                "1012",
                "--ledger",
                str(ledger),
                "-o",
                "out",
            ]
        )
        == 1  # ATTENTION: two closing outputs have no proof
    )
    out, err = capsys.readouterr()
    assert out.strip() == "out" and "RESULT: ATTENTION" in err and "proof coverage: 1/3" in err
    written = json.loads(Path("out/out.json").read_text())
    assert (
        written["label"] == "watch"
        and not written["coverage"]["complete"]
        and Path("out/out.html").exists()
        and Path("out/out.csv").exists()
    )
    assert (
        Path("out/ledger/one/proofs.json").read_bytes() == (ledger / "one" / "proofs.json").read_bytes()
    )  # what the reader gets, no PSBTs
    assert not any(p.suffix == ".psbt" for p in Path("out").rglob("*")) and written["bundles"][0]["bundle"] == "one/proofs.json"
    assert (
        cli_module.main(["report", "--history", "history.json", "--from-height", "1000", "--to-height", "1012", "-o", "out"]) == 2
    )  # not empty
    assert "not empty" in capsys.readouterr().err
    assert cli_module.main(["report", "--history", "history.json", "--from-height", "1000", "-o", "x"]) == 2  # incomplete period
    assert cli_module.main(["report", "--history", "history.json", "-o", "x"]) == 2
    # the fake chain lives in November 2023: with the clock set there, the year runs to the tip
    monkeypatch.setattr("bip322report.period._now", lambda: datetime.fromtimestamp(block_time(1012) + 60, UTC))
    assert cli_module.main(["report", "--history", "history.json", "--year", "2023", "-o", "y", "--rate", "5", "--currency", "USD"]) == 1
    capsys.readouterr()
    written = json.loads(Path("y/y.json").read_text())
    assert (
        written["period"]["label"] == "2023"
        and written["fiat"] == {"currency": "USD", "source": "constant 5 USD/BTC"}
        and written["ok"] is False
    )
    assert cli_module.main(["help", "report"]) == 0 and "Examples:" in capsys.readouterr().out


def test_node_check_that_cannot_run_is_not_a_failure(tmp_path, wallet):
    from bip322audit.rpc import RpcError

    node = _node(wallet, tip=1012)
    history = fetch_history(node)
    original = node.call

    def call(method, *params):
        if method == "listunspent":
            raise RpcError("Multiple wallets are loaded")
        return original(method, *params)

    node.call = call
    report = build_report(history, period_for_heights(node, 1000, 1012), label="x", cli=node)
    assert report["node_check"] == {"ok": None, "reason": "Multiple wallets are loaded"} and report["coverage"]["complete"] is False
    assert "node check (listunspent vs closing coins): not run (Multiple wallets are loaded)" in format_summary(report)
    history_only = build_report(history, period_for_heights(node, 1000, 1012), label="x", cli=None)
    assert history_only["node_check"] is None


def test_pdf_without_weasyprint_explains_itself(tmp_path, monkeypatch):
    import sys

    from bip322report.render import write_pdf

    monkeypatch.setitem(sys.modules, "weasyprint", None)  # makes `from weasyprint import HTML` raise ImportError
    with pytest.raises(RuntimeError, match=r"bip322-report\[pdf\]"):
        write_pdf("<html></html>", tmp_path / "x.pdf")


def test_change_address_proven_before_the_spend_covers_the_change_output(tmp_path, wallet, signer_expressions):
    """The owner's flow: prove the change address, then broadcast; the report covers the change by that proof."""
    ledger = tmp_path / "ledger"
    before = _node(wallet, tip=1000)
    _bundle(ledger / "all", before, wallet, signer_expressions)
    change = wallet.derive(1, 1).address  # a2: where the 0.29 change of the spend at 1005 will land
    ahead = _bundle(ledger / "change-ahead", before, wallet, signer_expressions, addresses=[change])
    assert ahead["proofs"][0]["address"] == change and ahead["proofs"][0]["utxos"] == []

    node = _node(wallet, tip=1020)
    history = fetch_history(node)
    report = build_report(history, period_for_heights(node, 1000, 1020), label="T", ledger_roots=[ledger], cli=node)
    by_out = {(c["txid"][:2], c["vout"]): c["proof"] for c in report["closing"]["coins"]}
    dd = by_out[("dd", 1)]
    assert dd["verified"] and dd["bundle"] == "change-ahead/proofs.json" and dd["before_output"] and not dd["lists_output"]
    assert not dd["after_period"] and report["coverage"]["covered_after_period_count"] == 0  # every proof here predates block 1020
    # before a year-end bundle, only a3 (the internal move's destination) is left to prove
    snapshot, _ = take_snapshot(node, wallet, "x", skip_addresses=proven_addresses([ledger]))
    assert [a["address"] for a in snapshot.addresses] == [wallet.derive(1).address]
    later = _bundle(ledger / "year-end", _node(wallet, tip=1040), wallet, signer_expressions, "Proof of control, audit FY2023, {date}")
    assert int(later["stamp"]["height"]) == 1034
    node40 = _node(wallet, tip=1040)
    report40 = build_report(fetch_history(node40), period_for_heights(node40, 1000, 1020), label="T", ledger_roots=[ledger], cli=node40)
    assert report40["coverage"]["covered_after_period_count"] == 3 and "stamped after the period's end: 3/3" in format_summary(report40)
    assert all(c["proof"]["after_period"] and c["proof"]["bundle"] == "year-end/proofs.json" for c in report40["closing"]["coins"])
    assert "Attention" not in render_html(report40) and "Attention" in render_html({**report40, "ok": False})
    assert by_out[("bb", 0)]["lists_output"] and not by_out[("bb", 0)]["before_output"]
    assert by_out[("ee", 0)] is None  # the internal move went to a3, never proven
    html = render_html(report)
    assert "The proof predates this UTXO" in html and "verifymessage" in html and "VALID" in html
    by_addr = {a["address"]: a for a in report["closing"]["addresses"]}
    assert (
        by_addr[wallet.derive(1, 1).address]["proof"]["before_coins"] == "all"
        and by_addr[wallet.derive(1, 1).address]["total_sat"] == 29_000_000
    )
    assert report["coverage"]["addresses_total"] == 3 and report["coverage"]["addresses_covered"] == 2
    # after the year-end bundle nothing is left to prove
    with pytest.raises(Exception, match="already proven"):
        take_snapshot(node40, wallet, "x", skip_addresses=proven_addresses([ledger]))


def test_handbook_ships_with_the_package(capsys):
    from bip322report import cli as cli_module

    assert cli_module.main(["handbook"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# The yearly proof of control, end to end") and "auditor" in out


def _proven(tmp_path, wallet, signers):
    """A ledger whose two bundles (stamps 994 and 1014) cover the three closing UTXOs at height 1020, and the node at that tip."""
    ledger = tmp_path / "ledger"
    _bundle(ledger / "snapshot-1", _node(wallet, tip=1000), wallet, signers)
    node = _node(wallet, tip=1020)
    _bundle(ledger / "snapshot-2", node, wallet, signers, skip=proven_addresses([ledger]))
    return ledger, node


def test_a_failed_lookup_does_not_hide_a_contradiction(tmp_path, wallet, signer_expressions, monkeypatch):
    from bip322audit.rpc import RpcError

    import bip322report.report as report_module

    ledger, node = _proven(tmp_path, wallet, signer_expressions)
    history, period = fetch_history(node), period_for_heights(node, 1000, 1020)
    assert build_report(history, period, label="T", ledger_roots=[ledger], cli=node)["ok"]
    real = report_module.holdings

    def lookups(fail_at, contradict):
        def holdings(cli, targets, at=None):
            if at == fail_at:
                raise RpcError("gettxout timed out")
            result = real(cli, targets, at=at)
            for o in result["outputs"]:
                o["amount_sat"] += contradict
            return result

        return holdings

    monkeypatch.setattr(report_module, "holdings", lookups(994, 1))  # bb's lookup fails; the chain disagrees on dd and ee
    report = build_report(history, period, label="T", ledger_roots=[ledger], cli=node)
    oc = report["onchain"]
    assert (oc["contradicted"], oc["not_run"], oc["matches"], oc["error"]) == (2, 1, 0, "gettxout timed out")
    assert not report["ok"] and report["coverage"]["complete"] and report["reconciliation"]["ok"]
    summary = format_summary(report)
    assert "0/3 held when proven, 2 CONTRADICTED, 1 not run (gettxout timed out)" in summary and "RESULT: ATTENTION" in summary
    html = render_html(report)
    assert "Attention" in html and "2 contradicted · 1 not run" in html and html.count("CONTRADICTED") == 2

    monkeypatch.setattr(report_module, "holdings", lookups(994, 0))  # a lookup that could not run is not a contradiction
    report = build_report(history, period, label="T", ledger_roots=[ledger], cli=node)
    assert report["ok"] and "2/3 held when proven, 1 not run (gettxout timed out)" in format_summary(report)
    assert "Attention" not in render_html(report) and "NOT RUN" in render_html(report)


def test_report_built_without_a_node_renders(tmp_path, wallet, signer_expressions):
    ledger = tmp_path / "ledger"
    node = _node(wallet, tip=1000)
    _bundle(ledger / "b", node, wallet, signer_expressions)
    report = build_report(fetch_history(node), period_for_heights(node, 979, 1000), label="x", ledger_roots=[ledger], cli=None)
    assert report["onchain"] is None and all(c["onchain"] is None for c in report["closing"]["coins"])
    html = render_html(report)
    assert html.count("NOT RUN") == 3 and "not run when this statement was prepared" in html and "Attention" not in html
    assert f"{'aa' * 32}:0 --at 994" in html  # the lookup the reader runs: at the block of the proof
    assert "on chain" not in format_summary(report) and "node check" not in format_summary(report)
    bare = build_report(fetch_history(node), period_for_heights(node, 979, 1000), label="x", cli=None)  # no ledger either
    assert f"{'aa' * 32}:0 --at 1000" in render_html(bare) and "NO PROOF" in render_html(bare)


def test_reference_depends_only_on_the_facts_stated(tmp_path, wallet, signer_expressions):
    import copy

    from bip322report.report import _report_id

    ledger, _ = _proven(tmp_path, wallet, signer_expressions)
    reports = []
    for tip, with_node in ((1040, True), (1041, True), (1041, False)):
        node = _node(wallet, tip=tip)
        history, period = fetch_history(node), period_for_heights(node, 1000, 1020)
        reports.append(build_report(history, period, label="T", ledger_roots=[ledger], cli=node if with_node else None))
    at_1040, at_1041, offline = reports
    assert at_1040["closing"]["coins"][0]["onchain"]["tip"] == 1040 and at_1041["closing"]["coins"][0]["onchain"]["tip"] == 1041
    assert at_1040["report_id"] == at_1041["report_id"] == offline["report_id"]  # a new block, or no node, states nothing new
    assert (
        _report_id({**at_1040, "generated_utc": "later", "history_fetched_utc": "later", "tool": "another version"}) == at_1040["report_id"]
    )

    def changed(edit) -> str:
        other = copy.deepcopy(at_1040)
        edit(other)
        return _report_id(other)

    ids = {
        at_1040["report_id"],
        changed(lambda r: r["closing"]["coins"][0].update(amount_sat=1)),
        changed(lambda r: r["closing"]["coins"][0]["proof"].update(signature="smpAAAA")),
        changed(lambda r: r["closing"]["coins"][0]["proof"].update(bundle="elsewhere/proofs.json")),
        changed(lambda r: r["closing"]["coins"][0].update(proof=None)),
        changed(lambda r: r["opening"]["coins"][0].update(address="bc1qother")),
        changed(lambda r: r["transactions"][0].update(fee_sat=1)),
        changed(lambda r: r["period"]["end"].update(hash="00" * 32)),
        changed(lambda r: r.update(label="U")),
    }
    assert len(ids) == 9


def test_node_check_runs_only_at_the_historys_tip(wallet):
    from html import unescape

    node = _node(wallet, tip=1012)
    history, period = fetch_history(node), period_for_heights(node, 1000, 1012)
    assert build_report(history, period, label="x", cli=node)["node_check"]["ok"] is True

    node.tip_height = 1013  # the history is a block old: listunspent now says nothing about the coins at 1012
    node.add("12" * 32, 1013, [("dd" * 32, 1)], [(EXTERNAL, 28_990_000)])
    report = build_report(history, period, label="x", cli=node)
    assert report["node_check"] == {"ok": None, "reason": "the node's tip is not the history's (block 1012)"}
    assert "node check (listunspent vs closing coins): not run (the node's tip is not the history's (block 1012))" in format_summary(report)
    assert "not run · the node's tip is not the history's (block 1012)" in unescape(render_html(report))  # the checklist: no proofs here

    node = _node(wallet, tip=1012)  # a block arrives while the check runs
    original = node.call

    def call(method, *params):
        out = original(method, *params)
        if method == "listunspent":
            node.tip_height = 1013
        return out

    node.call = call
    report = build_report(history, period, label="x", cli=node)
    assert report["node_check"] == {"ok": None, "reason": "a block arrived during the check"}

    assert (
        build_report(history, period_for_heights(node, 1000, 1011), label="x", cli=node)["node_check"] is None
    )  # not at the tip: no check


def test_node_check_tells_a_mempool_spend_from_a_mismatch(tmp_path, wallet, signer_expressions):
    ledger, _ = _proven(tmp_path, wallet, signer_expressions)
    node = _node(wallet, tip=1020)
    node.hide_mempool_spent = True  # ff, unconfirmed, spends bb:0; a node wallet then leaves bb:0 out of listunspent
    history, period = fetch_history(node), period_for_heights(node, 1000, 1020)
    report = build_report(history, period, label="x", ledger_roots=[ledger], cli=node)
    nc = report["node_check"]
    assert nc["ok"] is True and nc["spent_in_mempool"] == [f"{'bb' * 32}:0"] and nc["not_in_listunspent"] == [] and report["ok"]
    assert (nc["listunspent_count"], nc["closing_count"]) == (2, 3) and ("gettxout", "bb" * 32, 0, False) in node.calls
    assert "node check (listunspent vs closing coins): ok, 1 spent by an unconfirmed transaction" in format_summary(report)
    assert "Attention" not in render_html(report)

    # an output the chain itself says is spent, in a transaction the history lacks, is a mismatch
    node.add("12" * 32, 1020, [("dd" * 32, 1)], [(EXTERNAL, 28_990_000)])
    report = build_report(history, period, label="x", ledger_roots=[ledger], cli=node)
    nc = report["node_check"]
    assert nc["ok"] is False and nc["not_in_listunspent"] == [f"{'dd' * 32}:1"] and nc["spent_in_mempool"] == [f"{'bb' * 32}:0"]
    assert not report["ok"] and f"MISMATCH: not in listunspent {'dd' * 32}:1" in format_summary(report)
    html = render_html(report)
    assert "Attention" in html and "1 of 3 · 1 spent by an unconfirmed transaction" in html
    assert html.count("✗") == 1  # the node check alone: proofs dated before the period's end and a UTXO spent since are no failed checks

    # an output listunspent has and the statement lacks is a mismatch too
    node = _node(wallet, tip=1020)
    node.add("13" * 32, 1020, [(EXTERNAL2, 5)], [(wallet.derive(0).address, 7_000_000)])
    nc = build_report(history, period, label="x", cli=node)["node_check"]
    assert nc["ok"] is False and nc["missing_from_report"] == [f"{'13' * 32}:0"] and nc["not_in_listunspent"] == []


def test_node_check_does_not_call_an_immature_coinbase_spent(wallet):
    node = _node(wallet, tip=1012)
    history, period = fetch_history(node), period_for_heights(node, 1000, 1012)
    original = node.call

    def call(method, *params):  # ee:0 as a coinbase output 3 blocks deep: listunspent leaves it out until it matures
        out = original(method, *params)
        if method == "listunspent":
            out = [r for r in out if r["txid"] != "ee" * 32]
        if method == "gettxout" and out and params[0] == "ee" * 32:
            out = {**out, "coinbase": True}
        return out

    node.call = call
    nc = build_report(history, period, label="x", cli=node)["node_check"]
    assert nc["ok"] is True and nc["immature"] == [f"{'ee' * 32}:0"] and nc["spent_in_mempool"] == [] and nc["not_in_listunspent"] == []


def test_spent_since_names_the_spending_transaction(tmp_path, wallet, signer_expressions):
    """Finding 5's case: a period closed at 1000, proven at 994, two of its three UTXOs spent before the statement is made."""
    ledger = tmp_path / "ledger"
    _bundle(ledger / "all", _node(wallet, tip=1000), wallet, signer_expressions)
    node = _node(wallet, tip=1020)
    period = period_for_heights(node, 985, 1000)
    report = build_report(fetch_history(node), period, label="T", ledger_roots=[ledger], cli=node)
    rows = {c["txid"][:2]: c for c in report["closing"]["coins"]}
    assert {k: r["onchain"]["status"] for k, r in rows.items()} == {"aa": "spent_since", "bb": "matches", "cc": "spent_since"}
    assert rows["aa"]["spent_by"] == {"txid": "dd" * 32, "height": 1005, "time_utc": "2023-11-21T21:43:20Z"}
    assert rows["cc"]["spent_by"]["txid"] == "ee" * 32 and rows["cc"]["spent_by"]["height"] == 1010 and rows["bb"]["spent_by"] is None
    html = render_html(report)
    assert html.count("SPENT SINCE") == 2 and "the spending transaction is in" not in html
    assert (
        "spent after the period, at block 1005, by" in html
        and html.count("dd" * 32) >= 1
        and "spent after the period, at block 1010" in html
    )
    # shown on the UTXO's page, as is a proof stamped before the period's end; neither is a failed check
    assert report["ok"] and report["coverage"]["covered_after_period_count"] == 0 and "Attention" not in html
    assert "1/3 held when proven, 2 spent since" in format_summary(report) and "RESULT: OK" in format_summary(report)

    stale = build_report(
        fetch_history(_node(wallet, tip=1000)), period, label="T", ledger_roots=[ledger], cli=node
    )  # read before the spends
    rows = {c["txid"][:2]: c for c in stale["closing"]["coins"]}
    assert rows["aa"]["onchain"]["status"] == "spent_since" and rows["aa"]["spent_by"] is None
    assert "spent after the history for this statement was read" in render_html(stale)
    assert stale["report_id"] == report["report_id"]  # what happened after the period is not among the period's facts


def test_a_proof_that_fails_the_statements_own_verification_is_not_ok(tmp_path, wallet, signer_expressions, monkeypatch):
    """A page said INVALID while the result said OK: the bundle had verified, the statement's own check by script had not."""
    import bip322report.report as report_module

    node = _node(wallet, tip=1000)
    ledger = tmp_path / "ledger"
    _bundle(ledger / "snapshot-1", node, wallet, signer_expressions, "Owner proof {date}")
    real, calls = report_module.verify_message, []

    class Failed:
        def __init__(self, verdict):
            self.verdict = verdict

        def to_dict(self):
            return {**self.verdict.to_dict(), "state": "invalid"}

    def first_fails(spk, signature, message, **kwargs):
        calls.append(spk)
        verdict = real(spk, signature, message, **kwargs)
        return Failed(verdict) if len(calls) == 1 else verdict

    monkeypatch.setattr(report_module, "verify_message", first_fails)
    report = build_report(fetch_history(node), period_for_heights(node, 985, 1000, label="t"), label="T", ledger_roots=[ledger], cli=node)
    cov = report["coverage"]
    assert cov["covered_count"] < cov["total_count"] and cov["addresses_covered"] < cov["addresses_total"]
    assert not cov["complete"] and not report["ok"] and "ATTENTION" in render_html(report)
    # the counts by output and by address agree: an address is covered exactly when its outputs are
    for a in report["closing"]["addresses"]:
        assert all(o["proof"]["verified"] == a["proof"]["verified"] for o in a["outputs"])
