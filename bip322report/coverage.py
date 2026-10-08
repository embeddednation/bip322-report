"""Which proof covers which coin.

The ledger (any directory tree of bip322-audit bundles) is read and every
``proofs.json`` in it is re-verified against the node at report time.  A coin
is *covered* when a verified proof exists for its address: a BIP-322 proof is
about the script behind an address, so it covers every output paid there,
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
from bip322audit.ledger import find_proofs, outpoints_in
from bip322audit.rpc import BitcoinCli

from .history import Coin, Outpoint


@dataclass
class Bundle:
    """One proofs document from the ledger and the result of verifying it now."""

    path: Path
    document: dict
    report: dict | None  # verify_proofs output; None when verification could not run
    error: str | None = None
    name: str = ""  # how the bundle is named in the report: relative to the ledger it came from, never an absolute path

    unusable: bool = False  # the document lacks what a proofs file must hold: it backs nothing and is never cited

    @property
    def stamp(self) -> dict:
        return self.document["stamp"]

    @property
    def message(self) -> str:
        return self.document["message"]

    @property
    def ok(self) -> bool:
        """The bundle's proofs stand: nothing failed.  Outputs spent since the bundle was made leave bip322-audit's
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
            "bundle": self.name,
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

    bundle: Bundle
    address: str
    signature: str
    variant: str
    state: str  # the signature's verdict now
    lists_output: bool  # the bundle's snapshot listed this very output
    coin_height: int | None = None

    @property
    def verified(self) -> bool:
        return self.state == "valid" and self.bundle.ok

    @property
    def before_output(self) -> bool:
        """The proof was made before the output existed (an address proven ahead of use)."""
        return self.coin_height is not None and int(self.bundle.stamp["height"]) < self.coin_height

    def to_dict(self) -> dict:
        verdict = self.bundle.verdict(self.address) or {}
        return {
            "bundle": self.bundle.name,
            "message": self.bundle.message,
            "message_hex": self.bundle.document.get("message_hex"),
            "stamp": self.bundle.stamp,
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


def load_ledger(cli: BitcoinCli | None, roots, *, engines=None, progress=None) -> list[Bundle]:
    """Every proofs document under the roots, verified against the node (signatures only when ``cli`` is None).

    A document that lacks what a proofs file must hold is kept as an unusable
    bundle with the reason: it is never verified, backs nothing and is never
    cited (``progress`` is told, so a holder sees why).
    """
    found = find_proofs(roots)
    names = bundle_names([path for path, _ in found], roots)
    bundles = []
    for path, document in found:
        flaw = _flaw(document)
        if flaw:
            if progress:
                progress(f"{path}: not a usable proofs file ({flaw}); left out")
            bundles.append(Bundle(path, document, None, flaw, name=names[path], unusable=True))
            continue
        if progress:
            progress(f"verifying {path}")
        try:
            report = verify_proofs(document, cli, engines=engines)
            bundles.append(Bundle(path, document, report, name=names[path]))
        except AuditError as exc:
            bundles.append(Bundle(path, document, None, str(exc), name=names[path]))
    return bundles


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


def _base(path: Path, roots) -> Path:
    """The directory a bundle is named from: its ledger root, or the bundle's parent when the root is the bundle itself.

    ``path`` is the bundle's ``proofs.json``, resolved.  Of nested roots the
    outermost wins, so the order of ``--ledger`` options does not matter.
    """
    bases = []
    for root in roots:
        root = Path(root).resolve()
        if root in (path, path.parent):
            bases.append(path.parent.parent)  # --ledger DIR/bundle or DIR/bundle/proofs.json: named bundle/proofs.json, not proofs.json
        elif root in path.parents:
            bases.append(root)
    return min(bases, key=lambda b: len(b.parts)) if bases else path.parent.parent


def bundle_names(paths, roots) -> dict[Path, str]:
    """How each ``proofs.json`` is named in the statement and under ``<DIR>/ledger/``: relative, unique, never absolute.

    ``<bundle dir>/proofs.json`` relative to the ledger root.  Two ledgers
    that hold equally named bundles (``2025/year-end`` and ``2026/year-end``)
    get the root's own directory in front, which is the name they would have
    had with the common parent as the ledger.  Names still equal after that
    are refused: a statement that cites two files under one name cannot be
    followed.
    """
    paths = list(dict.fromkeys(Path(p).resolve() for p in paths))
    bases = {p: _base(p, roots) for p in paths}
    names = {p: p.relative_to(bases[p]).as_posix() for p in paths}
    clashing = {n for n in names.values() if list(names.values()).count(n) > 1}
    for p in paths:
        if names[p] in clashing:
            names[p] = p.relative_to(bases[p].parent).as_posix()
    seen: dict[str, Path] = {}
    for p in paths:
        if names[p] in seen:
            raise ValueError(
                f"two bundles would both be named {names[p]} ({seen[names[p]]} and {p}): give a common parent directory as the ledger, or rename one"
            )
        seen[names[p]] = p
    return names


def cover_coins(coins: list[Coin], bundles: list[Bundle]) -> dict[Outpoint, list[Cover]]:
    """For each coin, the proofs for its address, best first (verified, then listing the output, then latest stamp)."""
    by_outpoint: dict[Outpoint, list[Cover]] = {c.outpoint: [] for c in coins}
    at_address: dict[str, list[Coin]] = {}
    for coin in coins:
        at_address.setdefault(coin.address, []).append(coin)
    for bundle in bundles:
        if bundle.unusable:
            continue
        for proof in bundle.document["proofs"]:
            listed = {(u["txid"], int(u["vout"])) for u in proof["utxos"]}
            for coin in at_address.get(proof["address"], ()):
                by_outpoint[coin.outpoint].append(
                    Cover(
                        bundle,
                        proof["address"],
                        proof["signature"],
                        proof.get("variant", proof["signature"][:3]),
                        bundle.proof_state(proof["address"]),
                        coin.outpoint in listed,
                        coin.height,
                    )
                )
    for covers in by_outpoint.values():
        covers.sort(key=lambda c: (not c.verified, not c.lists_output, -int(c.bundle.stamp["height"])))
    return by_outpoint


def pending_bundles(roots, addresses=None) -> list[Path]:
    """Snapshot directories that have no proofs.json yet: signatures still to collect.

    With ``addresses``, only the snapshots that list one of them: a ledger may
    hold another wallet's bundles, which are not this statement's business.
    """
    out = []
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for snap in sorted(root.rglob("snapshot.json")):
            if (snap.parent / "proofs.json").exists() or snap.parent in out:
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


def pending_name(directory: Path, roots) -> str:
    """A pending bundle's directory, named as its proofs.json will be."""
    path = Path(directory).resolve() / "proofs.json"
    return path.parent.relative_to(_base(path, roots)).as_posix()
