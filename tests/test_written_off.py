"""A coin declared lost: written off at a block, held no more from then on, never asked for a proof."""

import json
from pathlib import Path

import pytest
from fake_node import block_time
from test_cli import _member, run  # noqa: F401, F811
from test_report import _node

from bip322report.history import WriteOff, fetch_history
from bip322report.period import period_for_heights
from bip322report.render import render_history_html, render_html, write_csv
from bip322report.report import build_report, format_summary


def test_a_write_off_leaves_the_books_at_its_block(wallet, tmp_path):
    node = _node(wallet, tip=1012)
    history = fetch_history(node)
    lost = WriteOff(("bb" * 32, 0), 1008, block_time(1008), None, "keys of the old wallet lost")
    report = build_report(history, period_for_heights(node, 979, 1012), label="T", written_off=[lost])
    rows = {t["txid"][:2]: t for t in report["transactions"] if t["kind"] == "written_off"}
    row = rows["bb"]
    assert row["height"] == 1008 and row["counterpart"] == "" and row["net_sat"] == -25_000_000  # no other side: a loss, not a payment
    assert row["amount_sat"] == -25_000_000 and row["fee_sat"] is None and row["reason"] == "keys of the old wallet lost"
    assert report["totals"]["written_off_sat"] == 25_000_000 and report["totals"]["written_off_count"] == 1
    assert report["closing"]["total_sat"] == 38_900_000 and {c["txid"][:2] for c in report["closing"]["coins"]} == {"dd", "ee"}
    assert report["reconciliation"]["ok"] and report["coverage"]["total_count"] == 2
    assert report["written_off"] == [
        {**{k: row["ours_in"][0][k] for k in ("txid", "vout", "address", "amount_sat", "amount_btc", "height")},
         "created_utc": "2023-11-21T19:43:20Z", "height": 1008, "time_utc": "2023-11-21T22:13:20Z", "reason": "keys of the old wallet lost"},
    ]  # fmt: skip
    assert "written off in the period: 0.25000000 BTC 2023-11-21 (keys of the old wallet lost)" in format_summary(report)
    html = render_html(report)
    assert "Written off" in html and "(0.25000000)" in html and "1 UTXO declared lost by the wallet, still on the chain" in html
    assert "Written off in the period, by the wallet's own declaration: 0.25000000 on 2023-11-21 (keys of the old wallet lost)" in html
    assert "Written off, keys of the old wallet lost" in render_history_html(report)
    write_csv(report, tmp_path / "t.csv")
    assert ",written_off,-0.25000000," in (tmp_path / "t.csv").read_text()
    # before the write-off the coin was held; after it, it is not: the opening of a later period excludes it
    before = build_report(history, period_for_heights(node, 979, 1007), label="T", written_off=[lost])
    assert {c["txid"][:2] for c in before["closing"]["coins"]} == {"bb", "cc", "dd"} and before["written_off"] == []  # ee comes at 1010
    later = build_report(history, period_for_heights(node, 1008, 1012), label="T", written_off=[lost])
    assert (
        later["opening"]["total_sat"] == 39_000_000 and later["written_off"] == [] and later["totals"]["written_off_count"] == 0
    )  # dd:1 and cc:0
    # a closing UTXO written off after the period says so on its page
    with_lookup = build_report(history, period_for_heights(node, 979, 1007), label="T", written_off=[lost], cli=node)
    bb = next(c for c in with_lookup["closing"]["coins"] if c["txid"].startswith("bb"))
    assert (
        bb["spent_by"]["written_off"] and bb["spent_by"]["reason"] == "keys of the old wallet lost" and bb["onchain"]["status"] == "matches"
    )
    html = render_html(with_lookup)
    assert (
        "WRITTEN OFF SINCE" in html
        and "declared lost by the wallet after the period, at block 1008 (keys of the old wallet lost); still on the chain" in html
    )
    assert html.count("WRITTEN OFF SINCE") == 1  # bb alone; cc, spent by ee at 1010, says SPENT SINCE as before


def test_a_write_off_the_wallets_cannot_bear_is_refused(wallet):
    node = _node(wallet, tip=1012)
    history = fetch_history(node)
    with pytest.raises(ValueError, match="is not an output of the wallets'"):
        history.written_off([WriteOff(("77" * 32, 0), 1008, block_time(1008), None, "x")])
    with pytest.raises(ValueError, match="was spent at block 1005, before it is written off at block 1008"):
        history.written_off([WriteOff(("aa" * 32, 0), 1008, block_time(1008), None, "x")])
    with pytest.raises(ValueError, match="was not confirmed yet at block 985"):
        history.written_off([WriteOff(("aa" * 32, 0), 985, block_time(985), None, "x")])
    assert history.written_off([]) is history


def test_the_statement_reads_the_write_offs_from_the_config(wallet, run, monkeypatch, capsys):  # noqa: F811
    import os

    node = _node(wallet, tip=1012)
    _member(node)
    root = Path(os.environ["BIP322_DIR"])
    date = "2023-11-21T22:05:00Z"  # between blocks 1007 (22:03:20) and 1008: the write-off stands in block 1008, the first at or after it
    (root / "bip322.toml").write_text(f'holder = "h"\n[written_off]\n"{"bb" * 32}:0" = {{ date = "{date}", reason = "keys lost" }}\n')
    code, out, err = run(node, "report", "--from-height", "979", "--to-height", "1012", "-o", "w")
    assert code == 1 and "written off in the period: 0.25000000 BTC 2023-11-21 (keys lost)" in err  # ATTENTION: no proofs
    written = json.loads(next(p for p in Path("w").iterdir() if p.name.endswith(".json")).read_text())
    assert written["written_off"][0]["height"] == 1008 and written["closing"]["total_sat"] == 38_900_000
    assert written["wallets"][0]["written_off_sat"] == 25_000_000
    (root / "bip322.toml").write_text('holder = "h"\n[written_off]\n"nonsense" = { date = "2023-11-21", reason = "x" }\n')
    assert run(node, "report", "--from-height", "979", "--to-height", "1012", "-o", "x")[0] == 2
    (root / "bip322.toml").write_text(f'holder = "h"\n[written_off]\n"{"bb" * 32}:0" = {{ date = "2099-01-01", reason = "x" }}\n')
    code, out, err = run(node, "report", "--from-height", "979", "--to-height", "1012", "-o", "y")
    assert code == 2 and "after the history's tip" in err
    (root / "bip322.toml").write_text(f'holder = "h"\n[written_off]\n"{"aa" * 32}:0" = {{ date = "{date}", reason = "x" }}\n')
    code, out, err = run(node, "report", "--from-height", "979", "--to-height", "1012", "-o", "z")
    assert code == 2 and "was spent at block 1005" in err
