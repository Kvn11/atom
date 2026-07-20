"""Persistent workflow notes: reach a per-workflow Obsidian vault via the device `obsidian` CLI.

An Obsidian vault is a directory of markdown files the running Obsidian app knows about (registered
in obsidian.json). The `obsidian` CLI addresses a vault by its registered NAME, so atom does not
create or own vaults — a workflow NAMES a registered vault and atom validates it exists (resolving
its on-disk path for the curate-kb island script). The Obsidian app is guaranteed running while a
workflow runs, so the CLI bridge is available.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional

CLIRunner = Callable[[list[str]], "tuple[int, str, str]"]


@dataclass
class NotesBinding:
    provider: str
    vault: str       # the registered Obsidian vault NAME (passed as vault=<name> to the CLI)
    root_dir: str    # the vault's on-disk path (for file-walk scripts)

    def as_prompt_ctx(self) -> dict:
        return {"provider": self.provider, "vault": self.vault, "root_dir": self.root_dir}


class VaultNotRegisteredError(RuntimeError):
    """The named vault is not registered in Obsidian, so the `obsidian` CLI cannot reach it."""

    def __init__(self, vault: str, known: list[str]):
        self.vault = vault
        self.known = known
        shown = ", ".join(known) if known else "(none)"
        super().__init__(
            f"Obsidian vault '{vault}' is not registered. Open it in Obsidian "
            f"('Open folder as vault') and retry. Known vaults: {shown}."
        )


def _default_runner(args: list[str]) -> "tuple[int, str, str]":
    if shutil.which(args[0]) is None:
        raise FileNotFoundError(
            f"'{args[0]}' CLI not found on PATH. The Obsidian CLI is required for persistent notes."
        )
    proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
    return proc.returncode, proc.stdout, proc.stderr


def _list_vaults(run: CLIRunner, cli: str) -> dict[str, str]:
    """Registered vault name -> path, via `obsidian vaults verbose` (tab-separated rows)."""
    _rc, out, _err = run([cli, "vaults", "verbose"])
    registry: dict[str, str] = {}
    for line in (out or "").splitlines():
        if "\t" not in line.strip():
            continue
        name, path = line.split("\t", 1)
        registry[name.strip()] = path.strip()
    return registry


def ensure_vault(
    workflow_name: str,
    notes_cfg,
    *,
    cli: str = "obsidian",
    runner: Optional[CLIRunner] = None,
) -> NotesBinding:
    """Validate the workflow's named Obsidian vault is registered and resolve its on-disk path.

    The vault name is ``notes_cfg.vault`` (falling back to the workflow name). atom does NOT create
    or register vaults; an unknown name raises :class:`VaultNotRegisteredError` and the engine halts
    the run cleanly.
    """
    provider = getattr(notes_cfg, "provider", "obsidian")
    if provider != "obsidian":
        raise NotImplementedError(f"notes provider '{provider}' is not supported")
    vault = getattr(notes_cfg, "vault", None) or workflow_name
    run = runner or _default_runner
    registry = _list_vaults(run, cli)
    if vault not in registry:
        raise VaultNotRegisteredError(vault, sorted(registry))
    return NotesBinding(provider="obsidian", vault=vault, root_dir=registry[vault])
