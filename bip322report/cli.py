"""``bip322-report``: the statement of a period for a holder, over every wallet under it, backed by verified BIP-322 proofs.

The statement goes to ``<holder>/statements/<label>/`` in the data directory (the one ``bip322 init``
made at or above the current directory; ``--dir`` or ``BIP322_DIR`` name another) as ``<holder>-<label>.pdf``
(``.html`` where the PDF cannot be made), ``.json``, ``.csv``, ``-history.pdf``, and a copy of each proofs file
it cites.  The histories come from the holder's node wallets (or the ``history.json`` kept in a wallet's
directory, for a wallet the node has not loaded), the proofs from the snapshots in the wallets' directories
under ``<holder>/``.  The holder is the word after the options, else the current directory's, else ``holder``
in ``bip322.toml``, else the tree's one.  ``-w`` names a wallet for ``history`` and ``balance``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bip322audit.audit import AuditError
from bip322audit.datadir import (
    HISTORY_FILE,
    Config,
    ConfigError,
    find_data_dir,
    holder_dir,
    holder_of_cwd,
    holder_statements_dir,
    holders,
    name_wallet,
    use_data_dir,
    wallet_dirs,
    wallet_of_cwd,
    wallets,
)
from bip322audit.proofs import find_proofs
from bip322audit.rpc import BitcoinCli, RpcError, btc
from bip322audit.snapshot import write_text_atomic
from bip322core._version import SPEC
from bip322core.cli import CLIError, add_help_command, emit
from bip322core.core import BIP322Error

from . import TOOL
from .coverage import pending_snapshots, proofs_names
from .fiat import Rates
from .history import History, fetch_history
from .holder import merge_histories
from .names import counterparts, name_by_wallets
from .period import Period, block, last_block_before, parse_when, period_between, period_for_heights, period_for_year
from .render import HEADINGS, PACKAGE_FONTS, THEMES, render_history_html, render_html, write_csv, write_pdf
from .report import build_report, format_summary, previous_statement

EXPLORER = "https://mempool.space"  # mainnet only: on another chain its links would lead to the wrong transactions
DUST_MAX = 10_000  # satoshi; a higher --dust would leave real coins out of the statement, so it needs --dust-force
DEFAULT_THEME = "journal"
STATEMENT_KEYS = ("dust", "theme", "heading", "explorer")  # the statement's own settings: the holder's table or the top level


def _opt(args, name: str):
    """A node option given after the subcommand wins over the same option given before it."""
    return getattr(args, f"{name}_sub", None) or getattr(args, name, None)


def _wallet_name(args, config: Config) -> str | None:
    """The wallet as named by the owner: ``-w``, else the wallet directory the command runs in, else ``wallet`` in bip322.toml; None when none says."""
    given = _opt(args, "wallet")
    if given:
        return name_wallet(str(given))
    here = wallet_of_cwd()
    if here is not None:
        return here
    named = config.get("wallet")
    return str(named) if named is not None else None


def _cli(args, config: Config, wallet: str | None, *, rpcwallet: bool = True) -> BitcoinCli:
    """The node as the options and bip322.toml describe it, on the wallet's node wallet when one is named (and ``rpcwallet``)."""
    cli = BitcoinCli(_opt(args, "cli") or config.get("cli", wallet) or "bitcoin-cli")
    if wallet is not None and rpcwallet:
        cli.argv.append(f"-rpcwallet={config.node_wallet(wallet)}")
    return cli


def _setting(args, config: Config, name: str, key: str, wallet: str | None, default=None, *, holder: str | None = None):
    """An option's value: the command line, else bip322.toml (the wallet's table, its holder's, then the top level), else ``default``."""
    given = getattr(args, name, None)
    return given if given is not None else config.get(key, wallet, default, holder=holder)


def _names(args, config: Config, history: History, period: Period, own: list[str]) -> dict[str, str]:
    """The other side of the period's movements named: ``[addresses]``, else the wallet of the tree whose node says the address is
    its own; ``own`` are the statement's wallets, whose addresses need no name."""
    others = {w: _cli(args, config, w) for w in wallets() if w not in own}
    return name_by_wallets(counterparts(history.between(period.start.height, period.end.height)), config.addresses, others, _progress)


def _add_node_args(p: argparse.ArgumentParser, *, wallet: bool = True) -> None:
    """The global options again, so that they may follow the command as well as precede it; ``-w`` only where a wallet is meant."""
    p.add_argument("--cli", dest="cli_sub", metavar="CMD", help="how to reach the node (may also be given before the command)")
    if wallet:
        p.add_argument("--wallet", "-w", dest="wallet_sub", metavar="NAME", help="the wallet (may also be given before the command)")
    p.add_argument("--dir", dest="dir_sub", metavar="DIR", help="the data directory (may also be given before the command)")


def _progress(line: str) -> None:
    print(line, file=sys.stderr)


def _history(args, cli: BitcoinCli) -> History:
    if getattr(args, "history", None):
        return History.load(Path(args.history))
    return fetch_history(cli, progress=_progress)


def _saved_history(wallet: str) -> Path | None:
    """The wallet's ``history.json`` in the tree, when the wallet is a wallet of the tree; None outside a tree.

    A tree laid out as earlier versions did is an error here as everywhere: silently not keeping the file would cost the wallet
    its only way into a statement once unloaded.
    """
    if find_data_dir() is None:
        return None
    path = wallet_dirs().get(wallet)
    return path / HISTORY_FILE if path is not None else None


def _keep_history(directory: Path, history: History) -> None:
    """The history read from the node, kept in the wallet's directory for when the wallet is not loaded any more."""
    write_text_atomic(directory / HISTORY_FILE, json.dumps(history.to_dict(), indent=2) + "\n")


def _wallet_history(args, config: Config, wallet: str | None, cli: BitcoinCli) -> tuple[History, dict]:
    """The wallet's history from the node, checked against the chain; or, when the node says the wallet is not loaded, from the
    ``history.json`` in its directory, provided the wallet held nothing at that history's tip (nothing can have left it since).
    Returns the history and where it came from: ``source`` node or file, with the tip and the time it was read."""
    try:
        history = fetch_history(cli, progress=_progress)
    except RpcError as exc:
        if wallet is None or "not loaded" not in str(exc):
            raise
        path = _saved_history(wallet)
        if path is None or not path.is_file():
            raise CLIError(
                f"{wallet} is not loaded on the node, and no history is kept in its directory: load it once (a statement keeps "
                f"the history it reads), or move its directory out from under the holder's"
            ) from exc
        history = History.load(path)
        held = history.balance_at(history.tip_height)
        if held:
            raise CLIError(
                f"{wallet} is not loaded on the node, and {path} ends at block {history.tip_height} with {btc(held)} BTC held: "
                "load the wallet, it may have spent since"
            ) from exc
        _check_history(_cli(args, config, wallet, rpcwallet=False), history, str(path))
        _progress(f"{wallet}: not loaded; from {path.name} saved {history.fetched_utc} at block {history.tip_height}, holding nothing")
        return history, {"source": "file", "tip_height": history.tip_height, "fetched_utc": history.fetched_utc}
    _check_history(cli, history, f"{wallet}'s history" if wallet else "the history")
    return history, {"source": "node", "tip_height": history.tip_height, "fetched_utc": history.fetched_utc}


def _check_history(cli: BitcoinCli, history: History, source: str) -> None:
    """A history is only good against the chain it was read from: same network, and its tip block still in the node's chain."""
    info = cli.call("getblockchaininfo")
    if info["chain"] != history.chain:
        raise CLIError(f"{source} is of the {history.chain} chain; the node is on {info['chain']}")
    if history.tip_height > int(info["blocks"]) or cli.block_hash(history.tip_height) != history.tip_hash:
        raise CLIError(
            f"{source}: its tip block {history.tip_height} ({history.tip_hash}) is not in the node's chain "
            "(a reorganisation, a node that is behind, or another network); read the history again"
        )


def _theme(args, config: Config, wallet: str | None, *, holder: str | None = None) -> str:
    theme = str(_setting(args, config, "theme", "theme", wallet, DEFAULT_THEME, holder=holder))
    if theme not in THEMES:
        raise CLIError(f"theme {theme!r} is not one of {', '.join(THEMES)}")
    return theme


def _engines(args) -> list[str] | None:
    return args.engines.split(",") if args.engines else None


def _period_given(args) -> bool:
    return any(getattr(args, k) is not None for k in ("year", "from_when", "to_when", "from_height", "to_height"))


def _whole_life(cli: BitcoinCli, history: History) -> Period:
    """From the block before the first transaction to the history's tip, labelled by the tip's date: the statement to date."""
    first = min((tx.height for tx in history.txs if tx.height is not None), default=history.tip_height)
    end = block(cli, history.tip_height)
    return Period(end.iso_time[:10], block(cli, max(first - 1, 0)), end, None, None, True)


def _period_label(args) -> str:
    """The period's label, from the options alone; refuses a period given in no way, in two ways, or by half."""
    year = args.year is not None
    dates = args.from_when is not None or args.to_when is not None
    heights = args.from_height is not None or args.to_height is not None
    if year + dates + heights != 1:
        raise CLIError("give the period one way: --year YEAR, --from WHEN --to WHEN, or --from-height H --to-height H")
    if dates and (args.from_when is None or args.to_when is None):
        raise CLIError("--from and --to go together")
    if heights and (args.from_height is None or args.to_height is None):
        raise CLIError("--from-height and --to-height go together")
    if year:
        return str(args.year)
    if dates:
        return f"{parse_when(args.from_when):%Y-%m-%d}..{parse_when(args.to_when):%Y-%m-%d}"
    return f"{args.from_height}..{args.to_height}"


def _proofs_roots(args, directories: list[Path]) -> list[Path]:
    """Where the proofs come from: the --snapshots paths, each holding a snapshot, or the holder's wallets' directories.

    A mistyped --snapshots path must not look like a wallet without proofs.
    """
    if args.snapshots:
        roots = [Path(p) for p in args.snapshots]
        for root in roots:
            if not root.exists():
                raise CLIError(f"--snapshots {root}: no such file or directory")
            if not find_proofs([root]) and not pending_snapshots([root]):  # snapshots awaiting signatures count
                raise CLIError(f"--snapshots {root}: no proofs file in it")
    else:
        roots = directories
    proofs_names([path for path, _ in find_proofs(roots)])  # refuses two files under one name before any work
    return roots


def _check_directory(directory: Path, force: bool) -> None:
    if directory.exists() and not directory.is_dir():
        raise CLIError(f"{directory} is not a directory")
    if directory.is_dir() and any(directory.iterdir()) and not force:
        raise CLIError(f"{directory} exists and is not empty (use --force to overwrite)")


def _pdf_possible() -> bool:
    """WeasyPrint and its system libraries are there: the statement can be a PDF."""
    try:
        import weasyprint  # noqa: F401
    except Exception:  # ImportError, or an OSError when its system libraries (Pango) are missing
        return False
    return True


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def cmd_block(args) -> int:
    cli = _cli(args, Config.load(), None)
    found = last_block_before(cli, parse_when(args.when))
    emit(json.dumps(found.to_dict(), indent=2), args.output)
    return 0


def cmd_history(args) -> int:
    config = Config.load()
    wallet = _wallet_name(args, config)
    history = fetch_history(_cli(args, config, wallet), progress=_progress)
    if wallet is None and history.wallet is not None:
        wallet = config.wallet_of_node(str(history.wallet))
    path = _saved_history(wallet) if wallet is not None else None
    if path is not None:
        _keep_history(path.parent, history)  # a wallet of the tree keeps it, for when it is not loaded any more
    emit(json.dumps(history.to_dict(), indent=2), args.output)
    print(
        json.dumps(
            {
                "wallet": history.wallet,
                "chain": history.chain,
                "tip": history.tip_height,
                "transactions": len(history.txs),
                "pending": len(history.pending),
            }
        ),
        file=sys.stderr,
    )
    return 0


def cmd_balance(args) -> int:
    config = Config.load()
    cli = _cli(args, config, _wallet_name(args, config))
    history = _history(args, cli)
    if args.height is not None:
        at = block(cli, args.height) if not args.history else None
        height = args.height
    elif args.at:
        if args.history:
            _check_history(cli, history, args.history)  # the date is resolved on the node: it must be on the history's chain
        at = last_block_before(cli, parse_when(args.at))
        height = at.height
    else:
        at, height = None, history.tip_height
    coins = history.coins_at(height)
    total = sum(c.amount_sat for c in coins)
    out = {
        "height": height,
        "block": at.to_dict() if at else None,
        "total_sat": total,
        "total_btc": btc(total),
        "coins": [c.to_dict() for c in coins],
    }
    emit(json.dumps(out, indent=2), args.output)
    return 0


def _rates(args) -> Rates | None:
    """The fiat valuation asked for with --rates or --rate, if any."""
    if args.rates is not None and args.rate is not None:
        raise CLIError("--rates and --rate exclude each other")
    if args.currency is not None and args.rates is None and args.rate is None:
        raise CLIError("--currency names the currency of --rates or --rate; give one of them")
    currency = args.currency or "FIAT"
    if args.rates is not None:
        return Rates.from_csv(currency, Path(args.rates))
    if args.rate is not None:
        return Rates.constant_rate(currency, args.rate)
    return None


def _dust(args, config: Config, wallet: str | None, *, holder: str | None = None) -> int:
    dust = _setting(args, config, "dust", "dust", wallet, 0, holder=holder)
    if not isinstance(dust, int) or isinstance(dust, bool):
        raise CLIError(f"dust must be a number of satoshi, not {dust!r}")
    if dust < 0:
        raise CLIError("--dust: a threshold cannot be negative")
    if dust > DUST_MAX and not args.dust_force:
        raise CLIError(f"--dust {dust}: more than {DUST_MAX} satoshi is not dust (--dust-force to leave such outputs out all the same)")
    return dust


def _period(args, cli: BitcoinCli, history: History, period_label: str) -> Period:
    """The period's opening and closing blocks, resolved on the node; never beyond the history."""
    # a period that runs to the tip closes on the history's own tip: a block that arrived since the history was read is not in it
    if args.year is not None:
        period = period_for_year(cli, args.year, tip_height=history.tip_height)
    elif args.from_when is not None:
        period = period_between(cli, parse_when(args.from_when), parse_when(args.to_when), period_label, tip_height=history.tip_height)
    else:
        period = period_for_heights(cli, args.from_height, args.to_height, period_label)
    if period.end.height > history.tip_height:
        raise CLIError(f"the history (tip {history.tip_height}) is older than the period's end block {period.end.height}; refresh it")
    return period


def _copy_proofs(report: dict, roots: list[Path], directory: Path) -> None:
    """What the reader needs next to the statement: each proofs file it cites, under its own name.

    Never the snapshot directories themselves (their PSBTs carry the wallet's xpubs), nor a proofs file the statement does not rely on.
    """
    cited = {f["file"] for f in report["proofs_files"]}
    for path, name in proofs_names([path for path, _ in find_proofs(roots)]).items():
        if name in cited:
            (directory / name).write_bytes(path.read_bytes())


def cmd_report(args) -> int:
    """The holder's statement: every wallet under its directory, their histories merged, for the period, or to date."""
    # what can be refused without the node is refused first: nothing is read, verified or written for a command that cannot finish
    if _opt(args, "wallet"):
        raise CLIError(
            "the statement is the holder's, over every wallet under it: bip322 report [HOLDER]; -w names a wallet for history and balance"
        )
    config = Config.load()
    found = holders()
    key = args.key or holder_of_cwd() or config.default_holder
    if key is None:
        if len(found) != 1:
            raise CLIError(
                "which holder? bip322 report HOLDER" + (f" (holders here: {', '.join(found)})" if found else ": none in the tree yet")
            )
        key = found[0]
    paths = {w: p for w, p in wallet_dirs().items() if p.parent.name == key} if key in found else {}  # the tree, read once
    members = sorted(paths)
    if not members:
        raise CLIError(
            f"no wallet under {holder_dir(key)} yet: the first snapshot of a wallet under it makes it"
            + (f" (holders here: {', '.join(found)})" if found else "")
        )
    for w in members:
        stray = [k for k in STATEMENT_KEYS if k in config.wallets.get(w, {})]
        if stray:
            _progress(
                f"note: {', '.join(stray)} in [wallets.{w}] is not read by the statement, which is the holder's: set it in [holders.{key}]"
            )
    period_label = _period_label(args) if _period_given(args) else None
    dust = _dust(args, config, None, holder=key)
    rates = _rates(args)
    theme = _theme(args, config, None, holder=key)
    pdf = not args.html and _pdf_possible()
    roots = _proofs_roots(args, [paths[w] for w in members])  # a given path is checked before any work
    if args.output:
        _check_directory(Path(args.output), args.force)
    elif period_label:
        _check_directory(holder_statements_dir(key) / period_label, args.force)  # the default directory is known before the long read

    histories: dict[str, History] = {}
    sources: dict[str, dict] = {}
    for w in members:
        _progress(f"{w}: reading the history")
        histories[w], sources[w] = _wallet_history(args, config, w, _cli(args, config, w))
        if sources[w]["source"] == "node":
            _keep_history(paths[w], histories[w])
    history = merge_histories(histories)
    loaded = [w for w in members if sources[w]["source"] == "node"]
    node_clis = {w: _cli(args, config, w) for w in loaded}  # each loaded wallet's node wallet: the listunspent check is per wallet
    cli = node_clis[loaded[0]] if loaded else _cli(args, config, members[0], rpcwallet=False)  # any one node for the chain-level work
    period = _period(args, cli, history, period_label) if period_label else _whole_life(cli, history)
    names = _names(args, config, history, period, members)
    directory = Path(args.output) if args.output else holder_statements_dir(key) / period.label
    _check_directory(directory, args.force)
    report = build_report(
        history,
        period,
        label=config.holder_name(key),
        proofs_roots=roots,
        cli=cli,
        rates=rates,
        engines=_engines(args),
        progress=_progress,
        dust_sat=dust,
        addresses=names,
        wallets=members,
        sources=sources,
        node_clis=node_clis,
    )
    report["previous"] = previous_statement(holder_statements_dir(key), report)
    return _write_statement(args, config, key, report, directory, f"{key}-{period.label}", roots, history.chain, theme, pdf)


def _write_statement(
    args, config: Config, holder: str, report: dict, directory: Path, stem: str, roots, chain: str, theme: str, pdf: bool
) -> int:
    """The JSON, the CSV, the proofs files cited and the PDF (or HTML) into the directory, the summary on stderr, the directory on stdout."""
    report["name"] = stem
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{stem}.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    write_csv(report, directory / f"{stem}.csv")
    if not args.no_proofs and roots:
        _copy_proofs(report, roots, directory)
    print(format_summary(report), file=sys.stderr)
    explorer = _setting(args, config, "explorer", "explorer", None, holder=holder)
    if explorer is None:
        explorer = EXPLORER if chain == "main" else None
    heading = _setting(args, config, "heading", "heading", None, holder=holder)
    if pdf:  # last: should the rendering fail, everything else is in place
        write_pdf(render_html(report, explorer=explorer, theme=theme, heading=heading, fonts_url=PACKAGE_FONTS), directory / f"{stem}.pdf")
        write_pdf(render_history_html(report, theme=theme, heading=heading, fonts_url=PACKAGE_FONTS), directory / f"{stem}-history.pdf")
    else:
        if not args.html:
            print(f"no PDF (WeasyPrint is not installed or lacks Pango): the statement is {stem}.html", file=sys.stderr)
        (directory / f"{stem}.html").write_text(render_html(report, explorer=explorer, theme=theme, heading=heading))
        (directory / f"{stem}-history.html").write_text(render_history_html(report, theme=theme, heading=heading))
    print(str(directory))
    return 0 if report["ok"] else 1


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #


def _add_period_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--year", "-y", type=int, metavar="YEAR", help="the calendar year, UTC")
    p.add_argument("--from", dest="from_when", metavar="WHEN", help="start instant (YYYY-MM-DD or ISO 8601, UTC); with --to")
    p.add_argument("--to", dest="to_when", metavar="WHEN", help="end instant, exclusive; in the future means up to the tip")
    p.add_argument("--from-height", type=int, metavar="H", help="opening block height; with --to-height")
    p.add_argument("--to-height", type=int, metavar="H", help="closing block height")


def _add_statement_args(p: argparse.ArgumentParser, where: str) -> None:
    """What every statement takes: dust, fiat, looks, output."""
    p.add_argument(
        "--dust",
        metavar="SATS",
        type=int,
        default=None,
        help=(
            "outputs of at most SATS satoshi received from others, in a transaction that spends nothing of the wallet's, "
            "and not spent by the closing block are dust: "
            "left out of every figure, and listed in the JSON; the wallet's own change and consolidations never are "
            "(default: dust in bip322.toml, else none)"
        ),
    )
    p.add_argument("--dust-force", action="store_true", help=f"accept a --dust above {DUST_MAX} satoshi")
    p.add_argument("--rates", metavar="CSV", help="date,rate rows (rate per BTC) for a fiat valuation of the movements")
    p.add_argument("--rate", metavar="RATE", help="a constant rate per BTC instead of --rates")
    p.add_argument("--currency", metavar="CODE", help="the fiat currency's name for --rates/--rate (default FIAT)")
    p.add_argument(
        "--explorer",
        default=None,
        metavar="URL",
        help=f"block explorer for links in the statement; '' for no links (default: explorer in bip322.toml, else {EXPLORER} on mainnet, none on other chains)",
    )
    p.add_argument("--html", action="store_true", help="write the statement as one HTML file instead of the PDF")
    p.add_argument(
        "--theme",
        choices=tuple(THEMES),
        default=None,
        help=f"page colours: a warm canvas with serif text (journal), Solarized light, white paper or Solarized dark (default: theme in bip322.toml, else {DEFAULT_THEME})",
    )
    p.add_argument(
        "--heading",
        choices=HEADINGS,
        help="section headings: coloured text over a rule (underline), a red tab over a grey rule (tab), the number in red (number), a thin red rule (redrule); default per theme",
    )
    p.add_argument("--no-proofs", action="store_true", help="do not copy the proofs files the statement cites next to it")
    p.add_argument("--engines", default=None, help="comma separated bip322 engines for re-verifying the proofs (default: all installed)")
    p.add_argument("--output", "-o", metavar="DIR", help=f"write the statement here instead of {where}")
    p.add_argument("--force", action="store_true", help="write into a non-empty directory")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bip322-report",
        description=(
            "The statement of a period for a holder: the holdings and movements of every wallet under it, in which every coin is "
            f"backed by a verified BIP-322 proof of control, from the owner's node ({SPEC}). With no command the statement is made: "
            "bip322-report [--cli CMD] [HOLDER] [--year Y | --from WHEN --to WHEN | --from-height H --to-height H] [options]. "
            "It goes to <holder>/statements/<label>/ in the data directory and its proofs come from the wallets' directories under "
            "<holder>/ there: the directory bip322 init made at or above the current one (--dir or BIP322_DIR name another), whose "
            "bip322.toml holds the defaults."
        ),
    )
    parser.add_argument("--version", action="version", version=f"{TOOL} ({SPEC})")
    parser.add_argument(
        "--cli",
        default=None,
        metavar="CMD",
        help='how to reach the node, e.g. "bitcoin-cli -signet" (default: bitcoin-cli, or cli in bip322.toml)',
    )
    parser.add_argument(
        "--wallet",
        "-w",
        default=None,
        metavar="NAME",
        help="the wallet, for history and balance: its directory NAME in the data directory, under whichever holder it is (KEY/NAME: "
        "the holder a new one goes under), and the node wallet of that name (or the one its [wallets.NAME] table names as node). "
        "Default: the wallet directory the command runs in, else wallet in bip322.toml, else the node's one loaded wallet. May also "
        "follow the command.",
    )
    parser.add_argument(
        "--dir",
        default=None,
        metavar="DIR",
        help="the data directory (default: the nearest directory at or above the current one with a bip322.toml, or BIP322_DIR). "
        "May also follow the command.",
    )
    sub = parser.add_subparsers(dest="command", metavar="command")
    sub.required = True

    p = sub.add_parser(
        "block",
        help="the last block before a UTC date or time",
        description="Height, hash and time of the last block whose header time is before the instant (YYYY-MM-DD is midnight UTC).",
    )
    p.add_argument("when", metavar="WHEN", help="YYYY-MM-DD or an ISO 8601 instant")
    _add_node_args(p)
    p.add_argument("--output", "-o", metavar="FILE", help="write the JSON here instead of stdout")
    p.set_defaults(func=cmd_block, examples=["block 2026-01-01", "block 2025-12-31T23:00:00Z"])

    p = sub.add_parser(
        "history",
        help="the node wallet's transaction history as JSON, kept in the wallet's directory when it is a wallet of the tree",
        description=(
            "Read every transaction of the node wallet (listtransactions, gettransaction, getaddressinfo) and resolve the wallet's side of each: "
            "outputs to the wallet, the wallet's outputs spent, outputs to outside, the exact fee. No index is needed. "
            "balance takes the result with --history; the statement reads a wallet the node has not loaded from the history.json "
            "kept in its directory, which this writes for a wallet of the tree, so a wallet about to be unloaded gets this run once."
        ),
    )
    _add_node_args(p)
    p.add_argument("--output", "-o", metavar="FILE", help="write history.json here instead of stdout")
    p.set_defaults(func=cmd_history, examples=["-w treasury history -o history.json"])

    p = sub.add_parser(
        "balance",
        help="the wallet's coins and balance at a height or date",
        description="The unspent outputs after every block up to the given height (or the last block before the date), from the history.",
    )
    _add_node_args(p)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--height", type=int, metavar="H")
    g.add_argument("--at", metavar="WHEN", help="YYYY-MM-DD or an ISO 8601 instant; the last block before it")
    p.add_argument("--history", metavar="FILE", help="use this history.json instead of reading the node wallet")
    p.add_argument("--output", "-o", metavar="FILE", help="write the JSON here instead of stdout")
    p.set_defaults(func=cmd_balance, examples=["-w treasury balance --at 2026-01-01", "balance --history history.json --height 912345"])

    p = sub.add_parser(
        "report",
        help="the holder's statement for a period, or to date: <holder>-<label>.pdf (or .html), .json, .csv, the proofs files it "
        "cites, and <holder>-<label>-history.pdf, the movements for a reader, under <holder>/statements/<label>/; the default command",
        description=(
            "One statement for a holder over every wallet under its directory: their histories merged, a move between two of them "
            "one transaction (sent by the one, received by the other, its fee the only cost), opening and closing holdings with each "
            "wallet's share, every movement between them, a reconciliation, and for each closing output the BIP-322 proof of control, "
            "from the wallets' snapshots, that lists it, re-verified against the node now. The period is a calendar year, two instants, "
            "or two heights; opening figures are as of the start block, closing figures as of the end block. Without a period the "
            "statement runs from the block before the first transaction to the tip, labelled by the tip's date. "
            "HOLDER is a directory of the tree, and may be left out inside its directory, with holder = KEY in bip322.toml, or when "
            "the tree has one holder; [holders.KEY] in bip322.toml gives its name for the header. "
            "The files go to <holder>/statements/<label>/ in the data directory, named <holder>-<label>.pdf (.html where WeasyPrint is "
            "not installed, or with --html), .json, .csv (one block per wallet, as that wallet sees the movements), and a copy of each "
            "proofs file the statement cites, under its own name. "
            "Exit status: 0 when the result is OK, 1 when it is ATTENTION, 2 on an error."
        ),
    )
    p.add_argument(
        "key",
        metavar="HOLDER",
        nargs="?",
        help="the holder: its directory in the data directory (default: the current directory's holder, holder in bip322.toml, else the "
        "tree's one); one named like a command (history, block, balance, handbook, help) is given as: report NAME",
    )
    _add_node_args(p, wallet=False)
    _add_period_args(p)
    p.add_argument(
        "--snapshots",
        metavar="DIR",
        action="append",
        help="take the proofs from here instead of the wallets' directories: a directory of snapshots, one snapshot, or one proofs file; may repeat",
    )
    _add_statement_args(p, "under HOLDER/statements/")
    p.set_defaults(
        func=cmd_report,
        examples=[
            "--year 2026",
            "company --year 2026 --dust 1000",
            "company --from 2026-01-01 --to 2026-07-01 --rates sek.csv --currency SEK",
            "company -o source-of-funds",
        ],
    )

    p = sub.add_parser("handbook", help="print the handbook: the yearly flow end to end, for the holder and for the auditor")
    p.set_defaults(func=cmd_handbook)

    add_help_command("bip322-report", sub, {"Workflow": ["history", "block", "balance", "report", "handbook", "help"]})
    return parser


def cmd_handbook(args) -> int:
    """The whole flow, holder's and auditor's, as shipped with the package."""
    print((Path(__file__).parent / "handbook.md").read_text(), end="")
    return 0


COMMANDS = ("history", "block", "balance", "report", "handbook", "help")
_GLOBAL_WITH_VALUE = ("--cli", "--wallet", "-w", "--dir")


def with_default_command(argv: list[str]) -> list[str]:
    """``bip322-report OPTIONS`` is ``bip322-report report OPTIONS``: the statement is what the command is for.

    The first word after the global options decides: a command name, or
    ``-h``/``--help``/``--version``, is left alone; anything else (a holder,
    ``bip322 report company``, an option of the statement, or nothing at all)
    gets ``report``.  No argument at all lists the commands.
    """
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in _GLOBAL_WITH_VALUE:
            i += 2
        elif tok.startswith(("--cli=", "--wallet=", "--dir=")) or (tok.startswith("-w") and not tok.startswith("--")):
            i += 1
        elif tok in COMMANDS or tok in ("-h", "--help", "--version"):
            return argv
        else:
            return [*argv[:i], "report", *argv[i:]]
    return [*argv, "report"] if argv else ["help"]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(with_default_command(sys.argv[1:] if argv is None else list(argv)))
    use_data_dir(_opt(args, "dir"))
    try:
        return args.func(args)
    except (CLIError, ConfigError, BIP322Error, RpcError, AuditError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: {exc.strerror or exc}: {exc.filename}" if getattr(exc, "filename", None) else f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
