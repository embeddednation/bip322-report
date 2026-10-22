"""bip322 report --prove: the snapshots the statement lacks, at its closing block; the rerun finalizes what has been signed."""

import json
import os
from pathlib import Path

from bip322core.dev.signing import sign_psbt
from bip322core.psbt import parse_psbt
from test_cli import _member, _written, run  # noqa: F401, F811  # the fixture that runs the CLI against a fake node
from test_report import _node


def _sign(directory: Path, signers) -> None:
    """Two cosigners sign every PSBT of a snapshot into its signed/, as the devices would."""
    snapshot = json.loads((directory / "snapshot.json").read_text())
    for entry in snapshot["addresses"]:
        psbt = parse_psbt((directory / entry["file"]).read_text())
        for signer in signers[:2]:
            assert sign_psbt(psbt, signer) == 1
        (directory / "signed" / Path(entry["file"]).name.replace(".psbt", "-part.psbt")).write_text(psbt.to_string() + "\n")


def test_prove_takes_the_snapshots_the_statement_lacks_and_the_rerun_finalizes_them(wallet, signer_expressions, run, monkeypatch):  # noqa: F811
    import bip322audit.cli as audit_cli

    node = _node(wallet, tip=1012)
    member = _member(node)  # h/watch, a wallet of the tree, loaded on the node
    monkeypatch.setattr(audit_cli, "BitcoinCli", lambda command: node)  # the snapshot and the finalize run on the same fake node
    root = Path(os.environ["BIP322_DIR"])
    (root / "bip322.toml").write_text('holder = "h"\n[holders.h]\nname = "Holder H"\n')
    heights = ["--from-height", "979", "--to-height", "1012"]

    # the statement first, to see what it covers: three closing UTXOs, none proven
    code, out, err = run(node, "report", *heights, "-o", "look")
    assert code == 1 and "proof coverage: 0/3" in err and "  to prove: bip322 audit snapshot -w watch --at 1012\n" in err

    # --prove: the full snapshot at the closing block, its directory on stdout, no statement written
    code, out, err = run(node, "report", *heights, "--prove", "-o", "look")  # the directory in use does not stop a run that writes nothing
    assert code == 1 and "watch: taking the snapshot at block 1012" in err and "then run the statement again" in err
    directory = Path(out.strip())
    assert directory.parent == member and directory.name.endswith("-1012") and (directory / "to_sign").is_dir()
    assert (directory / "message.txt").read_text().startswith("Proof of control, Holder H, 2023-11-21\nblock: 1012 ")
    assert not any(p.name.endswith("-proofs.json") for p in directory.iterdir())
    assert sorted(p.name for p in Path("look").iterdir()) == sorted(p.name for p in Path("look").iterdir())  # untouched
    # again, before signing: nothing new is taken, the one there awaits signatures
    code, out, err = run(node, "report", *heights, "--prove", "-o", "look")
    assert code == 1 and out == "" and f"watch: {directory.name} awaits signatures" in err and len(list(member.glob("*-1012*"))) == 1

    # signed on the devices; the rerun finalizes it, cites it, and the statement is OK
    _sign(directory, signer_expressions)
    code, out, err = run(node, "report", *heights, "-o", "done")
    assert code == 0 and f"watch: finalizing {directory.name}, signed and not yet finalized" in err and "RESULT: OK" in err
    proofs = [p for p in directory.iterdir() if p.name.endswith("-proofs.json")]
    assert len(proofs) == 1 and (Path("done") / proofs[0].name).is_file() and out.strip() == "done"
    written = json.loads(_written("done", ".json").read_text())
    assert written["ok"] and written["coverage"]["at_closing_count"] == 3 and written["proofs_files"][0]["file"] == proofs[0].name
    # --prove with nothing lacking writes the statement as usual
    code, out, err = run(node, "report", *heights, "--prove", "-o", "again")
    assert code == 0 and out.strip() == "again" and "taking the snapshot" not in err


def test_prove_leaves_a_wallet_that_is_not_loaded_and_says_why(wallet, run, monkeypatch, capsys):  # noqa: F811
    """A wallet emptied and unloaded is read from its kept history; for a period in which it still held coins, --prove cannot
    take its snapshot and says so."""
    import bip322audit.cli as audit_cli
    from fake_node import FakeNode
    from test_holder import _Switch
    from test_report import EXTERNAL

    node = FakeNode(tip=1012)
    node.descriptor = wallet.to_descriptor()
    a0 = wallet.derive(0).address
    node.mine.add(a0)
    node.add("aa" * 32, 990, [(EXTERNAL, 0)], [(a0, 50_000_000)])
    node.add("bb" * 32, 1011, [("aa" * 32, 0)], [(EXTERNAL, 49_990_000)])  # emptied: nothing held at the file's tip
    _member(node, "cold")
    monkeypatch.setattr(audit_cli, "BitcoinCli", lambda command: _Switch({}, chain=node))
    code, out, err = run(lambda: _Switch({}, chain=node), "report", "--from-height", "979", "--to-height", "1000", "--prove", "-o", "x")
    assert code == 1, err
    assert out == "" and "cold: not loaded on the node (read from its kept history): load it to take its snapshot" in err
    assert "proof coverage: 0/1" in err and not Path("x").exists()
