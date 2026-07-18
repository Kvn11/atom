"""Persistent workflow notes: provision + reuse a per-workflow Logseq vault (graph).

The vault lives OUTSIDE any per-run workspace, keyed by workflow name, so it is shared across
every run of that workflow. ``ensure_vault`` is idempotent (list-then-create-if-absent).
"""
from __future__ import annotations

import json
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
