"""Obsidian curate-kb file-walk scripts: island detection + recent-change scoping."""
import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skill_library" / "curate-knowledge-base" / "scripts"


def _load(name):
    # The scripts import `_vault_ids` as a sibling; put the scripts dir on sys.path first.
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mkvault(tmp_path):
    v = tmp_path / "vault"
    v.mkdir()
    (v / "A.md").write_text("Links to [[B]] and [[C]].")
    (v / "B.md").write_text("Back to [[A]].")
    (v / "C.md").write_text("Mentions [[A]].")
    (v / "D.md").write_text("Talks to [[E]].")            # island D<->E
    (v / "E.md").write_text("Talks to [[D]].")
    (v / "F.md").write_text("Isolated note, no links.")   # singleton
    return v


def test_islands_and_isolated(tmp_path):
    r = _load("find-disconnected-notes").analyze_vault(_mkvault(tmp_path))
    assert r["note_count"] == 6
    assert r["main_component"]["size"] == 3
    assert sorted(r["main_component"]["members"]) == ["A", "B", "C"]
    assert [sorted(i["members"]) for i in r["islands"]] == [["D", "E"]]
    assert r["isolated"] == ["F"]


def test_recent_mtime(tmp_path):
    import os
    import time

    mod = _load("find-recently-modified-notes")
    v = _mkvault(tmp_path)
    old = time.time() - 10_000
    for p in v.glob("*.md"):
        os.utime(p, (old, old))
    os.utime(v / "A.md", None)  # touch A to "now"
    assert mod.list_recent(v, since=str(time.time() - 100))["changed"] == ["A"]
