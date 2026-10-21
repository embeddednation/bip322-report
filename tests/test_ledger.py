"""A custodial account's own record, the ledger file: read, matched by txid, checked, and shown."""

import json
from pathlib import Path

import pytest
from fake_node import block_time
from test_report import EXTERNAL, _node

from bip322report import cli as cli_module
from bip322report.history import fetch_history
from bip322report.ledger import Ledger, LedgerError, find_ledgers, match_ledgers
from bip322report.period import period_for_heights
from bip322report.render import render_history_html, render_html, write_csv
from bip322report.report import build_report, format_summary


def _iso(height: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(block_time(height), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ledger(path: Path, entries: list[dict], **head) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"ledger": 1, "currency": "EUR", "source": {"name": "raw/x.json", "sha256": "ab" * 32}, **head, "entries": entries})
    )
    return path


def _entries() -> list[dict]:
    """The fake wallet's aa (received 0.5 at 990) came from the account; its dd (sent 0.2 at 1005) went to the account."""
    return [
        {
            "kind": "withdrawal",
            "time_utc": _iso(989),
            "txid": "aa" * 32,
            "amount_sat": 50_000_000,
            "fee_sat": 5000,
            "address": "bc1qours",
            "reference": "W-777",
        },
        {"kind": "deposit", "time_utc": _iso(1005), "txid": "dd" * 32, "amount_sat": 20_000_000, "address": EXTERNAL, "reference": "D-501"},
    ]


def test_a_ledger_names_the_other_side_by_the_accounts_own_record(tmp_path, wallet):
    node = _node(wallet, tip=1012)
    history, period = fetch_history(node), period_for_heights(node, 979, 1012)
    ledger = Ledger.load(_ledger(tmp_path / "bitstamp" / "ledger.json", _entries()), "bitstamp")
    assert (
        ledger.account == "bitstamp" and ledger.currency == "EUR" and len(ledger.entries) == 2 and ledger.entries[0].time == block_time(989)
    )
    report = build_report(history, period, label="T", ledgers=[ledger])
    rows = {t["txid"][:2]: t for t in report["transactions"]}
    assert rows["aa"]["counterpart"] == "bitstamp" and rows["aa"]["endpoint"] == {
        "account": "bitstamp", "kind": "withdrawal", "reference": "W-777", "amount_sat": 50_000_000, "fee_sat": 5000, "entries": 1
    }  # fmt: skip
    assert rows["dd"]["counterpart"] == "bitstamp" and rows["dd"]["endpoint"]["reference"] == "D-501" and rows["bb"]["endpoint"] is None
    ends = report["endpoints"]
    assert ends["ok"] and ends["accounts"] == [
        {
            "name": "bitstamp", "file": "ledger.json", "currency": "EUR", "source": {"name": "raw/x.json", "sha256": "ab" * 32},
            "entries": 2, "in_period": 2, "matched": 2, "unmatched": [], "mismatched": [],
        }
    ]  # fmt: skip
    assert report["unnamed_movements"] == 2  # bb and cc came from nobody the statement can name
    summary = format_summary(report)
    assert "  ledger bitstamp: 2/2 entries of the period matched\n" in summary and "movements without a named far end: 2" in summary
    html = render_html(report)
    assert html.count("bitstamp") >= 2 and "W-777" in html and "D-501" in html and "bitstamp 2 of 2" in html  # ATTENTION: no proofs here
    assert "is named by that account's own record of it, kept with the holder (bitstamp: raw/x.json, sha256 abababababababab…)" in html
    assert "bitstamp" in render_history_html(report) and "W-777" not in render_history_html(report)  # the reader gets the name, not the id
    write_csv(report, tmp_path / "t.csv")
    assert ",bitstamp\n" in (tmp_path / "t.csv").read_text()  # the wallet column, last
    # the record is authoritative over [addresses]: a name given there for the account's address yields to the account
    named = build_report(history, period, label="T", ledgers=[ledger], addresses={EXTERNAL: "Exchange"})
    assert {t["txid"][:2]: t["counterpart"] for t in named["transactions"]}["dd"] == "bitstamp"


def test_an_entry_no_wallet_saw_or_an_amount_that_disagrees_is_attention(tmp_path, wallet):
    node = _node(wallet, tip=1012)
    history, period = fetch_history(node), period_for_heights(node, 979, 1012)
    entries = _entries() + [
        {
            "kind": "withdrawal",
            "time_utc": _iso(1000),
            "txid": "99" * 32,
            "amount_sat": 7_000_000,
            "reference": "W-900",
        },  # to another wallet
        {
            "kind": "deposit",
            "time_utc": _iso(300),
            "txid": "77" * 32,
            "amount_sat": 1,
            "reference": "D-old",
        },  # before the period: not counted
    ]
    entries[1]["amount_sat"] = 19_000_000  # the record says 0.19, the chain paid the account 0.20
    ledger = Ledger.load(_ledger(tmp_path / "x" / "ledger.json", entries), "x")
    report = build_report(history, period, label="T", ledgers=[ledger])
    ends = report["endpoints"]
    assert not ends["ok"] and not report["ok"]
    a = ends["accounts"][0]
    assert (a["in_period"], a["matched"]) == (3, 2) and [u["reference"] for u in a["unmatched"]] == ["W-900"]
    assert [(m["reference"], m["chain_sat"], m["claimed_sat"]) for m in a["mismatched"]] == [("D-501", 20_000_000, 19_000_000)]
    summary = format_summary(report)
    assert "UNMATCHED 1: withdrawal 0.07000000 BTC 2023-11-21 (W-900) seen by no wallet" in summary
    assert "MISMATCH 1: deposit 0.19000000 BTC 2023-11-21 (D-501), the chain 0.20000000 BTC" in summary and "RESULT: ATTENTION" in summary
    html = render_html(report)
    assert "x records a withdrawal of 0.07000000 on 2023-11-21 (W-900) that no wallet of this statement received." in html
    assert "x records a deposit of 0.19000000 on 2023-11-21 (D-501); the chain shows 0.20000000 in" in html
    assert "The accounts' own records agree with the movements" in html and "x 2 of 3 · 1 unmatched · 1 differ" in html
    # the same record in a period that holds none of it: nothing to say, nothing failed
    early = build_report(history, period_for_heights(node, 979, 985), label="T", ledgers=[ledger])
    assert early["endpoints"]["ok"] and early["endpoints"]["accounts"][0]["in_period"] == 0


def test_the_statement_reads_the_holders_ledgers_from_the_tree_and_refuses_a_broken_one(tmp_path, wallet, monkeypatch, capsys):
    from test_cli import _member

    node = _node(wallet, tip=1012)
    _member(node)
    root = Path(cli_module.find_data_dir())
    _ledger(root / "h" / "bitstamp" / "ledger.json", _entries())
    monkeypatch.setattr(cli_module, "BitcoinCli", lambda command: node)
    monkeypatch.setattr(cli_module, "_pdf_possible", lambda: False)
    monkeypatch.chdir(tmp_path)
    assert cli_module.main(["report", "--from-height", "979", "--to-height", "1012", "-o", "out"]) == 1  # no proofs: ATTENTION
    out, err = capsys.readouterr()
    assert "bitstamp: ledger of 2 entries" in err and "  ledger bitstamp: 2/2 entries of the period matched" in err
    written = json.loads(Path("out/h-979..1012.json").read_text())
    assert written["endpoints"]["ok"] and {t["txid"][:2]: t["counterpart"] for t in written["transactions"]}["aa"] == "bitstamp"
    assert [w for w in written["wallets"]][0]["transactions"][1]["counterpart"] == "bitstamp"  # the wallet's own block names it too
    assert [b.account for b in find_ledgers(root / "h")] == ["bitstamp"] and find_ledgers(root / "nowhere") == []
    (root / "h" / "bitstamp" / "ledger.json").write_text('{"ledger": 1, "entries": [{"kind": "sale"}]}')
    assert cli_module.main(["report", "--from-height", "979", "--to-height", "1012", "-o", "out2"]) == 2
    assert "entry 1: 'kind' must be one of deposit, withdrawal, not 'sale'" in capsys.readouterr().err and not Path("out2").exists()


@pytest.mark.parametrize(
    ("text", "flaw"),
    [
        ("[]", "not a ledger file of version 1"),
        ('{"ledger": 2, "entries": []}', "not a ledger file of version 1"),
        ('{"ledger": 1}', "'entries' is missing"),
        ('{"ledger": 1, "entries": [1]}', "entry 1 is not an object"),
        (
            '{"ledger": 1, "entries": [{"kind": "deposit", "txid": "xyz", "amount_sat": 1, "time_utc": "2026-01-01T00:00:00Z"}]}',
            "'txid' is not a transaction id",
        ),
        (
            '{"ledger": 1, "entries": [{"kind": "deposit", "txid": "'
            + "aa" * 32
            + '", "amount_sat": 1.5, "time_utc": "2026-01-01T00:00:00Z"}]}',
            "'amount_sat' is not a whole number",
        ),
        (
            '{"ledger": 1, "entries": [{"kind": "deposit", "txid": "' + "aa" * 32 + '", "amount_sat": 1, "time_utc": "yesterday"}]}',
            "'time_utc' is not an ISO 8601 instant",
        ),
        (
            '{"ledger": 1, "entries": [{"kind": "deposit", "txid": "'
            + "aa" * 32
            + '", "amount_sat": 1, "time_utc": "2026-01-01T00:00:00Z", "reference": 5}]}',
            "'reference' is not a string",
        ),
        ('{"ledger": 1, "source": "x", "entries": []}', "'source' is not an object with a 'name'"),
        ("{not json", "cannot be read as JSON"),
    ],
)
def test_a_ledger_file_that_is_not_one_is_refused_with_the_reason(tmp_path, text, flaw):
    path = tmp_path / "ledger.json"
    path.write_text(text)
    with pytest.raises(LedgerError, match=flaw):
        Ledger.load(path, "x")


def test_match_ledgers_without_a_ledger_or_a_movement(wallet, tmp_path):
    node = _node(wallet, tip=1012)
    history, period = fetch_history(node), period_for_heights(node, 979, 1012)
    assert match_ledgers([], history, period) == {"by_txid": {}, "accounts": [], "ok": True}
    report = build_report(history, period, label="T")
    assert report["endpoints"] is None and report["ok"] is False and "ledger" not in format_summary(report)
    ledger = Ledger.load(_ledger(tmp_path / "e" / "ledger.json", []), "e")
    assert build_report(history, period, label="T", ledgers=[ledger])["endpoints"]["accounts"][0]["entries"] == 0
