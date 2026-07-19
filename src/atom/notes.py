"""Persistent workflow notes: provision + reuse a per-workflow Logseq vault (graph).

The vault lives OUTSIDE any per-run workspace, keyed by workflow name, so it is shared across
every run of that workflow. ``ensure_vault`` is idempotent (list-then-create-if-absent).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from atom.sandbox.paths import atom_home

CLIRunner = Callable[[list[str]], "tuple[int, str, str]"]


@dataclass
class NotesBinding:
    provider: str
    root_dir: str
    graph: str

    def as_prompt_ctx(self) -> dict:
        return {"provider": self.provider, "root_dir": self.root_dir, "graph": self.graph}


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s or "workflow"


# Namespace for atom-managed graphs when co-located in the Logseq desktop app's graph home.
# Load-bearing: `clear_vault` only ever `graph remove`s a name carrying this prefix, so it can
# never touch a user's personal graph that shares the home. (If the Task 1 spike found "." renders
# badly in the switcher, use "atom-" and update the tests accordingly.)
ATOM_GRAPH_PREFIX = "atom."


class VaultBusyError(RuntimeError):
    """The vault's graph is open in the Logseq desktop app, so a lifecycle op (remove) is refused."""


def resolve_logseq_root(override: str | None = None) -> Path:
    """Resolve the Logseq desktop app's graph home: the ``--root-dir`` whose ``graphs/`` the app
    scans for its switcher. Explicit override wins; else ``$LOGSEQ_GRAPHS_DIR``'s parent (the env
    var points at the graphs/ dir, not the root); else ``~/logseq``."""
    if override:
        return Path(override).expanduser().resolve()
    env = os.environ.get("LOGSEQ_GRAPHS_DIR")
    if env:
        return Path(env).expanduser().resolve().parent
    return (Path.home() / "logseq").resolve()


def _atom_graph_name(workflow_name: str, override: str | None = None) -> str:
    """The co-located graph name: always ``atom.<slug>`` (slugged for a filesystem-safe, collision-
    resistant, traversal-free graph/dir name)."""
    return f"{ATOM_GRAPH_PREFIX}{_slug(override or workflow_name)}"


def _list_graph_names(run: CLIRunner, root_dir: Path) -> list[str]:
    """The graph names under a Logseq root-dir (via ``graph list``), or [] on parse failure.
    Shared by ``ensure_vault`` (create-if-absent) and ``clear_vault`` (remove-if-present)."""
    _rc, out, _err = run(["logseq", "graph", "list", "--root-dir", str(root_dir), "--output", "json"])
    try:
        return (json.loads(out).get("data") or {}).get("graphs") or []
    except (ValueError, AttributeError):
        return []


def notes_root(home, workflow_name: str) -> Path:
    return atom_home(home) / "notes" / _slug(workflow_name)


def _default_runner(args: list[str]) -> "tuple[int, str, str]":
    if shutil.which(args[0]) is None:
        raise FileNotFoundError(
            f"'{args[0]}' CLI not found on PATH. Install the Logseq CLI to use persistent notes."
        )
    proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
    return proc.returncode, proc.stdout, proc.stderr


def ensure_vault(home, workflow_name: str, notes_cfg, *, runner: Optional[CLIRunner] = None) -> NotesBinding:
    """Ensure the workflow's Logseq graph exists (create once, reuse thereafter). Idempotent."""
    provider = getattr(notes_cfg, "provider", "logseq")
    if provider != "logseq":
        raise NotImplementedError(f"notes provider '{provider}' is not supported")
    run = runner or _default_runner
    root = notes_root(home, workflow_name)
    root.mkdir(parents=True, exist_ok=True)
    graph = getattr(notes_cfg, "graph", None) or _slug(workflow_name)

    _rc, out, _err = run(["logseq", "graph", "list", "--root-dir", str(root), "--output", "json"])
    try:
        existing = (json.loads(out).get("data") or {}).get("graphs") or []
    except (ValueError, AttributeError):
        existing = []
    if graph not in existing:
        run(["logseq", "graph", "create", "--graph", graph, "--root-dir", str(root)])
    return NotesBinding(provider="logseq", root_dir=str(root), graph=graph)


def clear_vault(home, workflow_name: str) -> bool:
    """Delete a workflow's persistent Logseq vault. Idempotent; returns whether one existed.

    Confined to ``$ATOM_HOME/notes/``: refuses to remove that directory itself or any path
    outside it. A fresh vault is re-provisioned by :func:`ensure_vault` on the next
    notes-enabled run, so this is a full reset rather than a content wipe.
    """
    notes_base = (atom_home(home) / "notes").resolve()
    root = notes_root(home, workflow_name).resolve()
    if root == notes_base or not root.is_relative_to(notes_base):
        raise ValueError(f"refusing to clear a path outside {notes_base}: {root}")
    if root.exists():
        shutil.rmtree(root)
        return True
    return False
