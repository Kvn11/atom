# Migrate persistent-notes backbone: Logseq → Obsidian (device `obsidian` CLI)

**Date:** 2026-07-19 (revised 2026-07-20 after discovering the device `obsidian` CLI)
**Status:** Design — model chosen (Option A); notes-clear removal awaiting explicit OK
**Branch:** `feat/obsidian-migration`

## 1. Goal

Replace Logseq with Obsidian as the backbone for per-workflow knowledge bases. Migrate all
scripts, skills, tools, prompts, config, and tests so atom has no remaining Logseq dependency.
Clean cutover — Logseq is dropped entirely.

## 2. The decisive discovery: a device `obsidian` CLI

The target device provides a real, compiled `obsidian` CLI at `/usr/local/bin/obsidian` (v1.12.7)
— the Obsidian analog of the device-provided `logseq` CLI. It is a **bridge to the running
Obsidian app** (the app is **guaranteed running while a workflow runs**), with a rich surface:

- **Addressing:** `obsidian <command> [options]`, with a global `vault=<name>` selector. Files are
  addressed by `file=<name>` (wikilink-style resolution) or `path=<folder/note.md>` (exact).
- **Read/sense:** `read`, `files`, `folders`, `links`, `backlinks`, `orphans`, `deadends`,
  `unresolved`, `tags`, `properties`, `outline`, `search`, `search:context`.
- **Write:** `create`, `append`, `prepend`, `move`, `rename`, `delete`, `property:set/read/remove`.
- **Vault registry:** `vaults [verbose]` lists known vaults (name → path); `vault [info=path]`
  shows one vault's info.

**Two hard constraints this imposes:**

1. **Vaults are addressed by *registered name*, never by path.** There is no `vault=<path>` and no
   CLI command to add/open/register a vault. A directory Obsidian does not know about is
   unreachable. Registration lives in `~/Library/Application Support/obsidian/obsidian.json`.
2. **`vault=<name>` is optional and defaults to the *active* vault** (whatever is focused in the
   GUI). For deterministic, unattended, concurrent runs, atom must pass `vault=<name>` on **every**
   call — exactly the invariant the Logseq design enforced with `--graph <NAME>`.

The user's environment already has ~18 registered vaults, each **co-located inside its project git
repo** (`brain`→`/Users/kev/gitclones/brain`, `kalshi`→`…/kalshi_arb/knowledge/kalshi`, `*_kb`,
`*-knowledge`, …). They deliberately manage per-project knowledge bases addressed by name.

## 3. The model (chosen: Option A — name a registered vault)

Because the CLI addresses vaults by registered name and cannot create/register one, and because the
user already curates per-project vaults, a workflow **names an existing registered vault**:

- Workflow YAML: `notes.enabled: true` + `notes.vault: <name>` (defaults to the workflow name when
  omitted). The named vault must already be registered in Obsidian.
- **atom validates, never provisions.** At run start `ensure_vault` runs `obsidian vaults verbose`,
  confirms the name is registered, and resolves its on-disk path. If the vault is not registered the
  run **halts cleanly** with a message telling the user to open it in Obsidian and retry (reusing
  the engine's existing notes-setup-failure halt path). atom never creates, registers, or deletes a
  vault.
- **Agent interaction:** the lead/bash agents work the vault through the CLI, always
  `obsidian vault=<name> <cmd> …`. The app is guaranteed up, so the bridge works. `bash` is required
  (the CLI is a shell command); `notes.enabled` workflows therefore assume `sandbox.bash_enabled`.

**Alternatives considered and rejected:**
- *Auto-provision + register `atom.<slug>`* (write `obsidian.json`, `restart` the app): preserves
  zero-setup but edits the user's real config (risking their 18 vaults), needs an app restart to
  register (fragile), and clutters their switcher. Rejected — the risk/complexity isn't worth it
  for a user who already manages vaults deliberately.
- *File tools only (no CLI):* works on the directory, but ignores the capable device CLI, can't use
  its `orphans`/`backlinks`/`tags`/`search` sensing, and diverges from the Logseq design's shape.
  Rejected now that a real CLI exists.

**Consequence — the `notes clear` feature is removed.** `atom workflow notes clear` and
`DELETE /api/workflows/{name}/notes` currently `rmtree` the vault. Under Option A the vault is
**user-owned**, so a "delete the whole vault" action is dangerous and semantically wrong. Both are
removed. *(This removes a feature previously built; flagged for explicit confirmation before the
removal task runs.)*

## 4. Terminology & config mapping

| Logseq | Obsidian |
|---|---|
| graph | vault |
| `NotesBinding(provider, root_dir, graph)` | `NotesBinding(provider, vault, root_dir)` — `vault` = registered name, `root_dir` = its resolved path |
| `NotesConfig.provider: Literal["logseq"]` | `Literal["obsidian"]` |
| `NotesConfig.graph` | `NotesConfig.vault` (defaults to workflow name; must resolve to a registered vault) |
| `notes.expose_to_logseq`, `notes.logseq_root_dir` | **removed** (no provisioning / co-location) |
| — | `notes.obsidian_cli: str = "obsidian"` (CLI binary name/path) |
| `logseq graph list` (validate) | `obsidian vaults verbose` (validate + resolve path) |
| `--graph <NAME>` on every call | `vault=<name>` on every call |
| `clear_vault`, `VaultBusyError`, `ATOM_GRAPH_PREFIX`, `resolve_logseq_root`, `_atom_graph_name` | **removed** |
| prompt ctx `notes.graph` | prompt ctx `notes.vault` (+ `notes.root_dir` for the island script) |

## 5. File-by-file plan

### Core: `src/atom/notes.py` (rewrite → validate + resolve)
- Delete: `clear_vault`, `VaultBusyError`, `_is_busy`, `ATOM_GRAPH_PREFIX`, `resolve_logseq_root`,
  `_atom_graph_name`, `_list_graph_names`, `notes_root`, the exposed/isolated mode split.
- Keep a `CLIRunner` seam + `_default_runner` (shells the CLI; `FileNotFoundError` if `obsidian`
  missing) for testability.
- `NotesBinding(provider: str, vault: str, root_dir: str)`; `as_prompt_ctx()` →
  `{"provider","vault","root_dir"}`.
- `class VaultNotRegisteredError(RuntimeError)` — carries the requested name + the known names.
- `_list_vaults(run, cli) -> dict[str,str]`: parse `obsidian vaults verbose` (`name\tpath` lines)
  into a name→path map.
- `ensure_vault(workflow_name, notes_cfg, *, cli="obsidian", runner=None) -> NotesBinding`:
  `vault = notes_cfg.vault or workflow_name`; look it up in `_list_vaults`; raise
  `VaultNotRegisteredError` if absent; else return the binding with the resolved path.
- Module docstring re-grounded on the CLI + validate-don't-provision model.

### `src/atom/config/schema.py`
- Replace `NotesRuntimeConfig` fields with a single `obsidian_cli: str = "obsidian"`; comments
  rewritten. (No `expose_*`/`*_root_dir`.)

### `src/atom/workflow/schema.py`
- `NotesConfig`: `provider: Literal["obsidian"] = "obsidian"`, `graph` → `vault: Optional[str] = None`
  (defaults at runtime to the workflow name), docstring updated.

### `src/atom/workflow/engine.py`
- The `ensure_vault(...)` call → `ensure_vault(workflow.name, workflow.notes, cli=self.cfg.notes.obsidian_cli)`.
  The surrounding halt-on-failure block is unchanged (now also catches `VaultNotRegisteredError`).

### `src/atom/cli.py` and `src/atom/api/app.py`
- **Remove** the `notes` Typer sub-app / `workflow notes clear` command and the
  `DELETE /api/workflows/{name}/notes` endpoint (and the now-unused `clear_vault` imports). *(Gated
  on explicit confirmation.)*

### Prompts (`lead_system.md`, `subagent_bash.md`)
- Retitle `# Persistent notes (Obsidian)`. Body: a registered Obsidian vault named `{{ notes.vault }}`
  (on disk at `{{ notes.root_dir }}`) persists across runs — long-term memory. Reach it with the
  `obsidian` CLI via `bash`, **always** passing `vault={{ notes.vault }}` (e.g.
  `obsidian vault={{ notes.vault }} files`, `… read file="<Note>"`,
  `… append file="<Note>" content="…"`, `… search query="…"`). Run `obsidian help` /
  `obsidian help <command>` for the full command list. Read what earlier runs left before starting;
  record durable notes as you go. Drop `load_skill("logseq-cli")`.

### `src/atom/subagent.py`, `src/atom/tools/search.py`
- `subagent.py:68` comment → `per-workflow Obsidian vault ctx (vault/root_dir); bash children only`.
- `search.py:85` docstring example `"logseq-cli"` → `"curate-knowledge-base"`.

### Skill: `skill_library/curate-knowledge-base/`
- **Scripts:** restore `_vault_ids.py` and `find-disconnected-notes.py` (islands/isolated only — no
  need to derive orphans/deadends; the CLI provides those) and `find-recently-modified-notes.py`
  (incremental `--since`/`--since-git`; the CLI has no "modified since"). All run on the vault
  **path** (`root_dir`). Delete `find-disconnected-pages.py`.
- **SKILL.md:** re-port to the real `obsidian` CLI (keep the map-reduce methodology). Substrate:
  - Unit of work = a registered Obsidian vault named `vault=<NAME>` (with `root_dir=<PATH>` for the
    island script). Every `obsidian` call carries `vault=<NAME>`. No default vault → STOP.
  - §3 Sense: `obsidian vault=<N> orphans|deadends|unresolved|tags counts|properties counts|files`
    for terrain; `python3 <skill_dir>/scripts/find-disconnected-notes.py <root_dir> --json` for
    multi-note islands (the one thing the CLI can't do). Incremental via
    `find-recently-modified-notes.py`.
  - §5/§7/§11 workers read notes with `obsidian vault=<N> read file="<Note>"` and edges with
    `links`/`backlinks`. No block ids — locate a claim by quoted text/heading.
  - §8/§9 annotations: append an Obsidian `> [!curator]` callout to the note
    (`obsidian vault=<N> append file="<Note>" content='> [!curator] …'`), plus a machine-readable
    `property:set` flag (e.g. `curator-flag`); `[[wikilinks]]` in callout content are fine (plain
    markdown — no propagation). Delete the entire Logseq apparatus (Datascript, `upsert`, schema
    bootstrap, propagation/ident warnings). Idempotency: enumerate flags with
    `obsidian vault=<N> search query="[!curator]" format=json`; prune a resolved flag by
    `read`→edit→`create overwrite`.
  - Keep all §12 invariants (vault-from-prompt, earned edits only, detect-and-flag-never-adjudicate,
    dual-channel, no vault logs, convergence, unattended).

### Config & fixtures
- `config.yaml` `notes:` block → `obsidian_cli: obsidian` (+ explanatory comment). No expose/root.
- `workflows/notes-smoke.yaml`: set `notes.vault: <a registered vault>`; tasks use
  `obsidian vault=… files|append` (no `load_skill`). Document that the named vault must be
  registered. Run twice to see recall.
- `README.md`: rewrite the persistent-notes section. Prerequisite becomes: the `obsidian` CLI on
  PATH + the named vault registered in Obsidian (app running during runs). Remove the `logseq`
  prerequisite.

### Tests
- `tests/test_notes.py` — rewrite around a fake CLI runner: `ensure_vault` resolves a registered
  vault to its path; unknown vault raises `VaultNotRegisteredError`; `vault` defaults to the
  workflow name; provider guard. (No clear/VaultBusy tests.)
- `tests/test_config.py` — `NotesRuntimeConfig.obsidian_cli` default; old fields gone.
- `tests/test_workflow_schema.py` — provider `"obsidian"`, `vault` field.
- `tests/test_workflow_engine.py` — `ensure_vault` called with the CLI; halt on unregistered vault.
- `tests/test_workflow_cli.py`, `tests/test_workflow_api.py` — **remove** the notes-clear cases.
- `tests/test_prompts.py`, `tests/test_subagent.py` — notes ctx uses `vault`+`root_dir`, provider
  `obsidian`, the `obsidian` CLI block renders; rename incidental `logseq-cli` fixtures.
- `tests/test_search.py`, `tests/test_library.py` — rename incidental `logseq-cli` fixtures.
- `tests/test_curate_disconnected_pages.py` → `tests/test_curate_disconnected_notes.py` — islands
  from the file-walk script over a tmp vault of `.md` files.

## 6. Notes on the user's environment (design inputs)
- Duplicate registered names exist (`knowledge` ×2). `vault=<name>` is then ambiguous — recommend
  unique vault names; validation surfaces the ambiguity if a named vault matches >1 path.
- Vaults live inside git repos, so curate-kb's `--since-git` incremental mode is directly useful.

## 7. Non-goals
- Writing `obsidian.json` / auto-registering vaults / restarting the app.
- A no-app "headless" path (the app is guaranteed running during workflows).
- Converting existing Logseq vault content.

## 8. Verification
- `pytest` full suite green.
- Manual: register a scratch vault, point `notes-smoke.vault` at it, run twice; second Recall sees
  the first run's note (via `obsidian vault=… read`). Confirm an unregistered name halts the run
  cleanly.
- curate-kb scripts unit-tested; SKILL.md reviewed prose.

## 9. Risks
- **Feature removal (`notes clear`)** — behavior change; gated on confirmation.
- **`notes.vault` now required-ish** (defaults to workflow name, which must be a registered vault) —
  breaking for any notes workflow relying on auto-provisioning; documented.
- **Ambiguous duplicate vault names** — surfaced by validation, not silently resolved.
- **curate-kb SKILL.md is prose** — correctness by review; the scripts are unit-tested.
