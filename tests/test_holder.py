"""The holder's ledger: several wallets merged, a transfer between them one movement, both sides named."""

import csv
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from bip322audit.rpc import BitcoinCli, RpcError
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
    treasury.others[(C, 1)] = c1  # the block's undo data: each node can tell the other wallet's input
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


def _chunked(txid: str) -> str:
    return "<wbr>".join(txid[i : i + 8] for i in range(0, len(txid), 8))


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
    assert [(t["wallet"], t["counterpart"]) for t in report["transactions"]] == [
        ("treasury", "Exchange"),
        ("treasury -> cold", ""),
        ("cold", "Exchange"),
        ("cold -> treasury", ""),
    ]
    treasury, cold = report["wallets"]  # oldest first: treasury received before cold did
    assert (cold["name"], cold["opening_sat"], cold["closing_sat"]) == ("cold", 0, 0)
    # what each wallet received and sent (a move between the holder's wallets is sent by one, received by the other, for the
    # same amount, the fee with the sender) and paid in fees: D's fee (500) is split pro rata to what each put in (29,000
    # and 49,500), the odd satoshi to the larger, so cold sends 49,184 to treasury and treasury receives 49,184
    keys = ("received", "sent", "fees", "mixed", "moved_in", "moved_out")
    assert {k: treasury[f"{k}_sat"] for k in keys} == {
        "received": 149_184, "sent": 70_000, "fees": 1_184, "mixed": 0, "moved_in": 49_184, "moved_out": 70_000,
    }  # fmt: skip
    assert {k: cold[f"{k}_sat"] for k in keys} == {
        "received": 70_000, "sent": 69_184, "fees": 816, "mixed": 0, "moved_in": 70_000, "moved_out": 49_184,
    }  # fmt: skip
    for w in (treasury, cold):
        assert w["opening_sat"] + w["received_sat"] - w["sent_sat"] - w["fees_sat"] + w["mixed_sat"] == w["closing_sat"]
    totals = report["wallets_totals"]
    assert (totals["received_sat"], totals["sent_sat"], totals["fees_sat"], totals["closing_btc"]) == (
        219_184,
        139_184,
        2_000,
        "0.00078000",
    )
    assert totals["moved_in_sat"] == totals["moved_out_sat"] == 119_184 and totals["amount_sat"] == 80_000
    assert (treasury["name"], treasury["opening_sat"], treasury["closing_sat"], treasury["closing_btc"]) == (
        "treasury",
        0,
        78_000,
        "0.00078000",
    )

    # each wallet's own view of the same movements: the amount before the fee, its fee, its net, its own running balance,
    # the other side plain; a move between the wallets pairs by txid and amount
    def rows(w):
        return [
            (t["txid"][:2], t["kind"], t["amount_sat"], t["fee_sat"], t["net_sat"], t["balance_after_sat"], t["counterpart"])
            for t in w["transactions"]
        ]

    assert rows(treasury) == [
        ("a1", "receive", 100_000, None, 100_000, 100_000, "Exchange"),
        ("b2", "send", -70_000, 1_000, -71_000, 29_000, "cold"),
        ("d4", "receive", 49_184, 184, 49_000, 78_000, "cold"),
    ]
    assert rows(cold) == [
        ("b2", "receive", 70_000, None, 70_000, 70_000, "treasury"),
        ("c3", "send", -20_000, 500, -20_500, 49_500, "Exchange"),
        ("d4", "send", -49_184, 316, -49_500, 0, "treasury"),
    ]
    assert "n" not in report["transactions"][0] and "n" not in treasury["transactions"][0]
    assert report["totals"]["fees_sat"] == 2_000 and report["reconciliation"]["ok"] and report["node_check"] is None
    assert "wallets: treasury 0.00078000 BTC, cold 0.00000000 BTC" in format_summary(report)
    write_csv(report, tmp_path / "h.csv")
    blocks = csv_blocks(tmp_path / "h.csv")
    assert list(blocks) == ["treasury", "cold"]
    assert [(r["category"], r["amount"], r["balance"], r["wallet"]) for r in blocks["treasury"]] == [
        ("receive", "0.00100000", "0.00100000", "Exchange"),
        ("send", "-0.00071000", "0.00029000", "cold"),
        ("receive", "0.00049000", "0.00078000", "cold"),
    ]
    assert [(r["category"], r["amount"], r["balance"], r["wallet"]) for r in blocks["cold"]] == [
        ("receive", "0.00070000", "0.00070000", "treasury"),
        ("send", "-0.00020500", "0.00049500", "Exchange"),
        ("send", "-0.00049500", "0.00000000", "treasury"),
    ]
    assert (tmp_path / "h.csv").read_text().startswith("treasury\ntime,txid,category,amount,balance,wallet\n")
    html = render_html(report)
    # the summary: from the opening holdings to the closing, the holder's wallets each a line, the moves between them inside
    # received and sent; then each wallet a block of movements, a move between two of them one number on both sides
    assert (
        'Received from others</td><td class="num">0.00100000</td>' in html
        and 'Sent to others</td><td class="num">(0.00020000)</td>' in html
    )
    # each wallet's share under the opening and under the closing holdings; no sums per wallet on the page (the JSON has them)
    assert html.count('<tr class="sub"><td class="sub">treasury</td><td class="num">–</td>') == 1  # opening: nothing yet
    assert html.count('<tr class="sub"><td class="sub">cold</td><td class="num">–</td>') == 2  # opening and closing: nothing
    assert '<tr class="sub"><td class="sub">treasury</td><td class="num">0.00078000</td>' in html  # closing: everything
    assert html.index("Opening holdings") < html.index('<td class="sub">cold</td>') < html.index("Closing holdings")
    assert "<th>Wallet</th>" not in html and "Transfers" not in html and "0.00149184" not in html and "<td>Transfer</td>" not in html
    assert html.index("1.1 treasury</h3>") < html.index("1.2 cold</h3>")
    ids = [html.count(f'<td class="id"><span class="mono">{_chunked(x)}</span></td>') for x in (A, B, C, D)]
    assert ids == [1, 2, 1, 2]  # the moves between the wallets (B and D) on both sides, each transaction's id whole, in its column
    assert (
        html.index("Opening holdings")
        < html.index("At the end of the period")
        < html.index('<td class="sub">treasury</td><td class="num">0.00078000')
        < html.index("Closing holdings")
    )
    assert '<td>cold</td><td class="num">(0.00070000)</td><td class="num">(0.00001000)</td>' in html and "<th>Kind</th>" not in html
    assert '<td>treasury</td><td class="num">0.00070000</td><td class="num"></td>' in html
    # the id links to the transaction when an explorer is named; one page, for the one closing UTXO
    assert C not in html and "The transactions" not in html and html.count("Proof of control of the scriptPubKey") == 1
    linked = render_html(report, explorer="https://x")
    assert linked.count(f'<a class="mono" href="https://x/tx/{C}?showDetails=true">{_chunked(C)}</a>') == 1 and linked.count(C) == 1
    assert '<tr class="group"><td colspan="4">treasury</td></tr>' in html and html.count('<tr class="group">') == 1  # cold holds nothing
    # one wallet alone is still its own view: no transfer, no wallet column, no table of wallets
    alone = build_report(histories["treasury"], period_for_heights(nodes["treasury"], 1000, 1010), label="treasury", cli=None)
    assert alone["wallets"] is None and "<h3>1.1 " not in render_html(alone) and '<tr class="sub">' not in render_html(alone)
    with pytest.raises(ValueError, match="different chains"):
        merge_histories({"a": histories["treasury"], "b": History("test", None, 1, "00", "", [], [])})
    emptied = History(merged.chain, None, 900, "00" * 32, "2020-01-01T00:00:00Z", [], [])  # kept on file: its old tip does not cut
    assert merge_histories({**histories, "old": emptied}).tip_height == 1010
    assert merge_histories({"old": emptied, "older": History(merged.chain, None, 800, "11" * 32, "", [], [])}).tip_height == 800


class _Switch(BitcoinCli):
    """The node the CLI builds: -rpcwallet=NAME in its argv picks the fake node wallet to answer."""

    def __init__(self, nodes: dict[str, FakeNode], chain: FakeNode | None = None):
        super().__init__(["bitcoin-cli"])
        self.nodes = nodes
        self.node = chain  # answers the node-level calls when no wallet is named (any node would)

    def call(self, method, *params):
        name = next((a.removeprefix("-rpcwallet=") for a in self.argv if a.startswith("-rpcwallet=")), None)
        if name is None:  # no wallet named: the node itself answers
            return (self.node or next(iter(self.nodes.values()))).call(method, *params)
        if name not in self.nodes:
            raise RpcError(f"{method}: error code: -18 error message: Requested wallet does not exist or is not loaded")
        return self.nodes[name].call(method, *params)


def test_the_holder_command(wallet, tmp_path, monkeypatch, capsys):
    """bip322 report holder: the holder is a directory, its wallets the directories under it, the ledger to date in its statements/."""
    nodes, _ = _two_wallets(wallet)
    chain = nodes["treasury"]  # the node itself, for the calls that name no wallet, whichever wallets are loaded
    monkeypatch.setattr(cli_module, "BitcoinCli", lambda command: _Switch(nodes, chain))
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
    assert out.strip() == str(root / "company" / "statements" / date) and "wallets: treasury 0.00078000 BTC, cold 0.00000000 BTC" in err
    directory = Path(out.strip())
    assert sorted(p.name for p in directory.iterdir()) == [
        f"company-{date}-history.html",
        f"company-{date}.csv",
        f"company-{date}.html",
        f"company-{date}.json",
    ]
    # the reader's document: each wallet a section, oldest first, the counterparty and the running balance, no txids, no proofs
    reader = (directory / f"company-{date}-history.html").read_text()
    assert "Transaction history" in reader and reader.index("1.</span> treasury") < reader.index("2.</span> cold")
    assert (
        "<th>Counterparty</th>" in reader
        and '<td>Exchange</td><td class="num">0.00100000</td>' in reader
        and '<th class="num">Fee</th>' in reader
    )
    assert '<td>cold</td><td class="num">(0.00070000)</td><td class="num">(0.00001000)</td>' in reader  # amount and fee apart
    assert (
        '<td>treasury</td><td class="num">0.00070000</td><td class="num"></td>' in reader and "<th>Kind</th>" not in reader
    )  # so the move pairs
    body = reader.split("</head>")[1]  # the shared style names the statement's colours; the document itself says nothing of proofs
    assert A not in body and B not in body and "proof" not in body.lower().replace("proofs of control", "") and "scriptPubKey" not in body
    assert (
        "Holdings at the end of the period: <strong>0.00078000 BTC</strong>, in 2 wallets: treasury 0.00078000, cold 0.00000000." in reader
    )
    written = json.loads((directory / f"company-{date}.json").read_text())
    assert written["label"] == "Company AB" and [w["name"] for w in written["wallets"]] == ["treasury", "cold"]
    assert written["period"]["start"]["height"] == 1000 and written["period"]["end"]["height"] == 1010 and written["period"]["to_tip"]
    blocks = csv_blocks(directory / f"company-{date}.csv")
    assert [(r["category"], r["wallet"]) for r in blocks["treasury"]] == [
        ("receive", "Exchange"),
        ("send", "cold"),
        ("receive", "cold"),
    ]
    assert [(r["category"], r["wallet"]) for r in blocks["cold"]] == [
        ("receive", "treasury"),
        ("send", "Exchange"),
        ("send", "treasury"),
    ]
    assert blocks["treasury"][-1]["balance"] == "0.00078000" and blocks["cold"][-1]["balance"] == "0.00000000"
    assert [w["history"]["source"] for w in written["wallets"]] == ["node", "node"]
    # the histories read are kept in the wallets' directories, for when a wallet is not loaded any more
    assert (root / "company" / "treasury" / "history.json").is_file() and (root / "company" / "cold" / "history.json").is_file()
    cold_node, treasury_node = nodes.pop("cold"), nodes["treasury"]  # cold unloaded: its kept history serves, it held nothing at its tip
    treasury_node.tip_height = 1012  # and the ledger runs to treasury's tip, not cut to cold's kept one
    date2 = datetime.fromtimestamp(block_time(1012), UTC).strftime("%Y-%m-%d")
    assert cli_module.main(["holder", "-o", str(tmp_path / "kept")]) == 1
    err = capsys.readouterr().err
    assert "cold: not loaded; from history.json saved " in err and "at block 1010, holding nothing" in err
    written = json.loads((tmp_path / "kept" / f"company-{date2}.json").read_text())
    assert written["period"]["end"]["height"] == 1012 and [w["history"]["source"] for w in written["wallets"]] == ["node", "file"]
    assert [w["history"]["tip_height"] for w in written["wallets"]] == [1012, 1010]
    assert csv_blocks(tmp_path / "kept" / f"company-{date2}.csv") == blocks
    assert "history kept " in (tmp_path / "kept" / f"company-{date2}.html").read_text()
    # the statement before: the ledger in the tree that closed at this one's opening block is cited, with its reference
    first_ref = json.loads((directory / f"company-{date}.json").read_text())["report_id"]
    assert cli_module.main(["holder", "--from-height", "1010", "--to-height", "1012", "-o", str(tmp_path / "next")]) == 1
    assert f"opening: the closing of {date} (ref {first_ref})" in capsys.readouterr().err
    following = json.loads((tmp_path / "next" / "company-1010..1012.json").read_text())
    assert following["previous"] == {
        "label": date, "report_id": first_ref, "file": f"company-{date}.json", "closing_sat": 78_000, "closing_btc": "0.00078000", "agrees": True,
    }  # fmt: skip
    assert f"the closing holdings of {date}, reference {first_ref}" in (tmp_path / "next" / "company-1010..1012.html").read_text()
    assert cli_module.main(["report", "-w", "cold", "--from-height", "1004", "--to-height", "1010", "-o", str(tmp_path / "c")]) == 0
    assert "cold: not loaded; from history.json" in capsys.readouterr().err  # a wallet's own statement, the same way
    nodes.pop("treasury")  # treasury unloaded too: it held coins at its kept tip, so a spend since would be missed
    assert cli_module.main(["holder", "-o", str(tmp_path / "refused")]) == 2
    assert "treasury is not loaded on the node, and " in capsys.readouterr().err and not (tmp_path / "refused").exists()
    nodes.update(treasury=treasury_node, cold=cold_node)
    treasury_node.tip_height = 1010
    assert "https://example.org/tx/" in (directory / f"company-{date}.html").read_text()  # explorer from [holders.company]
    # a wallet's statement takes its holder's settings too
    assert cli_module.main(["report", "-w", "treasury", "--from-height", "1004", "--to-height", "1010", "-o", str(tmp_path / "t")]) == 1
    capsys.readouterr()
    assert "https://example.org/tx/" in (tmp_path / "t" / "treasury-1004..1010.html").read_text()
    rows = list(csv.DictReader((tmp_path / "t" / "treasury-1004..1010.csv").open()))
    assert [(r["category"], r["wallet"]) for r in rows] == [("mixed", "cold")]  # cold's input, named by cold's node, no [addresses] entry
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
    assert cli_module.main(["holder", "company", "-o", str(tmp_path / "company")]) == 1
    assert "asking cold which of 2 addresses are theirs" in capsys.readouterr().err
    assert cli_module.main(["company", "-o", str(tmp_path / "bare")]) == 1  # bip322 report company: the holder's ledger
    capsys.readouterr()
    assert (tmp_path / "bare" / f"company-{date}.json").exists()
    assert cli_module.main(["--dir", str(root), "company", "-o", str(tmp_path / "bare2")]) == 1 and (tmp_path / "bare2").is_dir()
    capsys.readouterr()
    blocks = csv_blocks(tmp_path / "company" / f"company-{date}.csv")
    assert [(r["category"], r["wallet"]) for r in blocks["treasury"]] == [("receive", "Exchange"), ("send", "cold"), ("mixed", "cold")]
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
