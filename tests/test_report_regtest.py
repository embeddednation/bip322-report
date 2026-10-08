"""End to end on a real regtest Bitcoin Core: fund, prove, spend, prove only the change, report.

Skipped when no Bitcoin Core binary is found: set BITCOIN_CORE_DIR to a Core install (the directory holding bin/),
or run refcheck/fetch.sh in a bip322-core checkout that is installed alongside.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from bip322audit.audit import finalize_bundle
from bip322audit.ledger import proven_addresses
from bip322audit.rpc import BitcoinCli
from bip322audit.snapshot import take_snapshot, write_bundle
from bip322audit.testing import Node, find_bitcoin_core
from bip322core.dev.signing import sign_psbt
from bip322core.dev.testing import ORIGIN_PATH
from bip322core.psbt import parse_psbt
from bip322core.wallet import Wallet
from embit.networks import NETWORKS

from bip322report.history import fetch_history
from bip322report.period import period_for_heights, period_for_year
from bip322report.render import render_html
from bip322report.report import build_report

ROOT = Path(__file__).resolve().parent.parent


CORE_DIR = find_bitcoin_core()
YEAR = datetime.now(UTC).year  # the blocks are mined now

pytestmark = pytest.mark.skipif(
    CORE_DIR is None, reason="no Bitcoin Core binary (set BITCOIN_CORE_DIR or run refcheck/fetch.sh in bip322-core)"
)


@pytest.fixture(scope="module")
def core(tmp_path_factory):
    daemon = Node(CORE_DIR, tmp_path_factory.mktemp("core"))
    daemon.start()
    try:
        yield daemon
    finally:
        daemon.stop()


@pytest.fixture(scope="module")
def regtest_wallet(wallet):
    return Wallet.from_descriptor(wallet.to_descriptor(), network="regtest")


@pytest.fixture(scope="module")
def private_descriptors(masters):
    out = []
    for holder in range(2):
        keys = []
        for j, m in enumerate(masters):
            account = m.derive("m/" + ORIGIN_PATH)
            text = (
                account.to_base58(NETWORKS["regtest"]["xprv"])
                if j == holder
                else account.to_public().to_base58(NETWORKS["regtest"]["xpub"])
            )
            keys.append(f"[{m.my_fingerprint.hex()}/{ORIGIN_PATH}]{text}/<0;1>/*")
        out.append("wsh(sortedmulti(2," + ",".join(keys) + "))")
    return out


def _bundle(directory: Path, watch: BitcoinCli, wallet: Wallet, signers, template: str, skip=None, addresses=None) -> dict:
    snapshot, psbts = take_snapshot(watch, wallet, template, depth=1, skip_addresses=skip, addresses=addresses)
    write_bundle(directory, snapshot, psbts)
    for entry in snapshot.addresses:
        psbt = parse_psbt((directory / entry["file"]).read_text())
        for signer in signers[:2]:
            assert sign_psbt(psbt, signer) == 1
        (directory / "signed" / Path(entry["file"]).name.replace(".psbt", "-part.psbt")).write_text(psbt.to_string())
    document = finalize_bundle(directory, cli=watch)
    (directory / "proofs.json").write_text(json.dumps(document, indent=2))
    return document


def test_reports_workflow_on_regtest(core, regtest_wallet, signer_expressions, private_descriptors, tmp_path):
    node = BitcoinCli(core.cli_argv())
    node.call("createwallet", "miner")
    miner = BitcoinCli([*core.cli_argv(), "-rpcwallet=miner"])
    watch = BitcoinCli([*core.cli_argv(), "-rpcwallet=watch"])
    mine_to = miner.call("getnewaddress")
    miner.call("generatetoaddress", 101, mine_to)
    node.call("createwallet", "watch", True, True, "", False, True)
    descriptors = [{"desc": d, "timestamp": "now", "range": [0, 50], "active": False} for d in regtest_wallet.core_descriptors()]
    assert all(r["success"] for r in watch.call("importdescriptors", descriptors))
    a0, a1 = regtest_wallet.derive(0).address, regtest_wallet.derive(2, 1).address
    miner.call("sendtoaddress", a0, 0.5)
    miner.call("sendtoaddress", a1, 0.1)
    miner.call("generatetoaddress", 2, mine_to)  # funding at 102, tip 103
    ledger = tmp_path / "ledger"

    # ---- the owner proves everything once ------------------------------------ #
    first = _bundle(ledger / "snapshot-first", watch, regtest_wallet, signer_expressions, "Owner proof {date}")
    assert len(first["proofs"]) == 2 and first["total_sat"] == 60_000_000

    # ---- a spend: 0.2 out, change back to the wallet. The change address is proven BEFORE broadcasting --- #
    change = regtest_wallet.derive(5, 1).address
    funded = watch.call("walletcreatefundedpsbt", [], [{mine_to: 0.2}], 0, {"subtractFeeFromOutputs": [0], "changeAddress": change})
    decoded = node.call("decodepsbt", funded["psbt"])
    assert change in [o["scriptPubKey"]["address"] for o in decoded["tx"]["vout"]]
    second = _bundle(ledger / "snapshot-change", watch, regtest_wallet, signer_expressions, "Owner proof {date}", addresses=[change])
    assert second["proofs"][0]["address"] == change and second["proofs"][0]["utxos"] == []
    signed = node.call("descriptorprocesspsbt", funded["psbt"], private_descriptors)
    assert signed["complete"]
    spend_txid = node.call("sendrawtransaction", signed["hex"])
    miner.call("generatetoaddress", 2, mine_to)  # spend at 104, tip 105
    with pytest.raises(Exception, match="already proven"):  # nothing left to prove: the change address is covered
        take_snapshot(watch, regtest_wallet, "x", depth=1, skip_addresses=proven_addresses([ledger]))

    # ---- the report over the whole life of the wallet -------------------------- #
    history = fetch_history(watch)
    assert len(history.txs) == 3 and history.balance_at(103) == 60_000_000 and history.tip_height == 105
    spend = next(tx for tx in history.txs if tx.txid == spend_txid)
    assert (
        spend.kind == "send"
        and spend.fee_sat
        and spend.net_sat == -sum(c.amount_sat for c in spend.ours_in) + sum(c.amount_sat for c in spend.ours_out)
    )
    period = period_for_year(watch, YEAR)  # regtest genesis is 2011; the year has not ended, so the period runs to the tip
    assert period.start.height == 0 and period.end.height == 105 and period.to_tip
    report = build_report(history, period, label="regtest", ledger_roots=[ledger], cli=watch)
    assert report["ok"], json.dumps(report["coverage"], indent=2) + json.dumps(report["node_check"])
    assert report["opening"]["total_sat"] == 0 and report["closing"]["total_sat"] == history.balance_at(105)
    assert report["totals"]["received_sat"] == 60_000_000 and report["totals"]["fees_sat"] == spend.fee_sat
    assert report["totals"]["sent_sat"] == 20_000_000 - spend.fee_sat  # the fee was taken from the 0.2 sent
    closing = history.coins_at(105)  # Core's coin selection decides whether the 0.1 output survives the spend
    assert report["coverage"]["complete"] and report["coverage"]["total_count"] == len(closing) in (1, 2)
    used = {b["bundle"].split("/")[0]: b["used_for"] for b in report["bundles"]}
    relied_on = {"snapshot-first": len(closing) - 1, "snapshot-change": 1}  # a bundle no closing UTXO relies on is not listed, only counted
    assert used == {k: n for k, n in relied_on.items() if n} and report["bundles_not_used"] == 2 - len(used)
    assert all(b["verified"] for b in report["bundles"])
    change_row = next(c for c in report["closing"]["coins"] if c["address"] == change)
    assert change_row["proof"]["before_output"] and not change_row["proof"]["lists_output"]
    assert report["node_check"]["ok"] and report["reconciliation"]["ok"]
    html = render_html(report)
    assert "pill bad" not in html and spend_txid in html

    # ---- a sub-period: only the spend, opening balance from before it ----------- #
    part = build_report(history, period_for_heights(watch, 103, 105), label="regtest", ledger_roots=[ledger], cli=watch)
    assert part["opening"]["total_sat"] == 60_000_000 and len(part["transactions"]) == 1 and part["ok"]

    # ---- the CLI, driving the same node through bitcoin-cli ------------------- #
    import bip322report.cli as cli_module

    cli_arg = " ".join(core.cli_argv())
    out = tmp_path / "report"
    assert cli_module.main(["--cli", cli_arg, "-w", "watch", "report", "--year", str(YEAR), "--ledger", str(ledger), "-o", str(out)]) == 0
    written = json.loads((out / "report.json").read_text())
    assert written["ok"] and written["label"] == "watch" and (out / "report.html").exists() and (out / "report.csv").exists()
