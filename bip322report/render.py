"""report.html, transactions.csv and, when WeasyPrint is installed, report.pdf."""

from __future__ import annotations

import csv
import re
import shutil
from pathlib import Path
from typing import TypedDict

from bip322audit.rpc import btc
from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape
from markupsafe import Markup, escape

SOLARIZED = {
    "base03": "#002b36",
    "base02": "#073642",
    "base01": "#586e75",
    "base00": "#657b83",
    "base0": "#839496",
    "base1": "#93a1a1",
    "base2": "#eee8d5",
    "base3": "#fdf6e3",
    "yellow": "#b58900",
    "orange": "#cb4b16",
    "red": "#dc322f",
    "magenta": "#d33682",
    "violet": "#6c71c4",
    "blue": "#268bd2",
    "cyan": "#2aa198",
    "green": "#859900",
}  # Ethan Schoonover's palette; the accents keep their weight on either background
_S = SOLARIZED

JOURNAL = {  # a newspaper's palette: one red, base colours that carry text, greys, warm canvases
    "red": "#E3120B",
    "red_dark": "#CC100A",
    "red_tint": "#FEE7E7",
    "navy": "#141F52",
    "blue": "#2E45B8",
    "teal": "#169C7F",
    "crimson": "#9C1633",
    "rose": "#C91D42",
    "green": "#4C9C16",
    "orange": "#F97A1F",
    "yellow": "#F9C31F",
    "grey5": "#0D0D0D",
    "grey10": "#1A1A1A",
    "grey20": "#333333",
    "grey35": "#595959",
    "grey70": "#B3B3B3",
    "grey85": "#D9D9D9",
    "grey95": "#F2F2F2",
    "gold": "#7D6210",  # the yellow at half strength: made fit for text
    # the same hues darkened to 4.5:1 on canvas95, the floor for text of 7.5 to 9 pt
    "red_text": "#DF120B",
    "orange_text": "#BA5005",
    "teal_text": "#127E67",
    "green_text": "#3D7E12",
    "canvas85": "#E1DFD0",
    "canvas90": "#EBE9E0",
    "canvas95": "#F5F4EF",
}
_J = JOURNAL

#: Bundled typefaces (SIL OFL; see fonts/LICENSE.md): Adobe's Source Serif 4 and Source Sans 3, the open
#: counterparts of a newspaper's serif for reading and sans for labels and tables, and JetBrains Mono for hashes.
FONTS_DIR = Path(__file__).parent / "fonts"
FONT_FACES = [  # family, file, weight, style
    ("Source Serif 4", "SourceSerif4-Regular.woff2", 400, "normal"),
    ("Source Serif 4", "SourceSerif4-It.woff2", 400, "italic"),
    ("Source Serif 4", "SourceSerif4-Semibold.woff2", 600, "normal"),
    ("Source Serif 4", "SourceSerif4-Bold.woff2", 700, "normal"),
    ("Source Sans 3", "SourceSans3-Regular.woff2", 400, "normal"),
    ("Source Sans 3", "SourceSans3-It.woff2", 400, "italic"),
    ("Source Sans 3", "SourceSans3-Semibold.woff2", 600, "normal"),
    ("Source Sans 3", "SourceSans3-Bold.woff2", 700, "normal"),
    ("JetBrains Mono", "JetBrainsMono-Regular.woff2", 400, "normal"),
    ("JetBrains Mono", "JetBrainsMono-Bold.woff2", 700, "normal"),
]
SANS = '"Source Sans 3", "Helvetica Neue", Arial, "DejaVu Sans", sans-serif'
SERIF = '"Source Serif 4", Georgia, "Times New Roman", "DejaVu Serif", serif'
MONO = '"JetBrains Mono", Menlo, Consolas, "DejaVu Sans Mono", monospace'

#: How section headings are set: coloured text over a coloured rule; a red tab over a grey rule;
#: black text with the section number in red; black text over a thin red rule.
HEADINGS = ("underline", "tab", "number", "redrule")

#: The values a reader matches across the statement, each with one colour; hash and txid appear in section 4 only.
VALUE_ROLES = ("script", "block", "proof", "utxo", "amount", "hash", "txid")


class Theme(TypedDict):
    """What a theme defines: every key templates/report.html reads from ``t``, and no other.

    The page's colours, and the colours of the values a reader matches
    across a page (scriptPubKey, block, proof, UTXO, amount): one thing, one
    colour.  Solarized in the Solarized themes; the journal palette's base
    colours in the journal theme.
    """

    bg: str  # the page
    ink: str  # running text
    strong: str  # headlines, table heads, totals
    muted: str  # captions, notes, page margins
    rule: str  # the line under a table row
    rule_strong: str  # the line under a table head and around a total; the boxes of the figure
    head: str  # a table head's background
    zebra: str  # every other table row's background
    accent: str  # section headings set as "underline"
    term_ink: str  # commands and their output
    ok: str  # a check that passed
    bad: str  # a check that failed, the ATTENTION pill
    on_pill: str  # text on the pill
    script: str  # the values, by VALUE_ROLES
    block: str
    proof: str
    utxo: str
    amount: str
    hash: str
    txid: str
    good: str  # VALID in a command's output
    bad_text: str  # INVALID, INCONCLUSIVE in a command's output
    tab: str | None  # the red of the headings and the eyebrow; None for the theme's "bad" and a muted eyebrow
    heading: str  # one of HEADINGS
    font_body: str
    font_heading: str
    font_label: str  # tables, badges, small print, the figure
    font_mono: str
    oldstyle: bool  # old-style figures in running text


_SOLARIZED_THEME = {  # what the three Solarized themes share: the values, the marks, sans throughout
    "script": _S["blue"],
    "block": _S["magenta"],
    "proof": _S["violet"],
    "utxo": _S["orange"],
    "amount": _S["green"],
    "hash": _S["cyan"],
    "txid": _S["yellow"],
    "ok": _S["green"],
    "bad": _S["red"],
    "good": _S["green"],
    "bad_text": _S["red"],
    "tab": None,
    "heading": "underline",
    "font_body": SANS,
    "font_heading": SANS,
    "font_label": SANS,
    "font_mono": MONO,
    "oldstyle": False,
}
THEMES: dict[str, Theme] = {
    "paper": {  # a white page; only the commands and the values are Solarized
        **_SOLARIZED_THEME,
        "bg": "#ffffff",
        "ink": "#1b1b1b",
        "strong": "#111111",
        "muted": "#6b6b6b",
        "rule": "#d6d6d6",
        "rule_strong": "#333333",
        "head": "#f2f3f5",
        "zebra": "#fafafa",
        "accent": "#1f3a5f",
        "term_ink": _S["base00"],
        "ok": "#0f7a3a",
        "bad": "#b3261e",
        "on_pill": "#ffffff",
    },
    "light": {  # Solarized light over the whole page
        **_SOLARIZED_THEME,
        "bg": _S["base3"],
        "ink": _S["base01"],
        "strong": _S["base02"],
        "muted": _S["base00"],
        "rule": _S["base2"],
        "rule_strong": _S["base01"],
        "head": _S["base2"],
        "zebra": "#f7f0dc",
        "accent": _S["base01"],
        "term_ink": _S["base00"],
        "on_pill": _S["base3"],
    },
    "dark": {  # Solarized dark
        **_SOLARIZED_THEME,
        "bg": _S["base03"],
        "ink": _S["base0"],
        "strong": _S["base1"],
        "muted": _S["base00"],
        "rule": _S["base02"],
        "rule_strong": _S["base1"],
        "head": _S["base02"],
        "zebra": "#04303c",
        "accent": _S["base1"],
        "term_ink": _S["base0"],
        "on_pill": _S["base03"],
    },
    "journal": {  # a warm canvas, greys, the section number in red, tables ruled not striped
        "bg": _J["canvas95"],
        "ink": _J["grey10"],
        "strong": _J["grey5"],
        "muted": _J["grey35"],
        "rule": _J["grey85"],
        "rule_strong": _J["grey20"],
        "head": _J["canvas95"],
        "zebra": _J["canvas95"],
        "accent": _J["grey5"],
        "term_ink": _J["grey20"],
        "ok": _J["green"],
        "bad": _J["red_dark"],
        "on_pill": "#ffffff",
        # the values: red for the block, base colours for the rest, each dark enough for small text on the canvas
        "script": _J["blue"],
        "block": _J["red_text"],
        "proof": _J["orange_text"],
        "utxo": _J["teal_text"],
        "amount": _J["green_text"],
        "hash": _J["crimson"],
        "txid": _J["gold"],
        "good": _J["green"],
        "bad_text": _J["rose"],
        "tab": _J["red"],
        "heading": "number",
        # serif for reading and headlines, sans for labels, tables and metadata
        "font_body": SERIF,
        "font_heading": SERIF,
        "font_label": SANS,
        "font_mono": MONO,
        "oldstyle": True,
    },
}


def _env() -> Environment:
    env = Environment(
        loader=PackageLoader("bip322report", "templates"),
        autoescape=select_autoescape(["html"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["btc"] = btc
    env.filters["acct"] = lambda text: f"({text[1:]})" if str(text).startswith("-") else str(text)  # accounting negatives
    env.filters["breakable"] = _breakable
    env.filters["hl"] = _highlight
    env.filters["utc"] = lambda iso: (str(iso).replace("T", " ").replace("Z", " UTC")) if iso else ""
    return env


def _breakable(text: str, every: int = 12) -> Markup:
    """A long token with a break opportunity every few characters, so it wraps evenly rather than at slashes.

    ``<wbr>`` adds no character: copying the text yields the token unchanged.
    """
    text = str(text)
    return Markup("<wbr>".join(str(escape(text[i : i + every])) for i in range(0, len(text), every)))


def _highlight(text: str, tokens: list) -> Markup:
    """The values that recur on a UTXO page, coloured so the reader matches them by eye: one thing, one colour, wherever it appears.

    ``tokens`` is a list of ``[role, value]`` or ``[role, value, prefix]``:
    every occurrence of ``value`` (after ``prefix``, which stays uncoloured)
    gets ``<span class="hl-ROLE">``.  A printed command may split a value
    with a backslash-newline; the split stays inside the span.  Earlier
    tokens win where they overlap.  The text is escaped; nothing is added
    that would change what a reader copies.
    """
    text = str(text)
    taken = [False] * len(text)
    spans: list[tuple[int, int, str]] = []
    for role, value, *rest in tokens:
        if not value:
            continue
        prefix = re.escape(rest[0]) if rest else ""
        pattern = prefix + "(" + r"(?:\\\n)?".join(re.escape(c) for c in str(value)) + ")"
        for m in re.finditer(pattern, text):
            start, end = m.span(1)
            if not any(taken[start:end]):
                spans.append((start, end, str(role)))
                taken[start:end] = [True] * (end - start)
    spans.sort()
    parts: list[Markup] = []
    pos = 0
    for start, end, role in spans:
        parts.append(escape(text[pos:start]))
        parts.append(Markup('<span class="hl-{}">{}</span>').format(role, text[start:end]))
        pos = end
    parts.append(escape(text[pos:]))
    return Markup("").join(parts)


FIGURE_CHARS = 27  # what the figure's message box holds in a line of its monospace size: "Proof of control 2026-09-18"


def _figure_message(report: dict) -> dict:
    """The message the section 4 figure quotes: the first proof's own title and block; placeholders when no UTXO has a proof."""
    proof = next((c["proof"] for c in report["closing"]["coins"] if c.get("proof")), None)
    if not proof:
        return {"title": "<title>", "block": "<height> <hash>"}
    title = str(proof["message"]).split("\n")[0]
    if len(title) > FIGURE_CHARS:
        title = title[: FIGURE_CHARS - 1] + "…"
    stamp = proof["stamp"]
    return {"title": title, "block": f"{stamp['height']} {str(stamp['hash'])[:6]}…"}


def render_html(
    report: dict,
    *,
    explorer: str | None = None,
    theme: str = "journal",
    heading: str | None = None,
    fonts_url: str = "fonts/",
) -> str:
    """The statement as HTML.  Fonts are referenced at ``fonts_url`` (see ``copy_fonts``); ``heading`` overrides the theme's heading style."""
    if theme not in THEMES:
        raise ValueError(f"unknown theme {theme!r}; one of {', '.join(THEMES)}")
    if heading is not None and heading not in HEADINGS:
        raise ValueError(f"unknown heading style {heading!r}; one of {', '.join(HEADINGS)}")
    t = {**THEMES[theme], "heading": heading or THEMES[theme]["heading"]}
    faces = [{"family": f, "url": fonts_url + file, "weight": w, "style": s} for f, file, w, s in FONT_FACES]
    fig = _figure_message(report)
    return _env().get_template("report.html").render(r=report, explorer=(explorer or "").rstrip("/") or None, t=t, faces=faces, fig=fig)


def copy_fonts(directory: Path) -> Path:
    """Put the bundled fonts next to a report, in ``fonts/``, where its HTML and PDF find them."""
    target = directory / "fonts"
    target.mkdir(parents=True, exist_ok=True)
    for _family, file, _w, _s in FONT_FACES:
        shutil.copyfile(FONTS_DIR / file, target / file)
    shutil.copyfile(FONTS_DIR / "LICENSE.md", target / "LICENSE.md")
    return target


def write_csv(report: dict, path: Path) -> None:
    fiat = report.get("fiat")
    fields = (
        ["time_utc", "height", "txid", "kind", "amount_btc", "fee_btc", "net_btc"]
        + (["rate", "amount_fiat", "fee_fiat", "net_fiat"] if fiat else [])
        + ["ours_in", "ours_out", "others_out"]
    )
    with Path(path).open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for tx in report["transactions"]:
            row = {k: tx.get(k) for k in ("time_utc", "height", "txid", "kind", "amount_btc", "fee_btc", "net_btc")}
            if fiat:
                row.update(
                    {
                        "rate": tx["fiat"]["rate"],
                        "amount_fiat": tx["fiat"]["amount"],
                        "fee_fiat": tx["fiat"]["fee"],
                        "net_fiat": tx["fiat"]["net"],
                    }
                )
            if tx["kind"] == "mixed":  # others funded it too: an empty fee cell would read as none
                row["fee_btc"] = "unknown"
                if fiat:
                    row["fee_fiat"] = "unknown"
            for key in ("ours_in", "ours_out", "others_out"):
                row[key] = " ".join(f"{c['address'] or 'no-address'}={c['amount_btc']}" for c in tx[key])
            w.writerow(row)


def write_pdf(html: str, path: Path) -> None:
    """Render the HTML to ``path``; relative URLs in it (the fonts) resolve next to ``path``."""
    try:
        from weasyprint import HTML
    except Exception as exc:  # ImportError, or an OSError when its system libraries (Pango) are missing
        raise RuntimeError(
            f"PDF output needs WeasyPrint and its system libraries: pip install 'bip322-report[pdf]' (see the README) [{exc}]"
        ) from exc
    HTML(string=html, base_url=str(path.parent) + "/").write_pdf(str(path))
