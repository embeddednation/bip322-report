"""The holder's ledger: several wallets merged, a transfer between them one movement, both sides named."""

import csv
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from bip322audit.rpc import BitcoinCli
from fake_node import FakeNode, block_time

import bip322report.cli as cli_module
from bip322report.history import History, fetch_history
from bip322report.holder import merge_histories
from bip322report.period import period_for_heights
from bip322report.render import render_html, write_csv
from bip322report.report import build_report, format_summary

OTHER = "bc1qexternal0000000000000000000000000000000ext"
EXCHANGE = "bc1qexchange0000000000000000000000000000000xch"
A, B, C, D = ("a1" * 32, "b2" * 32, "c3" * 32, "d4" * 32)


def _two_wallets(wallet) -> tuple[dict[str, FakeNode], list[str]]:
    """treasury (a0, a1) and cold (c0, c1) on two node wallets: a receive, a transfer, a send, a consolidation across both."""
    a0, a1, c0, c1 = (wallet.derive(i, b).address for i, b in ((0, 0), (1, 0), (0, 1), (1, 1)))
    treasury, cold = FakeNode(tip=1010, wallet_name="treasury"), FakeNode(tip=1010, wallet_name="cold")
    treasury.mine |= {a0, a1}
    cold.mine |= {c0, c1}
    treasury.others[(OTHER, 0)] = EXCHANGE
    treasury.add(A, 1001, [(OTHER, 0)], [(a0, 100_000)])  # received from the exchange
    for node in (treasury, cold):  # the transfer: both wallets see it
        node.add(B, 1003, [(A, 0)], [(c0, 70_000), (a1, 29_000)])  # fee 1,000
    cold.others[(A, 0)] = a0
    cold.add(C, 1005, [(B, 0)], [(OTHER, 20_000), (c1, 49_500)])  # cold pays the exchange; fee 500
    for node in (treasury, cold):
        node.add(D, 1007, [(B, 1), (C, 1)], [(a0, 78_000)])  # both wallets' coins into treasury; fee 500
    return {"treasury": treasury, "cold": cold}, [a0, a1, c0, c1]


def csv_blocks(path: Path) -> dict[str, list[dict]]:
    """A holder's CSV: the wallet named above each block, its rows as dicts."""
    blocks = {}
    for block in path.read_text().strip().split("\n\n"):
        name, *rows = block.splitlines()
        blocks[name] = list(csv.DictReader(rows))
    return blocks


def test_merged_histories_make_one_ledger(wallet, tmp_path):
    nodes, (a0, a1, c0, c1) = _two_wallets(wallet)
    histories = {name: fetch_history(node) for name, node in nodes.items()}
    assert [tx.kind for tx in histories["treasury"].txs] == ["receive", "send", "mixed"]  # each wallet's own view
    assert [tx.kind for tx in histories["cold"].txs] == ["receive", "send", "mixed"]
    merged = merge_histories(histories)
    assert [(tx.txid[:2], tx.kind, tx.net_sat, tx.fee_sat, tx.others_in) for tx in merged.txs] == [
        ("a1", "receive", 100_000, None, 1),
        ("b2", "transfer", -1_000, 1_000, 0),
        ("c3", "send", -20_500, 500, 0),
        ("d4", "transfer", -500, 500, 0),
    ]
    assert [tx.payers for tx in merged.txs] == [[EXCHANGE], [], [], []]  # the transfer's payer is a wallet of the holder's, not a payer
    assert {c.wallet for tx in merged.txs for c in tx.ours_in + tx.ours_out} == {"treasury", "cold"}
    assert merged.balance_at(1010) == 78_000 and [c.wallet for c in merged.coins_at(1010)] == ["treasury"]
    again = History.from_dict(json.loads(json.dumps(merged.to_dict())))
    assert [tx.kind for tx in again.txs] == ["receive", "transfer", "send", "transfer"] and again.to_dict() == merged.to_dict()
    names = {EXCHANGE: "Exchange", OTHER: "Exchange"}
    report = build_report(
        merged, period_for_heights(nodes["cold"], 1000, 1010), label="Company AB", cli=None, addresses=names, wallets=["cold", "treasury"]
    )
    assert [t["counterpart"] for t in report["transactions"]] == [
        "Exchange -> treasury",
        "treasury -> cold",
        "cold -> Exchange",
        "cold -> treasury",
    ]
    cold, treasury = report["wallets"]
    assert (cold["name"], cold["opening_sat"], cold["closing_sat"]) == ("cold", 0, 0)
    assert (treasury["name"], treasury["opening_sat"], treasury["closing_sat"], treasury["closing_btc"]) == (
        "treasury",
        0,
        78_000,
        "0.00078000",
    )
    # each wallet's own view of the same movements: its net, its own running balance, the other side plain
    assert [(t["txid"][:2], t["kind"], t["net_sat"], t["balance_after_sat"], t["counterpart"]) for t in treasury["transactions"]] == [
        ("a1", "receive", 100_000, 100_000, "Exchange"),
        ("b2", "transfer", -71_000, 29_000, "cold"),
        ("d4", "transfer", 49_000, 78_000, "cold"),
    ]
    assert [(t["txid"][:2], t["kind"], t["net_sat"], t["balance_after_sat"], t["counterpart"]) for t in cold["transactions"]] == [
        ("b2", "transfer", 70_000, 70_000, "treasury"),
        ("c3", "send", -20_500, 49_500, "Exchange"),
        ("d4", "transfer", -49_500, 0, "treasury"),
    ]
    assert report["totals"]["fees_sat"] == 2_000 and report["reconciliation"]["ok"] and report["node_check"] is None
    assert "wallets: cold 0.00000000 BTC, treasury 0.00078000 BTC" in format_summary(report)
    write_csv(report, tmp_path / "h.csv")
    blocks = csv_blocks(tmp_path / "h.csv")
    assert list(blocks) == ["cold", "treasury"]
    assert [(r["category"], r["amount"], r["balance"], r["wallet"]) for r in blocks["treasury"]] == [
        ("receive", "0.00100000", "0.00100000", "Exchange"),
        ("transfer", "-0.00071000", "0.00029000", "cold"),
        ("transfer", "0.00049000", "0.00078000", "cold"),
    ]
    assert [(r["category"], r["amount"], r["balance"], r["wallet"]) for r in blocks["cold"]] == [
        ("transfer", "0.00070000", "0.00070000", "treasury"),
        ("send", "-0.00020500", "0.00049500", "Exchange"),
        ("transfer", "-0.00049500", "0.00000000", "treasury"),
    ]
    assert (tmp_path / "h.csv").read_text().startswith("cold\ntime,txid,category,amount,balance,wallet\n")
    html = render_html(report)
    assert "From → to" in html and "treasury → cold" in html and "<td>treasury</td>" in html and "Wallets of the holder" in html
    # one wallet alone is still its own view: no transfer, no wallet column
    alone = build_report(histories["treasury"], period_for_heights(nodes["treasury"], 1000, 1010), label="treasury", cli=None)
    assert alone["wallets"] is None and "From → to" not in render_html(alone)
    with pytest.raises(ValueError, match="different chains"):
        merge_histories({"a": histories["treasury"], "b": History("test", None, 1, "00", "", [], [])})


class _Switch(BitcoinCli):
    """The node the CLI builds: -rpcwallet=NAME in its argv picks the fake node wallet to answer."""

    def __init__(self, nodes: dict[str, FakeNode]):
        super().__init__(["bitcoin-cli"])
        self.nodes = nodes

    def call(self, method, *params):
        name = next((a.removeprefix("-rpcwallet=") for a in self.argv if a.startswith("-rpcwallet=")), None)
        return self.nodes[name].call(method, *params)


def test_the_holder_command(wallet, tmp_path, monkeypatch, capsys):
    """bip322 report holder: the holder is a directory, its wallets the directories under it, the ledger to date in its statements/."""
    nodes, _ = _two_wallets(wallet)
    monkeypatch.setattr(cli_module, "BitcoinCli", lambda command: _Switch(nodes))
    monkeypatch.setattr(cli_module, "_pdf_possible", lambda: False)
    root = Path(os.environ["BIP322_DIR"])
    for name in ("treasury", "cold"):
        (root / "company" / name / "snapshots").mkdir(parents=True)
    (root / "bip322.toml").write_text(
        'explorer = ""\n[holders.company]\nname = "Company AB"\nexplorer = "https://example.org"\n'  # the holder's beats the top level
        f'\n[addresses]\n"{EXCHANGE}" = "Exchange"\n"{OTHER}" = "Exchange"\n'
    )
    monkeypatch.chdir(root)
    assert cli_module.main(["holder"]) == 1  # the one holder needs no naming; no proofs: ATTENTION, the ledger is written all the same
    out, err = capsys.readouterr()
    date = datetime.fromtimestamp(block_time(1010), UTC).strftime("%Y-%m-%d")
    assert out.strip() == str(root / "company" / "statements" / date) and "wallets: cold 0.00000000 BTC, treasury 0.00078000 BTC" in err
    directory = Path(out.strip())
    assert sorted(p.name for p in directory.iterdir()) == [f"company-{date}.csv", f"company-{date}.html", f"company-{date}.json"]
    written = json.loads((directory / f"company-{date}.json").read_text())
    assert written["label"] == "Company AB" and [w["name"] for w in written["wallets"]] == ["cold", "treasury"]
    assert written["period"]["start"]["height"] == 1000 and written["period"]["end"]["height"] == 1010 and written["period"]["to_tip"]
    blocks = csv_blocks(directory / f"company-{date}.csv")
    assert [(r["category"], r["wallet"]) for r in blocks["treasury"]] == [
        ("receive", "Exchange"),
        ("transfer", "cold"),
        ("transfer", "cold"),
    ]
    assert [(r["category"], r["wallet"]) for r in blocks["cold"]] == [
        ("transfer", "treasury"),
        ("send", "Exchange"),
        ("transfer", "treasury"),
    ]
    assert blocks["treasury"][-1]["balance"] == "0.00078000" and blocks["cold"][-1]["balance"] == "0.00000000"
    assert "https://example.org/tx/" in (directory / f"company-{date}.html").read_text()  # explorer from [holders.company]
    # a wallet's statement takes its holder's settings too
    assert cli_module.main(["report", "-w", "treasury", "--from-height", "1004", "--to-height", "1010", "-o", str(tmp_path / "t")]) == 1
    capsys.readouterr()
    assert "https://example.org/tx/" in (tmp_path / "t" / "treasury-1004..1010.html").read_text()
    # a period, as for a wallet's statement; the holder named by its directory
    assert cli_module.main(["holder", "company", "--from-height", "1004", "--to-height", "1010"]) == 1
    out, _ = capsys.readouterr()
    assert out.strip() == str(root / "company" / "statements" / "1004..1010")
    # a second holder: cold moves under it; then the holder must be named, or be the current directory's, or bip322.toml's
    (root / "jonas").mkdir()
    (root / "company" / "cold").rename(root / "jonas" / "cold")
    assert (
        cli_module.main(["holder"]) == 2
        and "which holder? bip322 report holder KEY (holders here: company, jonas)" in capsys.readouterr().err
    )
    assert cli_module.main(["holder", "jonas", "-o", str(tmp_path / "jonas")]) == 0  # cold holds nothing at the end: nothing to cover
    written = json.loads((tmp_path / "jonas" / f"jonas-{date}.json").read_text())
    assert written["label"] == "jonas" and [w["name"] for w in written["wallets"]] == ["cold"]
    monkeypatch.chdir(root / "jonas" / "cold")
    assert cli_module.main(["holder", "-o", str(tmp_path / "here")]) == 0 and (tmp_path / "here" / f"jonas-{date}.json").exists()
    monkeypatch.chdir(root)
    (root / "bip322.toml").write_text('holder = "jonas"\n')
    assert cli_module.main(["holder", "-o", str(tmp_path / "default")]) == 0 and (tmp_path / "default" / f"jonas-{date}.json").exists()
    assert cli_module.main(["holder", "nobody", "-o", str(tmp_path / "nobody")]) == 2
    assert f"no wallet under {root / 'nobody'} yet" in capsys.readouterr().err
    (root / "jonas" / "treasury" / "snapshots").mkdir(parents=True)  # the same wallet name under two holders
    assert (
        cli_module.main(["holder", "company"]) == 2 and "wallet treasury is under two holders, company and jonas" in capsys.readouterr().err
    )
