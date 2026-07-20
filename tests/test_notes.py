"""Persistent-notes vault validation (Obsidian CLI), with an injected fake runner."""
from types import SimpleNamespace

import pytest

from atom.notes import NotesBinding, VaultNotRegisteredError, _list_vaults, ensure_vault


def _runner(vaults):
    """A fake CLIRunner that answers `obsidian vaults verbose` with the given name->path map."""
    lines = "\n".join(f"{n}\t{p}" for n, p in vaults.items())

    def run(args):
        assert args[1:] == ["vaults", "verbose"]
        return 0, lines + "\n", ""

    return run


def _cfg(vault=None):
    return SimpleNamespace(provider="obsidian", vault=vault)


def test_ensure_vault_resolves_registered_vault():
    run = _runner({"kalshi": "/repos/kalshi/kb", "brain": "/repos/brain"})
    b = ensure_vault("kalshi", _cfg(), runner=run)
    assert b == NotesBinding(provider="obsidian", vault="kalshi", root_dir="/repos/kalshi/kb")
    assert b.as_prompt_ctx() == {
        "provider": "obsidian", "vault": "kalshi", "root_dir": "/repos/kalshi/kb"}


def test_vault_defaults_to_workflow_name():
    b = ensure_vault("my-wf", _cfg(), runner=_runner({"my-wf": "/repos/x"}))
    assert b.vault == "my-wf"


def test_explicit_vault_override_wins():
    b = ensure_vault("some-workflow", _cfg(vault="brain"), runner=_runner({"brain": "/repos/brain"}))
    assert b.vault == "brain" and b.root_dir == "/repos/brain"


def test_unregistered_vault_raises():
    with pytest.raises(VaultNotRegisteredError) as ei:
        ensure_vault("ghost", _cfg(), runner=_runner({"brain": "/repos/brain"}))
    assert "ghost" in str(ei.value) and "brain" in str(ei.value)


def test_rejects_non_obsidian_provider():
    with pytest.raises(NotImplementedError):
        ensure_vault("wf", SimpleNamespace(provider="logseq", vault=None), runner=_runner({}))


def test_list_vaults_parses_tsv():
    assert _list_vaults(_runner({"a": "/p/a", "b": "/p/b"}), "obsidian") == {"a": "/p/a", "b": "/p/b"}
