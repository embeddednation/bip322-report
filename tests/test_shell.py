"""The printed verifymessage command reassembles to its argv in bash, line by line as the statement shows it."""

import os
import random
import shutil
import subprocess
import tempfile

import pytest

from bip322report.report import WIDTH, _verify_by_script, shell_command, shell_words

SPK = "0020" + "ab" * 32
SIG = "smp" + "A" * 300 + "+/=="
HASH = "0000000000000000000123456789abcdef0123456789abcdef0123456789abcd"
MESSAGE = f'Proof of control 2026-09-18 "quoted" $HOME `x`\nblock: 967247 {HASH} 2026-09-18T10:37:00Z'
FORMAT = "%s\x1f"  # printf's format: every argument, each followed by a separator no message holds
END = "\x1e"  # printed after each command when several are run in one shell

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")


def _pasted(texts: list[str], interactive: bool) -> list[list[str]]:
    """What bash makes of printed ``printf '%s\\x1f' ...`` commands read line by line, as when pasted: the arguments of each.

    An interactive bash expands history (``!``, and ``^`` at the start of a
    line) before it looks at quotes; a script or ``bash -c`` does not.  The
    shell gets its own session, so it has no terminal to take over, and no
    startup or history file, so the user's are neither read nor written.
    """
    script = "".join(f"{text}\nprintf '{END}'\n" for text in texts)
    argv = ["bash", "--norc", "--noprofile", *(["--noediting", "-i"] if interactive else [])]
    env = {**os.environ, "HISTFILE": "/dev/null"}
    with tempfile.TemporaryDirectory() as scratch:  # a mis-quoted command may redirect into a file: never into the repository
        done = subprocess.run(argv, input=script, capture_output=True, text=True, env=env, start_new_session=True, timeout=120, cwd=scratch)
    return [record.split("\x1f")[:-1] for record in done.stdout.split(END)[:-1]]


def _lines_fit(text: str) -> bool:
    return all(len(line) <= WIDTH + 2 for line in text.splitlines())  # WIDTH plus " \\"


def test_bash_reassembles_the_printed_command():
    argv = ["printf", FORMAT, SPK, SIG, MESSAGE]
    text = shell_command(argv)
    assert _lines_fit(text)
    assert any(line.startswith(f"block: 967247 {HASH}") for line in text.splitlines())  # the block line keeps its hash whole
    out = subprocess.run(["bash", "-c", text], capture_output=True, text=True, check=True, cwd=tempfile.gettempdir()).stdout
    assert out.split("\x1f")[:-1] == argv[2:]


@pytest.mark.parametrize(
    "message",
    [
        "wow !important",  # bash -i: "!important: event not found"
        "!",
        "!!\n!$ !-1 !?x? !#",
        f"it's done!\nisn't it!?\nblock: 967247 {HASH} 2026-09-18T10:37:00Z!",
        "Proof of control\n^control^custody^",  # a line starting with ^ is a quick substitution: "!!: event not found"
        "x" * WIDTH + "^y^z",  # the same after a continuation
        "a" * (WIDTH - 2) + "!" * 40,
    ],
)
def test_an_interactive_bash_reassembles_a_message_with_history_characters(message):
    argv = ["printf", FORMAT, SPK, "it's !here", "^first^", SIG, message]
    text = shell_command(argv)
    assert _lines_fit(text)
    assert not any(line.startswith("^") for line in text.splitlines())
    assert _pasted([text], interactive=True) == [argv[2:]]
    assert _pasted([text], interactive=False) == [argv[2:]]


@pytest.mark.parametrize("backslashes", [1, 2, 41, 42, 43, 83, 84, 85, 200])
def test_a_run_of_backslashes_is_never_cut_inside_an_escape(backslashes):
    for message in ("\\" * backslashes, "ab" + "\\" * backslashes + '"c', "a b " * 10 + "\\" * backslashes):
        argv = ["printf", FORMAT, SPK, message]
        text = shell_command(argv)
        assert _lines_fit(text)
        assert _pasted([text], interactive=False) == [argv[2:]]


@pytest.mark.parametrize(
    "word",
    [
        "two words " * 12,  # needs quotes and is longer than a line: a backslash-newline inside single quotes would stay in the word
        "it's " * 30,
        "'" * 100,
        "$HOME `id` \\ " * 9 + "!x",
        "a" * (WIDTH - 3) + " b",
        "line one\n^line^two\n!three " * 5,
    ],
)
def test_a_long_word_that_needs_quoting_is_split_between_quotes(word):
    for printed, argv in (
        (shell_words, ["printf", FORMAT, "a", word, "b c", word]),
        (shell_command, ["printf", FORMAT, "a", word, SIG, "the message"]),
    ):
        text = printed(argv)
        assert _lines_fit(text)
        assert not any(line.startswith("^") for line in text.splitlines())
        assert _pasted([text], interactive=False) == [argv[2:]]
        assert _pasted([text], interactive=True) == [argv[2:]]


def test_plain_long_words_are_cut_at_the_line_width():
    assert shell_words(["bip322", "x", SIG]).splitlines() == [
        "bip322 x \\",
        *(SIG[i : i + WIDTH] + "\\" for i in (0, WIDTH, 2 * WIDTH)),
        SIG[3 * WIDTH :],
    ]
    assert shell_command(["bip322", SIG, "m"]).splitlines()[:2] == ["bip322 \\", SIG[:WIDTH] + "\\"]


@pytest.mark.parametrize("interactive", [False, True])
def test_bash_reassembles_any_message_and_words(interactive):
    rng = random.Random(322)
    alphabet = (
        list("abcXYZ019 -_=+,.:/@%~#*?&|;<>()[]{}")
        + list("'\"\\$`!^\n\t") * 4
        + list("åéö€₿")
        + ["\\\\", "\\\n", "$(id)", "${x}", " !", "\n^", "\n-"]
    )
    cases = []
    for n in range(300):
        words = ["".join(rng.choices(alphabet, k=rng.choice((0, 1, 5, 30, 90, 200)))) for _ in range(rng.randrange(3))]
        message = "".join(rng.choices(alphabet, k=rng.choice((0, 1, 2, 10, 60, 84, 85, 200, 400))))
        cases.append(["printf", FORMAT, f"case{n}", *words, message])
    texts = [shell_command(argv) for argv in cases] + [shell_words(argv) for argv in cases]
    assert all(_lines_fit(text) for text in texts)
    assert _pasted(texts, interactive) == [argv[2:] for argv in cases] * 2


def test_a_message_that_starts_with_a_dash_is_not_taken_for_an_option():
    def command(message: str) -> list[str]:
        a = {"address": SPK, "script": {"script_pubkey": SPK}, "proof": {"message": message, "signature": SIG, "verified": False}}
        _verify_by_script(a, None)
        return _pasted([a["proof"]["command"].replace("bip322 verifymessage", f"printf '{FORMAT}'", 1)], interactive=True)[0]

    assert command("--json\nblock: 1 00 2026") == [SPK, SIG, "--", "--json\nblock: 1 00 2026"]
    assert command("-h") == [SPK, SIG, "--", "-h"]
    assert command(MESSAGE) == [SPK, SIG, MESSAGE]  # as before: the same argument order as Bitcoin Core's verifymessage
