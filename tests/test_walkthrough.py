"""The walkthrough writes only into a directory that is new or empty, and deletes nothing it was pointed at."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "examples" / "report_walkthrough.sh"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_walkthrough_refuses_what_exists_and_is_not_empty_and_leaves_it_alone(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "keep.txt").write_text("mine\n")
    file = tmp_path / "a-file"
    file.write_text("mine too\n")
    env = {**os.environ, "BITCOIN_CORE_DIR": str(tmp_path / "no-core")}  # the refusal comes before any binary is looked for
    for given in (work, file):
        done = subprocess.run(["bash", str(SCRIPT), str(given)], capture_output=True, text=True, env=env, cwd=tmp_path, timeout=120)
        assert done.returncode == 2 and done.stdout == ""
        assert done.stderr == f"error: {given} exists and is not an empty directory; give a new or empty one\n"
    assert [p.name for p in work.iterdir()] == ["keep.txt"] and (work / "keep.txt").read_text() == "mine\n"
    assert file.read_text() == "mine too\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a-file", "work"]  # nothing was created beside them either


def test_walkthrough_removes_only_its_own_temporary_datadir():
    removals = [line for line in SCRIPT.read_text().splitlines() if "rm -r" in line or "rm -f" in line]
    assert removals and all('rm -rf "$DATADIR"' in line and "$WORK" not in line and "$1" not in line for line in removals)
    assert "DATADIR=$(mktemp -d)" in SCRIPT.read_text()
