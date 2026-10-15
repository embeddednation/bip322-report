"""The command line: what it refuses before any work, how it names and copies proofs files, and what it exits with."""

import csv
import json
import os
import re
import shutil
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fake_node import FakeNode, block_time
from test_report import _bundle, _node

import bip322report.cli as cli_module
from bip322report.coverage import load_proofs_files, pending_snapshots, proofs_names
from bip322report.fiat import Rates
from bip322report.history import fetch_history
from bip322report.period import period_between, period_for_heights
from bip322report.report import build_report, format_summary

HEIGHTS = ["--from-height", "979", "--to-height", "1000"]  # the whole life of the fake wallet at tip 1000: aa, bb at a0, cc at a1


@pytest.fixture(scope="module")
def bundles(tmp_path_factory, wallet, signer_expressions) -> Path:
    """Bundles at tip 1000: ``a0`` and ``a1`` prove one address each, ``full`` both; ``old`` is an earlier full one, ``foreign`` proves an address of no coin."""
    root = tmp_path_factory.mktemp("bundles")
    node = _node(wallet, tip=1000)
    a0, a1 = wallet.derive(0).address, wallet.derive(0, 1).address
    _bundle(root / "a0", node, wallet, signer_expressions, skip=[a1])
    _bundle(root / "a1", node, wallet, signer_expressions, skip=[a0])
    _bundle(root / "full", node, wallet, signer_expressions)
    _bundle(root / "old", _node(wallet, tip=996), wallet, signer_expressions)
    _bundle(root / "foreign", node, wallet, signer_expressions, addresses=[wallet.derive(40).address])
    return root


@pytest.fixture
def run(monkeypatch, tmp_path, capsys):
    """Run the CLI against a fake node, in an empty directory: (exit status, stdout, stderr)."""
    monkeypatch.chdir(tmp_path)

    def go(node, *argv, pdf: bool = False):
        monkeypatch.setattr(cli_module, "BitcoinCli", lambda command: node)
        monkeypatch.setattr(cli_module, "_pdf_possible", lambda: pdf)  # the HTML, not a PDF, unless a test says so
        code = cli_module.main([str(a) for a in argv])
        out, err = capsys.readouterr()
        return code, out, err

    return go


def _place(bundles: Path, target: Path, name: str) -> Path:
    shutil.copytree(bundles / name, target)
    return target


def _written(directory: str, suffix: str) -> Path:
    """The statement's file with this suffix: ``<wallet>-<label><suffix>`` in the directory, whatever -o named the directory."""
    found = [p for p in Path(directory).iterdir() if p.name.endswith(suffix) and not p.name.endswith("-proofs.json")]
    assert len(found) == 1, found
    return found[0]


def test_proofs_files_are_cited_and_copied_under_their_own_names(bundles, wallet, run, tmp_path):
    """A proofs file has one name wherever it is: the statement cites it by that name and copies it beside itself under it."""
    node = _node(wallet, tip=1000)
    code, out, err = run(
        node, "report", *HEIGHTS, "--snapshots", bundles / "a0", "--snapshots", bundles / "a1" / "a1-proofs.json", "-o", "out"
    )
    assert code == 0 and "RESULT: OK" in err and out.strip() == "out"
    written = json.loads(_written("out", ".json").read_text())
    assert {f["file"]: f["used_for"] for f in written["proofs_files"]} == {"a0-proofs.json": 2, "a1-proofs.json": 1}
    assert "proofs a0-proofs.json" in err and "used for 2" in err and "proofs a1-proofs.json" in err
    for name in ("a0", "a1"):
        assert Path(f"out/{name}-proofs.json").read_bytes() == (bundles / name / f"{name}-proofs.json").read_bytes()
    assert sorted(p.name for p in Path("out").iterdir()) == [
        "a0-proofs.json", "a1-proofs.json", "watch-979..1000.csv", "watch-979..1000.html", "watch-979..1000.json",
    ]  # fmt: skip
    cited = {c["proof"]["file"] for c in written["closing"]["coins"]}
    assert cited == {"a0-proofs.json", "a1-proofs.json"} and str(bundles) not in _written("out", ".json").read_text()  # never a path


def test_two_proofs_files_under_one_name_are_refused_and_an_old_proofs_json_is_named_after_its_snapshot(bundles, wallet, run, tmp_path):
    """Nothing tells two equally named files apart in a statement: refused before any work. A ``proofs.json`` (earlier versions) is
    named after the snapshot directory that holds it."""
    old = _place(bundles, tmp_path / "old-style" / "year-end", "a0")
    (old / "a0-proofs.json").rename(old / "proofs.json")
    assert proofs_names([old / "proofs.json", bundles / "a1" / "a1-proofs.json"]) == {
        (old / "proofs.json").resolve(): "year-end-proofs.json",
        (bundles / "a1" / "a1-proofs.json").resolve(): "a1-proofs.json",
    }
    node = _node(wallet, tip=1000)
    code, _, err = run(node, "report", *HEIGHTS, "--snapshots", tmp_path / "old-style", "--snapshots", bundles / "a1", "-o", "out")
    assert code == 0, err
    assert Path("out/year-end-proofs.json").read_bytes() == (old / "proofs.json").read_bytes()
    _place(bundles, tmp_path / "x" / "one", "a0")
    _place(bundles, tmp_path / "y" / "one", "a1")
    (tmp_path / "y" / "one" / "a1-proofs.json").rename(tmp_path / "y" / "one" / "a0-proofs.json")
    node = _node(wallet, tip=1000)
    code, out, err = run(node, "report", *HEIGHTS, "--snapshots", tmp_path / "x", "--snapshots", tmp_path / "y", "-o", "clash")
    assert code == 2 and "would both be named a0-proofs.json" in err and out == "" and not Path("clash").exists() and node.calls == []


def test_only_proofs_files_the_statement_relies_on_are_listed_and_copied(bundles, wallet, run, tmp_path):
    """Finding 10: a second wallet's proofs and a superseded file among the snapshots were named in the statement and copied beside it."""
    ledger = tmp_path / "ledger"
    for name in ("full", "old", "foreign"):
        _place(bundles, ledger / name, name)
    foreign = wallet.derive(40).address
    for name, address in (("foreign-pending", foreign), ("pending", wallet.derive(0).address)):
        (ledger / name).mkdir()
        (ledger / name / "snapshot.json").write_text(json.dumps({"addresses": [{"address": address}]}))
    code, _, err = run(_node(wallet, tip=1000), "report", *HEIGHTS, "--snapshots", ledger, "-o", "out")
    assert code == 0, err
    written = json.loads(_written("out", ".json").read_text())
    assert [(f["file"], f["used_for"]) for f in written["proofs_files"]] == [("full-proofs.json", 3)] and written[
        "proofs_files_not_used"
    ] == 2
    assert written["policy"] == "2 of 3" and written["pending_snapshots"] == ["pending"]
    assert [p.name for p in Path("out").glob("*-proofs.json")] == ["full-proofs.json"]
    everything = _written("out", ".json").read_text() + _written("out", ".html").read_text() + _written("out", ".csv").read_text()
    assert "old-proofs.json" not in everything and "foreign" not in everything and foreign not in everything
    assert "other proofs files, not used: 2" in err and "old-proofs.json" not in format_summary(written)
    # the policy is that of the proofs files relied on, not of whatever else the snapshots hold
    document = json.loads((ledger / "foreign" / "foreign-proofs.json").read_text())
    (ledger / "foreign" / "foreign-proofs.json").write_text(json.dumps({**document, "policy": "3 of 5"}))
    node = _node(wallet, tip=1000)
    report = build_report(fetch_history(node), period_for_heights(node, 979, 1000), label="x", proofs_roots=[ledger], cli=node)
    assert report["policy"] == "2 of 3" and pending_snapshots([ledger]) == [ledger / "foreign-pending", ledger / "pending"]


def test_a_cited_proofs_file_that_does_not_verify_is_still_listed_and_copied(bundles, wallet, run, tmp_path):
    """Cited means shown on a UTXO's page, verified or not: the reader needs the file to see the proof fail."""
    ledger = tmp_path / "ledger"
    document = json.loads((_place(bundles, ledger / "a1", "a1") / "a1-proofs.json").read_text())
    other = json.loads((bundles / "a0" / "a0-proofs.json").read_text())["proofs"][0]["signature"]
    document["proofs"][0]["signature"] = other  # a well-formed proof of another script
    (ledger / "a1" / "a1-proofs.json").write_text(json.dumps(document))
    code, _, err = run(_node(wallet, tip=1000), "report", *HEIGHTS, "--snapshots", ledger, "-o", "out")
    written = json.loads(_written("out", ".json").read_text())
    assert code == 1 and [(f["file"], f["verified"], f["used_for"]) for f in written["proofs_files"]] == [("a1-proofs.json", False, 1)]
    assert "NOT VERIFIED, used for 1" in err and Path("out/a1-proofs.json").exists() and written["proofs_files_not_used"] == 0


def test_report_exits_0_when_ok_1_on_attention_2_on_error(bundles, wallet, run, capsys):
    """Packaging review: ``report`` always exited 0, so a script could not tell a clean statement from one that needs attention."""
    assert run(_node(wallet, tip=1000), "report", *HEIGHTS, "--snapshots", bundles / "full", "-o", "ok")[0] == 0
    code, out, err = run(_node(wallet, tip=1000), "report", *HEIGHTS, "-o", "attention")  # no proofs at all
    assert code == 1 and "RESULT: ATTENTION" in err and out.strip() == "attention" and _written("attention", ".json").exists()
    assert run(_node(wallet, tip=1000), "report", *HEIGHTS, "-o", "attention")[0] == 2  # exists and is not empty
    with pytest.raises(SystemExit) as stop:
        cli_module.main(["report", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert stop.value.code == 0 and "Exit status: 0 when the result is OK, 1 when it is ATTENTION, 2 on an error" in text


def test_report_help_describes_dust_the_default_directory_and_the_explorer_as_they_are(capsys):
    """Finding 16: the help said dust was counted in the balance, and named a default directory the code does not use."""
    with pytest.raises(SystemExit):
        cli_module.main(["report", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "left out of every figure" in text and "counted in the balance" not in text and "change and consolidations never are" in text
    assert "named <wallet>-<label>.pdf" in text and "report-<label>" not in text
    assert "on mainnet, none on other chains" in text  # finding 24


def test_dust_threshold_is_bounded(wallet, run):
    """Finding 3: --dust -5 was silently no dust, and --dust 100000000 an empty statement with RESULT: OK."""
    node = _node(wallet, tip=1000)
    code, _, err = run(node, "report", *HEIGHTS, "--dust", "-5", "-o", "d")
    assert code == 2 and "cannot be negative" in err and len(err.splitlines()) == 1
    code, _, err = run(node, "report", *HEIGHTS, "--dust", "10001", "-o", "d")
    assert code == 2 and "--dust-force" in err and len(err.splitlines()) == 1 and node.calls == [] and not Path("d").exists()
    assert run(node, "report", *HEIGHTS, "--dust", "10000", "-o", "d")[0] == 1  # the bound itself is allowed (no proofs: ATTENTION)
    code, _, err = run(node, "report", *HEIGHTS, "--dust", "10001", "--dust-force", "-o", "forced")
    assert code == 1 and json.loads(_written("forced", ".json").read_text())["dust"]["threshold_sat"] == 10001


def test_options_that_contradict_or_point_nowhere_are_refused_before_any_work(bundles, wallet, run, tmp_path):
    """Finding 17: conflicting period options ran the year, --rate beside --rates was ignored, a mistyped --snapshots looked like "no proofs"."""
    node = _node(wallet, tip=1000)
    refused = [
        (["--year", "2023", *HEIGHTS], "give the period one way"),
        (["--year", "2023", "--from", "2020-01-01", "--to", "2021-01-01"], "give the period one way"),
        ([*HEIGHTS, "--to", "2021-01-01"], "give the period one way"),
        ([], "give the period one way"),
        (["--from", "2020-01-01"], "--from and --to go together"),
        (["--to-height", "1000"], "--from-height and --to-height go together"),
        ([*HEIGHTS, "--rate", "5", "--rates", "r.csv"], "--rates and --rate exclude each other"),
        ([*HEIGHTS, "--currency", "USD"], "--currency names the currency of --rates or --rate"),
        ([*HEIGHTS, "--snapshots", tmp_path / "snapshtos"], "no such file or directory"),
        ([*HEIGHTS, "--snapshots", tmp_path], "no proofs file in it"),
    ]
    for argv, message in refused:
        code, out, err = run(node, "report", *argv, "-o", "never")
        assert code == 2 and message in err and len(err.splitlines()) == 1 and out == "", (argv, err)
    assert node.calls == [] and not Path("never").exists()
    # the output directory is looked at before the node is asked anything; the default one as soon as the wallet's name is known
    Path("taken").mkdir()
    Path("taken/file").write_text("x")
    code, _, err = run(node, "report", *HEIGHTS, "--snapshots", bundles / "full", "-o", "taken")
    assert code == 2 and "taken exists and is not empty" in err and node.calls == []
    default = tmp_path / "bip322" / "watch" / "statements" / "979..1000"  # the data directory: BIP322_DIR, set per test
    default.mkdir(parents=True)
    (default / "file").write_text("x")
    code, _, err = run(node, "report", *HEIGHTS)
    assert code == 2 and f"{default} exists and is not empty" in err and [c[0] for c in node.calls] == ["getwalletinfo"]
    node.calls.clear()
    assert run(node, "-w", "watch", "report", *HEIGHTS)[0] == 2 and node.calls == []
    assert run(node, "report", *HEIGHTS, "--force")[0] == 1 and (default / "watch-979..1000.json").exists()
    # snapshots awaiting signatures count as snapshots: not an error, and the statement says what is awaited
    waiting = tmp_path / "waiting" / "snapshot-1"
    waiting.mkdir(parents=True)
    (waiting / "snapshot.json").write_text(json.dumps({"addresses": [{"address": wallet.derive(0).address}]}))
    code, _, err = run(node, "report", *HEIGHTS, "--snapshots", tmp_path / "waiting", "-o", "w")
    assert code == 1 and "awaiting signatures: snapshot-1" in err


def test_bad_rates_give_one_line_with_file_and_line(wallet, run, tmp_path):
    """Finding 15: --rate abc raised InvalidOperation, a one-column rates row IndexError."""
    node = _node(wallet, tip=1000)
    code, _, err = run(node, "report", *HEIGHTS, "--rate", "abc", "-o", "r")
    assert code == 2 and err == "error: the rate: 'abc' is not a rate (a positive number per BTC)\n" and node.calls == []
    rates = tmp_path / "rates.csv"
    for body, message in (
        ("date,rate\n2023-11-10,100\n2023-11-16\n", "rates.csv:3: expected date,rate"),
        ("2023-11-10,100\n16/11/2023,200\n", "rates.csv:2: '16/11/2023' is not a date"),
        ("# rates\n\n2023-11-10,1e\n", "rates.csv:3: '1e' is not a rate"),
        ("2023-11-10,-4\n", "rates.csv:1: '-4' is not a rate"),
        ("2023-11-10,NaN\n", "rates.csv:1: 'NaN' is not a rate"),
    ):
        rates.write_text(body)
        with pytest.raises(ValueError, match=message):
            Rates.from_csv("EUR", rates)
        code, _, err = run(node, "report", *HEIGHTS, "--rates", rates, "--currency", "EUR", "-o", "r")
        assert code == 2 and message in err and len(err.splitlines()) == 1 and not Path("r").exists()
    with pytest.raises(ValueError, match="not a rate"):
        Rates.constant_rate("EUR", "0")


def test_a_malformed_proofs_file_is_left_out_with_one_line_not_a_traceback(bundles, wallet, run, tmp_path):
    """Finding 15: a proofs file that find_proofs accepts but that lacks a message raised KeyError; now unusable, said so, backs nothing."""
    node = _node(wallet, tip=1000)
    ledger = tmp_path / "ledger"
    document = json.loads((_place(bundles, ledger / "full", "full") / "full-proofs.json").read_text())
    for name, broken, flaw in (
        ("no-message", {k: v for k, v in document.items() if k != "message"}, "no message"),
        ("no-signature", {**document, "proofs": [{k: v for k, v in document["proofs"][0].items() if k != "signature"}]}, "proof 1 without"),
        ("no-utxos", {**document, "proofs": [{**document["proofs"][0], "utxos": None}]}, "proof 1 without a list of utxos"),
        ("half-stamp", {**document, "stamp": {"height": 994}}, "stamp without"),
    ):
        (ledger / name).mkdir()
        (ledger / name / f"{name}-proofs.json").write_text(json.dumps(broken))
        said: list[str] = []
        file = load_proofs_files(node, [ledger / name], progress=said.append)[0]
        assert file.unusable and flaw in file.error and not file.ok and f"not a usable proofs file ({flaw}" in said[0]
    shutil.rmtree(ledger / "full")
    code, _, err = run(node, "report", *HEIGHTS, "--snapshots", ledger, "-o", "broken")
    assert code == 1 and "Traceback" not in err and "not a usable proofs file (no message)" in err and "proof coverage: 0/3" in err
    assert json.loads(_written("broken", ".json").read_text())["proofs_files"] == [] and not list(Path("broken").glob("*-proofs.json"))
    _place(bundles, ledger / "full", "full")
    assert run(node, "report", *HEIGHTS, "--snapshots", ledger, "-o", "mended")[0] == 0  # the whole one beside them still serves


def test_a_node_that_is_not_past_the_periods_end_is_an_error(wallet, run, monkeypatch):
    """Finding 13: a past end beyond the node's last block closed the period on the tip, a year-end that is not one."""
    node = _node(wallet, tip=1012)
    assert run(node, "-w", "watch", "history", "-o", "history.json")[0] == 0
    # every fake block lies in November 2023, the year's end is past, and the node has no block after it
    code, _, err = run(node, "report", "--history", "history.json", "--year", "2023", "-o", "y")
    assert code == 2 and "is before the period's end 2024-01-01T00:00:00Z" in err and "not settled" in err and not Path("y").exists()
    start, end = datetime.fromtimestamp(block_time(100), UTC), datetime.fromtimestamp(block_time(900), UTC)
    assert period_between(node, start, end).end.height == 899  # an end the node is well past resolves as before

    def syncing(method, *params):
        answer = FakeNode.call(node, method, *params)
        return {**answer, "initialblockdownload": True} if method == "getblockchaininfo" else answer

    with monkeypatch.context() as m:
        m.setattr(node, "call", syncing)
        with pytest.raises(ValueError, match="still syncing"):
            period_between(node, start, end)
        assert period_for_heights(node, 1000, 1012).end.height == 1012  # heights say which blocks: allowed, also up to the tip


def test_a_period_to_the_tip_closes_on_the_historys_tip(wallet, run, monkeypatch):
    """Finding 13: a block arriving after the history was read made a to-tip period fail with "the history is older"."""
    node = _node(wallet, tip=1012)
    assert run(node, "-w", "watch", "history", "-o", "history.json")[0] == 0
    node.tip_height = 1015
    code, _, err = run(node, "report", "--history", "history.json", "--from", "2023-11-14", "--to", "2099-01-01", "-o", "t")
    assert code == 1 and "older than" not in err
    period = json.loads(_written("t", ".json").read_text())["period"]
    assert period["to_tip"] and period["end"]["height"] == 1012 and period["label"] == "2023-11-14..2099-01-01"
    start = datetime.fromtimestamp(block_time(100), UTC)
    assert period_between(node, start, datetime(2099, 1, 1, tzinfo=UTC)).end.height == 1015  # without a history: the node's tip
    # the clock set to the fake chain's present: the year is not over, so it runs to the tip too
    monkeypatch.setattr("bip322report.period._now", lambda: datetime.fromtimestamp(block_time(1012) + 60, UTC))
    assert run(node, "report", "--history", "history.json", "--year", "2023", "-o", "y")[0] == 1
    period = json.loads(_written("y", ".json").read_text())["period"]
    assert period["to_tip"] and period["end"]["height"] == 1012 and period["label"] == "2023"


def test_a_history_from_another_chain_or_a_reorged_tip_is_refused(wallet, run):
    """Finding 19: --history was trusted against any node."""
    node = _node(wallet, tip=1012)
    assert run(node, "-w", "watch", "history", "-o", "history.json")[0] == 0
    history = json.loads(Path("history.json").read_text())
    for name, change, message in (
        ("testnet.json", {"chain": "test"}, "testnet.json is of the test chain; the node is on main"),
        ("reorged.json", {"tip_hash": "ab" * 32}, "reorged.json: its tip block 1012 (" + "ab" * 32 + ") is not in the node's chain"),
        ("ahead.json", {"tip_height": 1013, "tip_hash": "cd" * 32}, "ahead.json: its tip block 1013"),
    ):
        Path(name).write_text(json.dumps({**history, **change}))
        code, out, err = run(node, "report", "--history", name, "--from-height", "1000", "--to-height", "1012", "-o", "h")
        assert code == 2 and message in err and len(err.splitlines()) == 1 and out == "" and not Path("h").exists()
        assert run(node, "balance", "--history", name, "--at", "2023-11-21T10:00:00Z")[0] == 2
    assert run(node, "balance", "--history", "history.json", "--at", "2023-11-21T10:00:00Z")[0] == 0
    assert run(node, "report", "--history", "history.json", "--from-height", "1000", "--to-height", "1012", "-o", "h")[0] == 1


def test_the_statement_is_a_pdf_when_it_can_be_and_html_otherwise(bundles, wallet, run, monkeypatch):
    """Installing [pdf] says what is wanted: no flag. Without WeasyPrint the statement is one HTML file, said so; --html asks for that."""
    node = _node(wallet, tip=1000)
    code, _, err = run(node, "report", *HEIGHTS, "--snapshots", bundles / "full", "-o", "h")  # the fixture: no WeasyPrint
    assert code == 0 and "no PDF (WeasyPrint is not installed or lacks Pango): the statement is watch-979..1000.html" in err
    assert sorted(p.name for p in Path("h").iterdir()) == [
        "full-proofs.json",
        "watch-979..1000.csv",
        "watch-979..1000.html",
        "watch-979..1000.json",
    ]
    assert 'src: url("data:font/woff2;base64,' in Path("h/watch-979..1000.html").read_text()  # one file: the fonts travel inside it
    code, _, err = run(node, "report", *HEIGHTS, "--snapshots", bundles / "full", "--html", "-o", "asked")
    assert code == 0 and "no PDF" not in err and Path("asked/watch-979..1000.html").exists()
    written: list = []
    monkeypatch.setattr(cli_module, "write_pdf", lambda html, path: written.append(path))
    code, _, err = run(node, "report", *HEIGHTS, "--snapshots", bundles / "full", "-o", "p", pdf=True)
    assert code == 0 and "no PDF" not in err and written == [Path("p/watch-979..1000.pdf")] and not Path("p/watch-979..1000.html").exists()
    code, _, err = run(node, "report", *HEIGHTS, "--snapshots", bundles / "full", "--html", "-o", "p2", pdf=True)  # asked for, PDF or not
    assert code == 0 and written == [Path("p/watch-979..1000.pdf")] and Path("p2/watch-979..1000.html").exists()


def test_pdf_is_written_last(bundles, wallet, run, monkeypatch):
    """Finding 18: should the rendering itself fail, everything else is already in place and the result has been said."""
    node = _node(wallet, tip=1000)

    def fail(html, path):
        assert (path.parent / "full-proofs.json").exists() and (path.parent / "watch-979..1000.csv").exists()
        assert 'src: url("file://' in html and "data:font" not in html  # the PDF reads the fonts as installed
        assert path.name == "watch-979..1000.pdf" and not (path.parent / "fonts").exists()
        raise RuntimeError("no Pango")

    monkeypatch.setattr(cli_module, "write_pdf", fail)
    code, out, err = run(node, "report", *HEIGHTS, "--snapshots", bundles / "full", "-o", "p", pdf=True)
    assert code == 2 and "RESULT: OK" in err and err.splitlines()[-1] == "error: no Pango" and Path("p/watch-979..1000.json").exists()
    assert not Path("p/watch-979..1000.html").exists()


def test_explorer_links_by_default_on_mainnet_only(wallet, run, monkeypatch):
    """Finding 24: the mainnet explorer was linked on every chain."""
    node = _node(wallet, tip=1000)
    run(node, "report", *HEIGHTS, "-o", "main")
    assert 'href="https://mempool.space/tx/' in _written("main", ".html").read_text()
    run(node, "report", *HEIGHTS, "--explorer", "", "-o", "none")
    assert "href=" not in _written("none", ".html").read_text()

    def signet(method, *params):
        answer = FakeNode.call(node, method, *params)
        return {**answer, "chain": "signet"} if method == "getblockchaininfo" else answer

    monkeypatch.setattr(node, "call", signet)
    run(node, "report", *HEIGHTS, "-o", "signet")
    html = _written("signet", ".html").read_text()
    assert "href=" not in html and "mempool.space" not in html
    run(node, "report", *HEIGHTS, "--explorer", "https://explorer.example/signet/", "-o", "named")
    assert 'href="https://explorer.example/signet/tx/' in _written("named", ".html").read_text()


def test_notes_name_the_repository_not_an_unregistered_package(wallet, run):
    """Finding 20: the statement told readers to ``pip install`` a name not yet registered."""
    run(_node(wallet, tip=1000), "report", *HEIGHTS, "-o", "n")
    html = _written("n", ".html").read_text()
    assert "https://github.com/embeddednation/bip322-report" in html and "pip install" not in html


def test_the_statement_is_the_default_command(wallet, run, capsys):
    from bip322report.cli import with_default_command

    assert with_default_command([]) == ["help"]
    assert with_default_command(["-w", "x", "--year", "2026"]) == ["-w", "x", "report", "--year", "2026"]
    assert with_default_command(["--cli=bitcoin-cli -signet", "--from-height", "1"]) == [
        "--cli=bitcoin-cli -signet",
        "report",
        "--from-height",
        "1",
    ]
    for untouched in (["--version"], ["history", "--help"], ["help", "report"]):
        assert with_default_command(untouched) == untouched
    assert with_default_command(["-w", "x"]) == ["-w", "x", "report"]
    assert with_default_command(["--dir", "d", "-y", "2026"]) == ["--dir", "d", "report", "-y", "2026"]
    assert with_default_command(["--dir=d"]) == ["--dir=d", "report"]
    node = _node(wallet, tip=1000)
    assert run(node, *HEIGHTS, "-o", "short")[0] == run(node, "report", *HEIGHTS, "-o", "long")[0] == 1  # no proofs: ATTENTION, both ways
    assert _written("short", ".json").exists() and _written("long", ".json").exists()
    assert run(node)[0] == 0 and "history" in run(node, "help")[1]  # nothing at all lists the commands


def test_wallet_and_dir_go_before_or_after_any_command(bundles, wallet, run, tmp_path):
    """-w and --dir are taken on either side of every command, and --dir beats BIP322_DIR."""
    node = _node(wallet, tip=1000)
    elsewhere = tmp_path / "elsewhere"
    _place(bundles, elsewhere / "watch" / "snapshots" / "full", "full")
    code, out, err = run(node, "report", *HEIGHTS, "--dir", elsewhere, "-w", "watch")
    assert code == 0 and "RESULT: OK" in err and out.strip() == str(elsewhere / "watch" / "statements" / "979..1000")
    assert _written(out.strip(), ".json").name == "watch-979..1000.json" and not (tmp_path / "bip322").exists()
    code, _, err = run(node, "--dir", elsewhere, "report", *HEIGHTS, "-w", "watch")
    assert code == 2 and "exists and is not empty" in err  # the same place, found again
    for argv in (["block", "-w", "x", "2026-01-01"], ["history", "--dir", "d", "-w", "x"], ["report", "-y", "2026", "--dir", "d"]):
        cli_module.build_parser().parse_args(argv)  # accepted after the command


def test_the_sample_config_names_a_theme_and_a_heading_style_that_exist():
    """bip322 init writes bip322-audit's sample; the theme and heading it suggests must be ones this package renders."""
    from bip322audit.datadir import sample_config

    data = tomllib.loads(re.sub(r"^# (?=[a-z]+ = |\[)", "", sample_config(), flags=re.M))
    assert data["theme"] == cli_module.DEFAULT_THEME in cli_module.THEMES and data["heading"] in cli_module.HEADINGS
    assert set(data["wallets"]) == {"treasury", "personal"} and tomllib.loads(sample_config()) == {}


def test_inside_a_wallets_directory_that_wallet_is_meant(bundles, wallet, run, tmp_path, monkeypatch):
    """Run in books/ (not the node wallet's name), the statement is books'; no -w, no asking the node which wallet."""
    node = _node(wallet, tip=1000)
    _place(bundles, tmp_path / "bip322" / "books" / "snapshots" / "full", "full")
    monkeypatch.chdir(tmp_path / "bip322" / "books")
    code, out, err = run(node, "report", *HEIGHTS)
    assert code == 0 and out.strip() == str(tmp_path / "bip322" / "books" / "statements" / "979..1000")
    assert _written(out.strip(), ".json").name == "books-979..1000.json"


def test_the_addresses_table_names_the_other_side_in_the_csv(wallet, run, tmp_path):
    """[addresses] in bip322.toml: the wallet column of the CSV, from a receive's payers and a send's payees."""
    node = FakeNode(tip=1000)
    a0, a1 = wallet.derive(0).address, wallet.derive(1).address
    node.mine |= {a0, a1}
    exchange = "bc1qexchange0000000000000000000000000000000xch"
    node.others[("ff" * 32, 0)] = exchange
    node.add("aa" * 32, 990, [("ff" * 32, 0)], [(a0, 100_000)])
    node.add("bb" * 32, 995, [("aa" * 32, 0)], [(exchange, 30_000), (a1, 69_000)])
    data_dir = Path(os.environ["BIP322_DIR"])
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "bip322.toml").write_text(f'[addresses]\n"{exchange}" = "Exchange"\n')
    code, out, err = run(node, "-w", "watch", "report", "--from-height", "980", "--to-height", "1000", "-o", "named")
    assert code == 1 and "ATTENTION" in err  # no proofs: the statement is written all the same
    rows = list(csv.DictReader(_written("named", ".csv").open()))
    assert list(rows[0]) == ["time", "txid", "category", "amount", "balance", "wallet"]
    assert [(r["category"], r["amount"], r["balance"], r["wallet"]) for r in rows] == [
        ("receive", "0.00100000", "0.00100000", "Exchange"),
        ("send", "-0.00031000", "0.00069000", "Exchange"),
    ]
    (data_dir / "bip322.toml").write_text('[addresses]\n"bc1q" = 3\n')
    code, out, err = run(node, "-w", "watch", "report", "--from-height", "980", "--to-height", "1000", "-o", "bad")
    assert code == 2 and "[addresses] must map each address to the name of the wallet" in err and not Path("bad").exists()
