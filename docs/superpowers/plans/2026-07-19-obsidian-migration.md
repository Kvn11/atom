# Logseq → Obsidian Persistent-Notes Migration — Implementation Plan (device `obsidian` CLI)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Replace Logseq with Obsidian for per-workflow notes, driven by the device `obsidian` CLI (a bridge to the running Obsidian app). A workflow NAMES a registered vault; atom validates it (never provisions/registers/deletes).

**Architecture:** `ensure_vault` runs `obsidian vaults verbose`, confirms the named vault is registered, resolves its path. Agents work the vault via `obsidian vault=<name> <cmd>`. curate-kb uses the CLI for sensing + a file-walk script only for multi-note island detection.

**Tech Stack:** Python 3.12, Pydantic v2, Typer, FastAPI, Jinja2, pytest; the compiled `obsidian` CLI at `/usr/local/bin/obsidian` (v1.12.7).

## Global Constraints
- Provider value is exactly `"obsidian"`. Every `obsidian` call carries `vault=<name>` (determinism vs. the CLI's active-vault default).
- Config: `notes.obsidian_cli: str = "obsidian"`. Workflow field `notes.vault` (defaults to the workflow name).
- `NotesBinding(provider, vault, root_dir)` where `vault` = registered name, `root_dir` = resolved path.
- atom NEVER creates/registers/deletes an Obsidian vault. The `notes clear` CLI+endpoint are removed.
- Full pytest suite green at the end of each task. Run: `cd /Users/kev/gitclones/atom && .venv/bin/pytest`.

See `docs/superpowers/specs/2026-07-19-obsidian-migration-design.md` for rationale + the CLI surface.

---

## Task 1: Backend notes cutover (validate-based) + remove `notes clear`

The notes-clear command currently `rmtree`s the vault; under the name-a-registered-vault model that would delete the user's KB, so it is removed **in this same task** (never ship an intermediate that deletes a user vault).

**Files:**
- Rewrite: `src/atom/notes.py`
- Modify: `src/atom/config/schema.py` (`NotesRuntimeConfig`), `src/atom/workflow/schema.py` (`NotesConfig`), `src/atom/workflow/engine.py` (ensure_vault call)
- Remove notes-clear from: `src/atom/cli.py`, `src/atom/api/app.py`
- Test: `tests/test_notes.py` (rewrite), `tests/test_config.py`, `tests/test_workflow_schema.py`, `tests/test_workflow_engine.py`, `tests/test_workflow_cli.py`, `tests/test_workflow_api.py`

**Interfaces produced:**
- `NotesBinding(provider: str, vault: str, root_dir: str)`; `.as_prompt_ctx() -> {"provider","vault","root_dir"}`
- `VaultNotRegisteredError(vault: str, known: list[str])` (RuntimeError)
- `_list_vaults(run, cli) -> dict[str,str]`
- `ensure_vault(workflow_name, notes_cfg, *, cli="obsidian", runner=None) -> NotesBinding`
- Config `cfg.notes.obsidian_cli`; workflow `workflow.notes.vault`, `.provider == "obsidian"`

- [ ] **Step 1: Rewrite `tests/test_notes.py` (failing)**

```python
"""Persistent-notes vault validation (Obsidian CLI), with an injected fake runner."""
from types import SimpleNamespace
import pytest
from atom.notes import NotesBinding, VaultNotRegisteredError, ensure_vault, _list_vaults

def _runner(vaults):
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
    assert b.as_prompt_ctx() == {"provider": "obsidian", "vault": "kalshi", "root_dir": "/repos/kalshi/kb"}

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
```

- [ ] **Step 2: Run — expect FAIL** — `cd /Users/kev/gitclones/atom && .venv/bin/pytest tests/test_notes.py -q` (import errors).

- [ ] **Step 3: Rewrite `src/atom/notes.py`**

```python
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
    """Validate the workflow's named Obsidian vault is registered and resolve its path.

    The vault name is ``notes_cfg.vault`` (falling back to the workflow name). atom does NOT create
    or register vaults; an unknown name raises VaultNotRegisteredError and the engine halts the run
    cleanly.
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
```

- [ ] **Step 4: Run — expect PASS** — `.venv/bin/pytest tests/test_notes.py -q`.

- [ ] **Step 5: `src/atom/config/schema.py` — replace `NotesRuntimeConfig` body**

```python
class NotesRuntimeConfig(_Base):
    # Persistent notes are backed by Obsidian vaults reached through the device `obsidian` CLI
    # (a bridge to the running Obsidian app). A workflow NAMES a registered vault (notes.vault);
    # atom validates the name is known and never creates/registers/deletes vaults.
    obsidian_cli: str = "obsidian"   # CLI binary name/path (override for a non-PATH install)
```

- [ ] **Step 6: `src/atom/workflow/schema.py` — replace `NotesConfig`**

```python
class NotesConfig(_Base):
    """Opt-in persistent notes for a workflow, backed by a registered Obsidian vault."""

    enabled: bool = False
    provider: Literal["obsidian"] = "obsidian"
    vault: Optional[str] = None   # registered vault name; defaults (in atom.notes) to the workflow name
```

- [ ] **Step 7: `src/atom/workflow/engine.py` — update the ensure_vault call**

```python
                    notes_binding = ensure_vault(
                        workflow.name, workflow.notes, cli=self.cfg.notes.obsidian_cli,
                    )
```

- [ ] **Step 8: Remove `notes clear` from `src/atom/cli.py`**

Delete the whole `notes_app` sub-app block (the `notes_app = typer.Typer(...)`, `workflow_app.add_typer(notes_app, name="notes")`, and the `@notes_app.command("clear")` function — approx lines 172-230) and any now-unused imports. Verify nothing else references `notes_app`.

- [ ] **Step 9: Remove the clear endpoint from `src/atom/api/app.py`**

Delete the `@app.delete("/api/workflows/{name}/notes")` function (approx lines 109-131). Verify no other reference to `clear_workflow_notes`.

- [ ] **Step 10: Update caller/config tests**

- `tests/test_config.py`: assert `AtomConfig().notes.obsidian_cli == "obsidian"`; remove old `expose_to_logseq`/`logseq_root_dir` assertions.
- `tests/test_workflow_schema.py`: provider `"obsidian"`; `NotesConfig(vault="x").vault == "x"`.
- `tests/test_workflow_engine.py`: update the `ensure_vault` monkeypatch/expectation to the new signature (`workflow.name, workflow.notes, cli=...`) and assert an unregistered vault halts the run (status `halted`, first task error mentions the vault). Read the file first; adapt the existing notes test.
- `tests/test_workflow_cli.py` and `tests/test_workflow_api.py`: **delete** the notes-clear test cases entirely (read first; remove the whole test functions + unused imports).

- [ ] **Step 11: Run the backend slice — expect PASS**

`cd /Users/kev/gitclones/atom && .venv/bin/pytest tests/test_notes.py tests/test_config.py tests/test_workflow_schema.py tests/test_workflow_engine.py tests/test_workflow_cli.py tests/test_workflow_api.py -q`

- [ ] **Step 12: Commit**

```bash
cd /Users/kev/gitclones/atom && git add -A
git commit -m "feat(notes): validate-based Obsidian vault binding via the device CLI; drop notes-clear

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Prompts + subagent/search touch-ups

**Files:** `src/atom/prompts/lead_system.md`, `src/atom/prompts/subagent_bash.md`, `src/atom/subagent.py:68`, `src/atom/tools/search.py:85`; tests `tests/test_prompts.py`, `tests/test_subagent.py`, `tests/test_search.py`, `tests/test_library.py`.

**Interfaces consumed:** prompt ctx `notes.vault`, `notes.root_dir`.

- [ ] **Step 1: Update `tests/test_prompts.py` + `tests/test_subagent.py` first (failing)** — change the `notes={...}` fixtures to `{"provider":"obsidian","vault":X,"root_dir":Y}`; assert the rendered block says `Obsidian`, references `obsidian vault=<vault>`, and contains no `logseq`/`load_skill("logseq-cli")`. Rename incidental `logseq-cli` example skills to `demo-skill`.

- [ ] **Step 2: Run — expect FAIL** — `.venv/bin/pytest tests/test_prompts.py tests/test_subagent.py -q`.

- [ ] **Step 3: Rewrite the `lead_system.md` notes block (lines 17-20)**

```
{% if notes %}
# Persistent notes (Obsidian)
A registered Obsidian vault named `{{ notes.vault }}` (on disk at `{{ notes.root_dir }}`) persists across every run of this workflow — treat it as long-term memory. Reach it with the `obsidian` CLI via `bash`, always passing `vault={{ notes.vault }}`: e.g. `obsidian vault={{ notes.vault }} files`, `obsidian vault={{ notes.vault }} read file="<Note>"`, `obsidian vault={{ notes.vault }} append file="<Note>" content="<text>"`, `obsidian vault={{ notes.vault }} search query="<text>"`. Run `obsidian help` (or `obsidian help <command>`) for the full command list. Before you start, read what earlier runs left; as you work, record durable notes so future runs can build on them.
{% endif %}
```

- [ ] **Step 4: Rewrite the `subagent_bash.md` notes block (lines 13-16)**

```
{% if notes %}
# Persistent notes (Obsidian)
This workflow has a registered Obsidian vault (long-term memory shared across runs): `{{ notes.vault }}` at `{{ notes.root_dir }}`. If your task involves it, reach it with the `obsidian` CLI via bash, always passing `vault={{ notes.vault }}` (e.g. `obsidian vault={{ notes.vault }} read file="<Note>"`, `... append file="<Note>" content="<text>"`). Run `obsidian help` for the command list.
{% endif %}
```

- [ ] **Step 5: Update the two comments** — `subagent.py:68` → `# per-workflow Obsidian vault ctx (vault/root_dir); bash children only`; `search.py:85` → `e.g. "curate-knowledge-base"`.

- [ ] **Step 6: Update `tests/test_search.py`, `tests/test_library.py`** — rename incidental `logseq-cli` fixtures to `demo-skill` (and the `/mnt/skills/demo-skill/` assertion). Read first; replace exact strings.

- [ ] **Step 7: Run — expect PASS** — `.venv/bin/pytest tests/test_prompts.py tests/test_subagent.py tests/test_search.py tests/test_library.py -q`.

- [ ] **Step 8: Commit** — `feat(prompts): drive persistent notes through the obsidian CLI (vault=<name>)`.

---

## Task 3: curate-kb file-walk scripts (islands + recent)

**Files:** Create `scripts/_vault_ids.py`, `scripts/find-disconnected-notes.py`, `scripts/find-recently-modified-notes.py`; delete `scripts/find-disconnected-pages.py`; rename test `tests/test_curate_disconnected_pages.py` → `tests/test_curate_disconnected_notes.py`.

- [ ] **Step 1: Restore the three scripts verbatim from `c969e61`**

```bash
cd /Users/kev/gitclones/atom
for f in _vault_ids.py find-disconnected-notes.py find-recently-modified-notes.py; do
  git show "c969e61:skill_library/curate-knowledge-base/scripts/$f" \
    > "skill_library/curate-knowledge-base/scripts/$f"
done
```
(These are islands/isolated-only + mtime/git recent detection — the CLI supplies orphans/deadends/unresolved, so no extension is needed.)

- [ ] **Step 2: Write `tests/test_curate_disconnected_notes.py`**

```python
"""Obsidian curate-kb file-walk scripts: island detection + recent-change scoping."""
import importlib.util, sys
from pathlib import Path
SCRIPTS = Path(__file__).resolve().parents[1] / "skill_library" / "curate-knowledge-base" / "scripts"

def _load(name):
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod

def _mkvault(tmp_path):
    v = tmp_path / "vault"; v.mkdir()
    (v / "A.md").write_text("Links to [[B]] and [[C]].")
    (v / "B.md").write_text("Back to [[A]].")
    (v / "C.md").write_text("Mentions [[A]].")
    (v / "D.md").write_text("Talks to [[E]].")
    (v / "E.md").write_text("Talks to [[D]].")
    (v / "F.md").write_text("Isolated note.")
    return v

def test_islands_and_isolated(tmp_path):
    r = _load("find-disconnected-notes").analyze_vault(_mkvault(tmp_path))
    assert r["note_count"] == 6
    assert r["main_component"]["size"] == 3
    assert sorted(r["main_component"]["members"]) == ["A", "B", "C"]
    assert [sorted(i["members"]) for i in r["islands"]] == [["D", "E"]]
    assert r["isolated"] == ["F"]

def test_recent_mtime(tmp_path):
    import os, time
    mod = _load("find-recently-modified-notes"); v = _mkvault(tmp_path)
    old = time.time() - 10_000
    for p in v.glob("*.md"): os.utime(p, (old, old))
    os.utime(v / "A.md", None)
    assert mod.list_recent(v, since=str(time.time() - 100))["changed"] == ["A"]
```

- [ ] **Step 3: Run — expect FAIL then PASS** — after Step 1 the scripts exist, so run `.venv/bin/pytest tests/test_curate_disconnected_notes.py -q` and expect PASS. (If the restored `find-disconnected-notes.py` returns extra keys that's fine; the asserts check only the ones above.)

- [ ] **Step 4: Delete the Logseq script + old test**

```bash
cd /Users/kev/gitclones/atom
git rm skill_library/curate-knowledge-base/scripts/find-disconnected-pages.py tests/test_curate_disconnected_pages.py
```

- [ ] **Step 5: Run — expect PASS** — `.venv/bin/pytest tests/test_curate_disconnected_notes.py -q`.

- [ ] **Step 6: Commit** — `feat(skill): restore Obsidian file-walk island/recent scripts; drop logseq edge script`.

---

## Task 4: curate-kb SKILL.md rewrite (obsidian CLI)

**Files:** Rewrite `skill_library/curate-knowledge-base/SKILL.md`. No test (reviewed prose).

- [ ] **Step 1: Rewrite SKILL.md** — base structure on `git show c969e61:.../SKILL.md`, keep the full map-reduce methodology + §12 invariants, and replace the fictional `obsidian <verb>` CLI / `/mnt/user-data/` / `/mnt/skills/public/obsidian-lint/` with the REAL device CLI + atom `root_dir` + `<skill_dir>/scripts/`:
  - Unit of work = registered vault `vault=<NAME>` (+ `root_dir=<PATH>` for the island script). Every `obsidian` call carries `vault=<NAME>`. No vault → STOP.
  - §3 Sense: `obsidian vault=<N> orphans` / `deadends` / `unresolved` / `tags counts` / `properties counts` / `files`; `python3 <skill_dir>/scripts/find-disconnected-notes.py <root_dir> --json` for islands; incremental via `find-recently-modified-notes.py`.
  - §5/§7/§11 workers: read notes via `obsidian vault=<N> read file="<Note>"`; edges via `links`/`backlinks`. No block ids — quote the claim.
  - §8/§9: append a `> [!curator]` callout (`obsidian vault=<N> append file="<Note>" content='> [!curator] …'`) + a machine-readable `obsidian vault=<N> property:set name=curator-flag value=<type> file="<Note>"`. `[[wikilinks]]` allowed in callout content. Delete ALL Logseq apparatus (Datascript/upsert/schema-bootstrap/propagation+ident warnings). Idempotency: `obsidian vault=<N> search query="[!curator]" format=json`; prune via `read`→edit→`create overwrite`.
  - Frontmatter `description`: Obsidian vault via the `obsidian` CLI; `> [!curator]` callout flags.

- [ ] **Step 2: Verify no stale refs** — `grep -niE 'logseq|/mnt/user-data|/mnt/skills/public|:block/|upsert|datascript' skill_library/curate-knowledge-base/SKILL.md || echo clean` → `clean`.

- [ ] **Step 3: Commit** — `feat(skill): re-port curate-knowledge-base SKILL.md to the obsidian CLI`.

---

## Task 5: config.yaml, notes-smoke fixture, README

**Files:** `config.yaml`, `workflows/notes-smoke.yaml`, `README.md`.

- [ ] **Step 1: `config.yaml` notes block (lines 31-34)**

```yaml
notes:
  obsidian_cli: obsidian   # device CLI that bridges to the running Obsidian app. A workflow names a
                           # REGISTERED vault (notes.vault); atom validates it (never creates/deletes).
```

- [ ] **Step 2: Rewrite `workflows/notes-smoke.yaml`**

```yaml
# workflows/notes-smoke.yaml — copy to $ATOM_HOME/workflows/ to run it.
# Persistent-notes smoke test. Point notes.vault at a vault you have REGISTERED in Obsidian
# ("Open folder as vault"). Run it TWICE — the second run's Recall sees the first run's note.
name: notes-smoke
description: Smoke-test persistent notes — recall prior notes in a registered Obsidian vault, then record a new one.
notes:
  enabled: true
  vault: notes-smoke          # <-- must match a vault registered in Obsidian
inputs:
  - name: entry
    required: false
    default: "hello from a notes-smoke run"
steps:
  - title: Recall
    description: Read what earlier runs left in the vault.
    tasks:
      - id: recall
        prompt: >
          Using the obsidian CLI (always pass vault=notes-smoke), list the vault's files and read
          any note this workflow created before; report how many exist and what they say. If none
          exist yet, say so plainly.
        model: haiku
        thinking: low
  - title: Record
    description: Append a dated note, then confirm it persisted.
    tasks:
      - id: record
        prompt: >
          Using the obsidian CLI (always pass vault=notes-smoke), create or append a dated note with
          the content "{{ entry }} ({{ date }})". Confirm by reading it back, then write a one-line
          confirmation to {{ outputs }}/notes-confirmation.md and call present_files on it.
        model: haiku
        thinking: low
```

- [ ] **Step 3: Rewrite the README persistent-notes section** — replace the `logseq`-CLI prerequisite (README ~23-24) and the `expose_to_logseq` prose (~79-94). New prereq: the `obsidian` CLI on PATH + the app running during runs + the named vault registered in Obsidian. New behavior: `notes.enabled: true` + `notes.vault: <registered-name>` (defaults to the workflow name); atom validates the vault is registered and halts cleanly if not; the agent works it via `obsidian vault=<name> <cmd>`. Point at `workflows/notes-smoke.yaml` (register a vault named `notes-smoke`, run twice). Remove the `logseq --version` line.

- [ ] **Step 4: Full-suite verification** — `cd /Users/kev/gitclones/atom && .venv/bin/pytest -q` → all green.

- [ ] **Step 5: Final grep — no Logseq in the shipped surface** — `grep -rni logseq src/ skill_library/ tests/ config.yaml workflows/ README.md | grep -v egg-info || echo clean` → `clean`.

- [ ] **Step 6: Commit** — `docs(config): flip config.yaml/notes-smoke/README to the obsidian CLI; drop logseq prereq`.

---

## Self-Review
- **Spec coverage:** every §5 spec file maps to a task (backend→T1, prompts/touch-ups→T2, scripts→T3, SKILL→T4, config/fixtures/README→T5). Feature removal (notes-clear) folded into T1 to avoid a dangerous intermediate. Non-goals excluded.
- **Type consistency:** `NotesBinding(provider, vault, root_dir)`, `ensure_vault(workflow_name, notes_cfg, *, cli, runner)`, `VaultNotRegisteredError`, `_list_vaults`, `cfg.notes.obsidian_cli`, `workflow.notes.vault` used identically across tasks.
- **Placeholder scan:** mechanical test edits (T1.10, T2.6) say "read first, replace exact strings" — find/replace of already-specified identifiers, not new logic.
```
