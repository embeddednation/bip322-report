"""Which proof covers which coin.

The wallets' proofs files (in their directories, or wherever ``--snapshots``
points) are read and each is re-verified against the node at report time.  A
coin is *covered* when a verified proof exists for its address: a BIP-322 proof
is about the script behind an address, so it covers every output paid there,
whether the proof was made before the output existed (a change address proven
before the spend was broadcast) or after (a snapshot that listed it).  Among
several proofs the verified ones come first, then those whose snapshot listed
the exact output, then the latest stamp; all are kept for the record.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from bip322audit.audit import AuditError, verify_proofs
from bip322audit.datadir import proofs_file_in
from bip322audit.proofs import find_proofs, outpoints_in
from bip322audit.rpc import BitcoinCli

from .history import Coin, Outpoint


@dataclass
class ProofsFile:
    """One proofs file and the result of verifying it now."""

    path: Path
    document: dict
    report: dict | None  # verify_proofs output; None when verification could not run
    error: str | None = None
    name: str = ""  # how the file is named in the statement and copied beside it: its own name, never a path

    unusable: bool = False  # the document lacks what a proofs file must hold: it backs nothing and is never cited

    @property
    def stamp(self) -> dict:
        return self.document["stamp"]

    @property
    def message(self) -> str:
        return self.document["message"]

    @property
    def ok(self) -> bool:
        """The file's proofs stand: nothing failed.  Outputs spent since the snapshot leave bip322-audit's
        verdict incomplete, which is no flaw of the proofs; each closing UTXO is looked up on the chain by the report itself."""
        if not self.report:
            return False
        return self.report.get("result", "ok" if self.report.get("ok") else "failed") != "failed"

    def proof_state(self, address: str) -> str:
        verdict = self.verdict(address)
        return verdict["state"] if verdict else ("not verified" if not self.report else "missing")

    def verdict(self, address: str) -> dict | None:
        """The verifier's own record for one address: state, tool, engines."""
        if not self.report:
            return None
        for row in self.report["proofs"]:
            if row["address"] == address:
                return row["bip322"]
        return None

    def to_dict(self) -> dict:
        s = (self.report or {}).get("summary") or {}
        return {
            "file": self.name,
            "stamp": self.stamp,
            "message": self.message,
            "finalized_utc": self.document.get("finalized_utc"),
            "policy": self.document.get("policy"),
            "outputs": len(outpoints_in(self.document)),
            "total_sat": self.document.get("total_sat"),
            "verified": self.ok,
            "signatures": f"{sum(1 for p in (self.report or {}).get('proofs', []) if p['bip322']['state'] == 'valid')}/{len(self.document['proofs'])}",
            "stamp_ok": s.get("stamp_ok"),
            "contradictions": s.get("contradictions"),
            "error": self.error,
        }


@dataclass
class Cover:
    """The proof chosen for one coin."""

    proofs: ProofsFile
    address: str
    signature: str
    variant: str
    state: str  # the signature's verdict now
    lists_output: bool  # the snapshot listed this very output
    coin_height: int | None = None

    @property
    def verified(self) -> bool:
        return self.state == "valid" and self.proofs.ok

    @property
    def before_output(self) -> bool:
        """The proof was made before the output existed (an address proven ahead of use)."""
        return self.coin_height is not None and int(self.proofs.stamp["height"]) < self.coin_height

    def to_dict(self) -> dict:
        verdict = self.proofs.verdict(self.address) or {}
        return {
            "file": self.proofs.name,
            "message": self.proofs.message,
            "message_hex": self.proofs.document.get("message_hex"),
            "stamp": self.proofs.stamp,
            "address": self.address,
            "signature": self.signature,
            "variant": self.variant,
            "state": self.state,
            "verified": self.verified,
            "verifier": verdict.get("tool"),
            "engines": _engine_names(verdict.get("engines", [])),
            "verdict": {k: verdict.get(k) for k in ("state", "reason", "engines")},
            "lists_output": self.lists_output,
            "before_output": self.before_output,
        }


def _engine_names(runs) -> list[str]:
    """The script engines that passed, by public name and version, once each (the verifier runs btclib with two rule sets)."""
    names = {"btclib": "btclib", "kernel": "libbitcoinkernel"}
    out: list[str] = []
    for run in runs:
        if not isinstance(run, dict) or not run.get("ok"):
            continue
        name = names.get(str(run.get("engine", "")).split("-")[0], str(run.get("engine")))
        label = f"{name} {run['version']}" if run.get("version") else name
        if label not in out:
            out.append(label)
    return out


def load_proofs_files(cli: BitcoinCli | None, roots, *, engines=None, progress=None) -> list[ProofsFile]:
    """Every proofs file under the roots, verified against the node (signatures only when ``cli`` is None).

    A document that lacks what a proofs file must hold is kept as unusable
    with the reason: it is never verified, backs nothing and is never cited
    (``progress`` is told, so a holder sees why).
    """
    found = find_proofs(roots)
    names = proofs_names([path for path, _ in found])
    files = []
    for path, document in found:
        flaw = _flaw(document)
        if flaw:
            if progress:
                progress(f"{path}: not a usable proofs file ({flaw}); left out")
            files.append(ProofsFile(path, document, None, flaw, name=names[path], unusable=True))
            continue
        if progress:
            progress(f"verifying {path}")
        try:
            report = verify_proofs(document, cli, engines=engines)
            files.append(ProofsFile(path, document, report, name=names[path]))
        except AuditError as exc:
            files.append(ProofsFile(path, document, None, str(exc), name=names[path]))
    return files


def _flaw(document: dict) -> str | None:
    """What a proofs document lacks of the shape this package and the verifier read; None when it is whole."""
    if not isinstance(document.get("message"), str):
        return "no message"
    stamp = document.get("stamp")
    if not isinstance(stamp, dict) or not str(stamp.get("height", "")).isdigit() or not stamp.get("hash") or not stamp.get("time"):
        return "stamp without height, hash or time"
    for n, proof in enumerate(document["proofs"], 1):
        if not isinstance(proof, dict) or not all(isinstance(proof.get(k), str) and proof[k] for k in ("address", "signature")):
            return f"proof {n} without address or signature"
        utxos = proof.get("utxos")
        if not isinstance(utxos, list) or not all(isinstance(u, dict) and "txid" in u and str(u.get("vout", "")).isdigit() for u in utxos):
            return f"proof {n} without a list of utxos (txid, vout)"
    return None


def proofs_names(paths) -> dict[Path, str]:
    """How each proofs file is named in the statement and copied beside it: its own file name, never a path.

    ``finalize`` names a proofs file ``<wallet>-<date>-<height>-proofs.json``,
    which says what it is wherever it ends up; a file still named
    ``proofs.json`` (earlier versions) is named after its snapshot directory.
    Two files under one name are refused: a statement that cites two files
    under one name cannot be followed.
    """
    names: dict[Path, str] = {}
    for path in dict.fromkeys(Path(p).resolve() for p in paths):
        name = path.name if path.name != "proofs.json" else f"{path.parent.name}-proofs.json"
        for other, taken in names.items():
            if taken == name:
                raise ValueError(f"two proofs files would both be named {name} ({other} and {path}): rename one")
        names[path] = name
    return names


def cover_coins(coins: list[Coin], files: list[ProofsFile]) -> dict[Outpoint, list[Cover]]:
    """For each coin, the proofs for its address, best first (verified, then listing the output, then latest stamp)."""
    by_outpoint: dict[Outpoint, list[Cover]] = {c.outpoint: [] for c in coins}
    at_address: dict[str, list[Coin]] = {}
    for coin in coins:
        at_address.setdefault(coin.address, []).append(coin)
    for file in files:
        if file.unusable:
            continue
        for proof in file.document["proofs"]:
            listed = {(u["txid"], int(u["vout"])) for u in proof["utxos"]}
            for coin in at_address.get(proof["address"], ()):
                by_outpoint[coin.outpoint].append(
                    Cover(
                        file,
                        proof["address"],
                        proof["signature"],
                        proof.get("variant", proof["signature"][:3]),
                        file.proof_state(proof["address"]),
                        coin.outpoint in listed,
                        coin.height,
                    )
                )
    for covers in by_outpoint.values():
        covers.sort(key=lambda c: (not c.verified, not c.lists_output, -int(c.proofs.stamp["height"])))
    return by_outpoint


def pending_snapshots(roots, addresses=None) -> list[Path]:
    """Snapshot directories that have no proofs file yet: signatures still to collect.

    With ``addresses``, only the snapshots that list one of them: a directory
    may hold another wallet's snapshots, which are not this statement's business.
    """
    out = []
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for snap in sorted(root.rglob("snapshot.json")):
            if proofs_file_in(snap.parent) is not None or snap.parent in out:
                continue
            if addresses is None or _snapshot_addresses(snap) & set(addresses):
                out.append(snap.parent)
    return out


def _snapshot_addresses(path: Path) -> set[str]:
    """The addresses a snapshot.json asks proofs for; empty when it cannot be read."""
    try:
        rows = json.loads(path.read_text()).get("addresses", [])
        return {row["address"] for row in rows if isinstance(row, dict) and isinstance(row.get("address"), str)}
    except (OSError, ValueError, AttributeError, TypeError):
        return set()
