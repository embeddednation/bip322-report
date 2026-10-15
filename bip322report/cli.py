"""``bip322-report``: the statement of a period, backed by verified BIP-322 proofs, from the owner's node wallet.

The statement goes to ``<wallet>/statements/<label>/`` in the data directory (the one ``bip322 init``
made at or above the current directory; ``--dir`` or ``BIP322_DIR`` name another) as ``<wallet>-<label>.pdf``
(``.html`` where the PDF cannot be made), ``.json``, ``.csv``, and a copy of each proofs file it cites;
the proofs come from ``<wallet>/snapshots/``.  ``-w`` names the wallet, else the wallet directory the
command runs in does, else ``bip322.toml``, else the node's one loaded wallet is it.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

from bip322audit.audit import AuditError
from bip322audit.datadir import Config, ConfigError, snapshots_dir, statements_dir, use_data_dir, wallet_of_cwd
from bip322audit.proofs import find_proofs
from bip322audit.rpc import BitcoinCli, RpcError, btc
from bip322core._version import SPEC
from bip322core.cli import CLIError, add_help_command, emit
from bip322core.core import BIP322Error

from . import TOOL
from .coverage import pending_snapshots, proofs_names
from .fiat import Rates
from .history import History, fetch_history
from .period import Period, block, last_block_before, parse_when, period_between, period_for_heights, period_for_year
from .render import HEADINGS, PACKAGE_FONTS, THEMES, render_html, write_csv, write_pdf
from .report import build_report, format_summary

EXPLORER = "https://mempool.space"  # mainnet only: on another chain its links would lead to the wrong transactions
DUST_MAX = 10_000  # satoshi; a higher --dust would leave real coins out of the statement, so it needs --dust-force
DEFAULT_THEME = "journal"


def _opt(args, name: str):
    """A node option given after the subcommand wins over the same option given before it."""
    return getattr(args, f"{name}_sub", None) or getattr(args, name, None)


def _wallet_name(args, config: Config) -> str | None:
    """The wallet as named by the owner: ``-w``, else the wallet directory the command runs in, else ``wallet`` in bip322.toml; None when none says."""
    given = _opt(args, "wallet")
    if given:
        return str(given)
    here = wallet_of_cwd()
    if here is not None:
        return here
    named = config.get("wallet")
    return str(named) if named is not None else None


def _cli(args, config: Config, wallet: str | None) -> BitcoinCli:
    """The node as the options and bip322.toml describe it, on the wallet's node wallet when one is named."""
    cli = BitcoinCli(_opt(args, "cli") or config.get("cli", wallet) or "bitcoin-cli")
    if wallet is not None:
        cli.argv.append(f"-rpcwallet={config.node_wallet(wallet)}")
    return cli


def _setting(args, config: Config, name: str, key: str, wallet: str | None, default=None):
    """An option's value: the command line, else bip322.toml (the wallet's table, then the top level), else ``default``."""
    given = getattr(args, name, None)
    return given if given is not None else config.get(key, wallet, default)


def _add_node_args(p: argparse.ArgumentParser) -> None:
    """The global options again, so that they may follow the command as well as precede it."""
    p.add_argument("--cli", dest="cli_sub", metavar="CMD", help="how to reach the node (may also be given before the command)")
    p.add_argument("--wallet", "-w", dest="wallet_sub", metavar="NAME", help="the wallet (may also be given before the command)")
    p.add_argument("--dir", dest="dir_sub", metavar="DIR", help="the data directory (may also be given before the command)")


def _progress(line: str) -> None:
    print(line, file=sys.stderr)


def _history(args, cli: BitcoinCli) -> History:
    if getattr(args, "history", None):
        return History.load(Path(args.history))
    return fetch_history(cli, progress=_progress)


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


def _proofs_roots(args, wallet: str | None) -> list[Path]:
    """Where the proofs come from: the --snapshots paths, each holding a snapshot, or the wallet's own snapshots directory.

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
        roots = [snapshots_dir(wallet)] if wallet is not None and snapshots_dir(wallet).is_dir() else []
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


def _file_stem(wallet: str, period_label: str) -> str:
    """``<wallet>-<label>``: the name the statement's files carry wherever they end up."""
    return f"{wallet}-{period_label}".replace(" ", "_").replace("/", "_")


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
    history = fetch_history(_cli(args, config, _wallet_name(args, config)), progress=_progress)
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


def _dust(args, config: Config, wallet: str | None) -> int:
    dust = _setting(args, config, "dust", "dust", wallet, 0)
    if not isinstance(dust, int) or isinstance(dust, bool):
        raise CLIError(f"dust must be a number of satoshi, not {dust!r}")
    if dust < 0:
        raise CLIError("--dust: a threshold cannot be negative")
    if dust > DUST_MAX and not args.dust_force:
        raise CLIError(f"--dust {dust}: more than {DUST_MAX} satoshi is not dust (--dust-force to leave such outputs out all the same)")
    return dust


def _report_history(args, cli: BitcoinCli, config: Config, wallet: str | None, period_label: str) -> tuple[History, str, Path]:
    """The history the statement is made from, checked against the node; the wallet's name; and the directory the statement goes to."""
    if not args.output and not args.history:
        # the default directory is named after the wallet: one call to learn the name before the long read of its history
        with contextlib.suppress(RpcError, KeyError, TypeError, ConfigError):  # no wallet: fetch_history says so better
            early = wallet or config.wallet_of_node(str(cli.call("getwalletinfo")["walletname"]))
            _check_directory(statements_dir(early) / period_label, args.force)
    history = _history(args, cli)
    if wallet is None and history.wallet is not None:
        wallet = config.wallet_of_node(str(history.wallet))
    if wallet is None:
        raise CLIError("no wallet name for the statement: -w NAME (or wallet = NAME in bip322.toml)")
    directory = Path(args.output) if args.output else statements_dir(wallet) / period_label
    _check_directory(directory, args.force)
    _check_history(cli, history, args.history or "the history")
    if history.wallet and not any(a.startswith("-rpcwallet=") for a in cli.argv):
        cli.argv.append(f"-rpcwallet={history.wallet}")  # a cached history knows its wallet; the node cross-check needs it
    return history, wallet, directory


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
    # what can be refused without the node is refused first: nothing is read, verified or written for a command that cannot finish
    period_label = _period_label(args)
    config = Config.load()
    wallet = _wallet_name(args, config)
    dust = _dust(args, config, wallet)
    rates = _rates(args)
    theme = str(_setting(args, config, "theme", "theme", wallet, DEFAULT_THEME))
    if theme not in THEMES:
        raise CLIError(f"theme {theme!r} is not one of {', '.join(THEMES)}")
    pdf = not args.html and _pdf_possible()
    roots = _proofs_roots(args, wallet) if args.snapshots else None  # a given path is checked before any work
    if args.output:
        _check_directory(Path(args.output), args.force)

    cli = _cli(args, config, wallet)
    history, wallet, directory = _report_history(args, cli, config, wallet, period_label)
    if roots is None:
        roots = _proofs_roots(args, wallet)
    period = _period(args, cli, history, period_label)
    report = build_report(
        history,
        period,
        label=wallet,
        holder=_setting(args, config, "holder", "holder", wallet),
        proofs_roots=roots,
        cli=cli,
        rates=rates,
        engines=args.engines.split(",") if args.engines else None,
        progress=_progress,
        dust_sat=dust,
        addresses=config.addresses,
    )
    stem = _file_stem(wallet, period_label)
    report["name"] = stem
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{stem}.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    write_csv(report, directory / f"{stem}.csv")
    if not args.no_proofs and roots:
        _copy_proofs(report, roots, directory)
    print(format_summary(report), file=sys.stderr)
    explorer = _setting(args, config, "explorer", "explorer", wallet)
    if explorer is None:
        explorer = EXPLORER if history.chain == "main" else None
    heading = _setting(args, config, "heading", "heading", wallet)
    if pdf:  # last: should the rendering fail, everything else is in place
        write_pdf(render_html(report, explorer=explorer, theme=theme, heading=heading, fonts_url=PACKAGE_FONTS), directory / f"{stem}.pdf")
    else:
        if not args.html:
            print(f"no PDF (WeasyPrint is not installed or lacks Pango): the statement is {stem}.html", file=sys.stderr)
        (directory / f"{stem}.html").write_text(render_html(report, explorer=explorer, theme=theme, heading=heading))
    print(str(directory))
    return 0 if report["ok"] else 1


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bip322-report",
        description=(
            "The statement of a period: holdings and movements in which every coin is backed by a verified BIP-322 proof of "
            f"control, from the owner's node wallet ({SPEC}). With no command the statement is made: "
            "bip322-report [--cli CMD] [-w NAME] (--year Y | --from WHEN --to WHEN | --from-height H --to-height H) [options]. "
            "It goes to <wallet>/statements/<label>/ in the data directory and its proofs come from <wallet>/snapshots/ there: "
            "the directory bip322 init made at or above the current one (--dir or BIP322_DIR name another), whose bip322.toml "
            "holds the defaults."
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
        help="the wallet: its directory NAME in the data directory, and the node wallet of that name (or the one its [wallets.NAME] "
        "table names as node). Default: the wallet directory the command runs in, else wallet in bip322.toml, else the node's one "
        "loaded wallet. May also follow the command.",
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
        help="the node wallet's transaction history as JSON (a cache for balance and report)",
        description=(
            "Read every transaction of the node wallet (listtransactions, gettransaction, getaddressinfo) and resolve the wallet's side of each: "
            "outputs to the wallet, the wallet's outputs spent, outputs to outside, the exact fee. No index is needed. "
            "balance and report take the result with --history so they can run without the wallet, or repeatably on the same data."
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
        help="the statement for a period: <wallet>-<label>.pdf (or .html), .json, .csv and the proofs files it cites, "
        "under the wallet's statements/<label>/; the default command",
        description=(
            "Opening and closing balances, every movement between them, a reconciliation, and for each closing output the BIP-322 proof of "
            "control, from the wallet's snapshots, that lists it, re-verified against the node now. The period is a calendar year, two instants, or two "
            "heights; opening figures are as of the start block, closing figures as of the end block. "
            "The files go to <wallet>/statements/<label>/ in the data directory, named <wallet>-<label>.pdf (.html where WeasyPrint is not "
            "installed, or with --html), .json, .csv, and a copy of each proofs file the statement cites, under its own name. "
            "Exit status: 0 when the result is OK, 1 when it is ATTENTION, 2 on an error."
        ),
    )
    _add_node_args(p)
    p.add_argument("--year", "-y", type=int, metavar="YEAR", help="the calendar year, UTC")
    p.add_argument("--from", dest="from_when", metavar="WHEN", help="start instant (YYYY-MM-DD or ISO 8601, UTC); with --to")
    p.add_argument("--to", dest="to_when", metavar="WHEN", help="end instant, exclusive; in the future means up to the tip")
    p.add_argument("--from-height", type=int, metavar="H", help="opening block height; with --to-height")
    p.add_argument("--to-height", type=int, metavar="H", help="closing block height")
    p.add_argument(
        "--snapshots",
        metavar="DIR",
        action="append",
        help="take the proofs from here instead of the wallet's snapshots/: a directory of snapshots, one snapshot, or one proofs file; may repeat",
    )
    p.add_argument(
        "--holder", metavar="NAME", help="the holder of the wallet, as named in the statement's header (default: holder in bip322.toml)"
    )
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
    p.add_argument("--history", metavar="FILE", help="use this history.json instead of reading the node wallet")
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
    p.add_argument("--output", "-o", metavar="DIR", help="write the statement here instead of under the wallet's statements/")
    p.add_argument("--force", action="store_true", help="write into a non-empty directory")
    p.set_defaults(
        func=cmd_report,
        examples=[
            "--year 2026",
            "-w treasury --year 2026 --dust 1000",
            "-w treasury --from 2026-01-01 --to 2026-07-01 --rates sek.csv --currency SEK",
            "--history history.json --from-height 900000 --to-height 912345 --snapshots DIR -o q3",
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
    ``-h``/``--help``/``--version``, is left alone; anything else (an option
    of the statement, or nothing at all) gets ``report`` put before it.  No
    argument at all lists the commands.
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
