"""The rendered statement: themes, the section 4 figure, the colours of the values."""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from test_report import _bundle, _node

import bip322report
from bip322report.cli import build_parser
from bip322report.history import fetch_history
from bip322report.period import period_for_heights
from bip322report.render import MONO, SANS, THEMES, VALUE_ROLES, render_html
from bip322report.report import build_report


def _report(tmp_path, wallet, signers, template="Owner proof {date}", ledger=True) -> dict:
    """Three closing UTXOs at the tip, each with a proof from one bundle (or none, without a ledger)."""
    node = _node(wallet, tip=1000)
    if ledger:
        _bundle(tmp_path / "ledger" / "snapshot-1", node, wallet, signers, template)
    roots = [tmp_path / "ledger"] if ledger else []
    period = period_for_heights(node, 985, 1000, label="test")
    return build_report(fetch_history(node), period, label="Treasury", ledger_roots=roots, cli=node)


class _Figure(HTMLParser):
    """The section 4 figure as parsed: every font-family attribute inside it, and its text."""

    def __init__(self):
        super().__init__()
        self.depth = 0
        self.attrs: list[dict] = []
        self.families: list[str | None] = []
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "svg" and "fig" in (attrs.get("class") or ""):
            self.depth = 1
            self.attrs.append(attrs)
        elif self.depth:
            self.depth += tag == "svg"
        if self.depth and "font-family" in attrs:
            self.families.append(attrs["font-family"])

    def handle_endtag(self, tag):
        if self.depth and tag == "svg":
            self.depth -= 1

    def handle_data(self, data):
        if self.depth and data.strip():
            self.text.append(data.strip())


def _figure(html: str) -> _Figure:
    fig = _Figure()
    fig.feed(html)
    assert fig.attrs, "the statement has no figure"
    return fig


def test_the_default_theme_is_journal_and_no_publication_is_named():
    assert list(THEMES) == ["paper", "light", "dark", "journal"]
    assert build_parser().parse_args(["report", "--year", "2026"]).theme == "journal"
    assert all(build_parser().parse_args(["report", "--year", "2026", "--theme", name]).theme == name for name in THEMES)
    # the earlier name and palette words, spelled in halves so that this file passes its own check
    halves = "econ/omist mar/ber lon/don chi/cago hong/kong singa/pore shang/hai to/kyo los/angeles new/york"
    words = re.compile("|".join(h.replace("/", " ?") for h in halves.split()), re.I)
    root = Path(bip322report.__file__).parent.parent
    texts = [*(root / "bip322report").rglob("*.py"), *(root / "bip322report").rglob("*.html"), root / "bip322report" / "handbook.md"]
    texts += [root / "README.md", *(root / "docs").glob("*.md"), *(root / "tests").glob("*.py"), *(root / "examples").glob("*.sh")]
    for path in texts:
        if path.exists():  # an installed package has no README, docs or tests beside it
            assert not words.search(path.read_text()), path


@pytest.mark.parametrize("theme", list(THEMES))
def test_figure_is_set_in_the_bundled_fonts(tmp_path, wallet, signer_expressions, theme):
    html = render_html(_report(tmp_path, wallet, signer_expressions), theme=theme)
    assert 'font-family=""' not in html
    fig = _figure(html)
    assert fig.attrs[0]["font-family"] == THEMES[theme]["font_label"] == SANS  # the figure's own face: the labels' sans
    assert len(fig.families) > 20 and set(fig.families) == {SANS, MONO}  # no attribute cut short at a quote
    assert f"--font-mono: {MONO};" in html and f"font: 7.5pt {SANS};" in html  # in the style sheet the stacks stay as CSS


def test_figure_quotes_the_first_proofs_own_message_and_block(tmp_path, wallet, signer_expressions):
    report = _report(tmp_path, wallet, signer_expressions)
    proof = report["closing"]["coins"][0]["proof"]
    title, stamp = proof["message"].split("\n")[0], proof["stamp"]
    assert title.startswith("Owner proof ") and int(stamp["height"]) != report["period"]["end"]["height"]
    fig = _figure(render_html(report))
    assert title in fig.text and f"{stamp['height']} {stamp['hash'][:6]}…" in fig.text
    assert "Proof of control 2026" not in fig.text and not any(t.startswith(f"{report['period']['end']['height']} ") for t in fig.text)
    left, _top, width, _height = (float(v) for v in fig.attrs[0]["viewbox"].split())  # html.parser lowers attribute names
    assert left <= 0.5 and left + width >= 700.5  # the boxes span x=1..700: their strokes need half a unit on either side


def test_figure_cuts_a_long_title_to_its_box(tmp_path, wallet, signer_expressions):
    long = "Proof of control of the treasury's holdings {date}"
    report = _report(tmp_path, wallet, signer_expressions, template=long)
    title = report["closing"]["coins"][0]["proof"]["message"].split("\n")[0]
    fig = _figure(render_html(report))
    shown = next(t for t in fig.text if t.startswith("Proof of control of"))
    assert len(title) > 27 and len(shown) == 27 and shown.endswith("…") and title.startswith(shown[:-1])


def test_figure_without_any_proof_shows_placeholders(tmp_path, wallet, signer_expressions):
    report = _report(tmp_path, wallet, signer_expressions, ledger=False)
    assert report["closing"]["coins"] and not any(c["proof"] for c in report["closing"]["coins"])
    fig = _figure(render_html(report))
    assert "<title>" in fig.text and "<height> <hash>" in fig.text
    assert not any(t.startswith(str(report["period"]["end"]["height"])) for t in fig.text)


def _luminance(colour: str) -> float:
    channels = [int(colour.lstrip("#")[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    r, g, b = (c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: str, b: str) -> float:
    """WCAG 2 contrast ratio of two #rrggbb colours."""
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_value_colours_of_the_default_theme_carry_small_text():
    t = THEMES["journal"]
    assert _contrast("#000000", "#ffffff") == pytest.approx(21)
    for role in VALUE_ROLES:
        assert _contrast(t[role], t["bg"]) >= 4.5, (role, t[role])
    assert len({t[role] for role in VALUE_ROLES}) == len(VALUE_ROLES)  # one value, one colour
