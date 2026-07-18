"""Persistent-notes vault lifecycle (Logseq), with an injected fake CLI runner."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from atom.notes import NotesBinding, _slug, ensure_vault, notes_root


def test_slug():
    assert _slug("Notes Smoke!") == "notes-smoke"
    assert _slug("  ") == "workflow"


def test_notes_root(atom_home):
    assert notes_root(str(atom_home), "Notes Smoke") == atom_home / "notes" / "notes-smoke"


def test_ensure_vault_creates_when_absent(atom_home):
    calls = []

    def fake_runner(args):
        calls.append(args)
        if args[1:3] == ["graph", "list"]:
            return 0, '{"status":"ok","data":{"graphs":[],"graph-items":[]}}', ""
        return 0, 'Created graph "notes-smoke"', ""

    cfg = SimpleNamespace(provider="logseq", graph=None)
    binding = ensure_vault(str(atom_home), "notes-smoke", cfg, runner=fake_runner)
    assert isinstance(binding, NotesBinding)
    assert binding.graph == "notes-smoke"
    assert binding.root_dir == str(atom_home / "notes" / "notes-smoke")
    assert binding.as_prompt_ctx() == {
        "provider": "logseq", "root_dir": binding.root_dir, "graph": "notes-smoke"}
    assert any(a[1:3] == ["graph", "create"] for a in calls)


def test_ensure_vault_reuses_when_present(atom_home):
    calls = []

    def fake_runner(args):
        calls.append(args)
        if args[1:3] == ["graph", "list"]:
            return 0, '{"status":"ok","data":{"graphs":["notes-smoke"]}}', ""
        return 0, "", ""

    cfg = SimpleNamespace(provider="logseq", graph=None)
    ensure_vault(str(atom_home), "notes-smoke", cfg, runner=fake_runner)
    assert not any(a[1:3] == ["graph", "create"] for a in calls)  # reused, no create


def test_ensure_vault_custom_graph_name(atom_home):
    def fake_runner(args):
        if args[1:3] == ["graph", "list"]:
            return 0, '{"data":{"graphs":[]}}', ""
        return 0, "", ""

    cfg = SimpleNamespace(provider="logseq", graph="custom")
    assert ensure_vault(str(atom_home), "wf", cfg, runner=fake_runner).graph == "custom"


def test_ensure_vault_rejects_unknown_provider(atom_home):
    with pytest.raises(NotImplementedError):
        ensure_vault(str(atom_home), "wf", SimpleNamespace(provider="notion", graph=None))


def test_clear_vault_removes_existing(atom_home):
    from atom.notes import clear_vault
    root = notes_root(str(atom_home), "wf-clear")
    (root / "pages").mkdir(parents=True)
    (root / "pages" / "a.md").write_text("note")
    assert clear_vault(str(atom_home), "wf-clear") is True
    assert not root.exists()


def test_clear_vault_absent_is_noop(atom_home):
    from atom.notes import clear_vault
    assert clear_vault(str(atom_home), "never-existed") is False


def test_clear_vault_refuses_outside_notes_dir(atom_home, monkeypatch):
    import atom.notes as notes_mod
    from pathlib import Path
    # A pathological notes_root that points outside $ATOM_HOME/notes/ must be refused.
    monkeypatch.setattr(notes_mod, "notes_root", lambda home, name: Path(home) / "workflows")
    with pytest.raises(ValueError):
        notes_mod.clear_vault(str(atom_home), "x")
