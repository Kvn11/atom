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


def ensure_vault(
    home,
    workflow_name: str,
    notes_cfg,
    *,
    expose_to_logseq: bool = False,
    logseq_root_dir: Optional[str] = None,
    runner: Optional[CLIRunner] = None,
) -> NotesBinding:
    """Ensure the workflow's Logseq graph exists (create once, reuse thereafter). Idempotent.

    When ``expose_to_logseq`` is True the graph is provisioned as ``atom.<slug>`` inside the desktop
    app's graph home (``resolve_logseq_root(logseq_root_dir)``) so it appears in the app's switcher;
    otherwise it lives isolated at ``$ATOM_HOME/notes/<slug>/`` under a bare-slug graph name.
    """
    provider = getattr(notes_cfg, "provider", "logseq")
    if provider != "logseq":
        raise NotImplementedError(f"notes provider '{provider}' is not supported")
    run = runner or _default_runner
    graph_override = getattr(notes_cfg, "graph", None)

    if expose_to_logseq:
        root = resolve_logseq_root(logseq_root_dir)
        graph = _atom_graph_name(workflow_name, graph_override)
    else:
        root = notes_root(home, workflow_name)
        graph = graph_override or _slug(workflow_name)
    root.mkdir(parents=True, exist_ok=True)

    if graph not in _list_graph_names(run, root):
        run(["logseq", "graph", "create", "--graph", graph, "--root-dir", str(root)])
    return NotesBinding(provider="logseq", root_dir=str(root), graph=graph)


def _is_busy(err: str) -> bool:
    """The `graph remove` failure that means the graph is open in the GUI (db-worker error 97)."""
    e = (err or "").lower()
    return "owned by another process" in e or "already locked" in e


def clear_vault(
    home,
    workflow_name: str,
    *,
    expose_to_logseq: bool = False,
    logseq_root_dir: Optional[str] = None,
    graph_override: Optional[str] = None,
    runner: Optional[CLIRunner] = None,
) -> bool:
    """Delete a workflow's persistent Logseq vault. Idempotent; returns whether one existed.

    Exposed mode: ``graph remove`` the ``atom.<slug>`` graph from the desktop app's home. The name
    is always namespaced, so a user's personal graph in the same home is never touched. Raises
    :class:`VaultBusyError` if the graph is currently open in the GUI (db-worker error 97).

    Isolated mode (default): path-confined ``rmtree`` of ``$ATOM_HOME/notes/<slug>/`` (legacy).
    A fresh vault is re-provisioned by :func:`ensure_vault` on the next notes-enabled run.
    """
    if expose_to_logseq:
        run = runner or _default_runner
        root = resolve_logseq_root(logseq_root_dir)
        graph = _atom_graph_name(workflow_name, graph_override)
        # Keyed on the current graph NAME: if a workflow's notes.graph override changed since the
        # vault was provisioned, a stale-named vault would be left untouched (rare). Isolated mode
        # is name-agnostic (removes the whole notes/<slug> dir).
        if not graph.startswith(ATOM_GRAPH_PREFIX):   # belt-and-suspenders; _atom_graph_name enforces it
            raise ValueError(f"refusing to remove non-atom graph '{graph}'")
        if graph not in _list_graph_names(run, root):
            return False
        rc, _out, err = run(["logseq", "graph", "remove", "--graph", graph, "--root-dir", str(root)])
        if rc != 0:
            if _is_busy(err):
                raise VaultBusyError(
                    f"graph '{graph}' is open in the Logseq desktop app; close it and retry")
            raise RuntimeError(f"logseq graph remove failed (rc={rc}): {err.strip()}")
        return True

    notes_base = (atom_home(home) / "notes").resolve()
    root = notes_root(home, workflow_name).resolve()
    if root == notes_base or not root.is_relative_to(notes_base):
        raise ValueError(f"refusing to clear a path outside {notes_base}: {root}")
    if root.exists():
        shutil.rmtree(root)
        return True
    return False
