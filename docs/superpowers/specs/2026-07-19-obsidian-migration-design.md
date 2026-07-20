# Migrate persistent-notes backbone: Logseq → Obsidian

**Date:** 2026-07-19
**Status:** Design — awaiting review
**Branch:** `feat/obsidian-migration`

## 1. Goal

Replace Logseq with Obsidian as the backbone for the per-workflow knowledge bases
("persistent notes"). Migrate **all** scripts, skills, tools, prompts, config, and tests
so atom has no remaining dependency on Logseq or its CLI. This is a clean cutover — Logseq
is dropped entirely, not kept as a second provider.

## 2. Why the two systems differ (the crux)

The current integration is Logseq-DB-native and leans on the `logseq` CLI everywhere:

- **Vault provisioning** (`atom.notes`) shells out to `logseq graph create/remove/list`.
  A "graph" is a Logseq-managed database; the CLI is required to create or inspect one.
- **Desktop visibility** (`expose_to_logseq`) provisions the graph as `atom.<slug>` inside
  `~/logseq`, which the Logseq app **folder-scans** to populate its graph switcher.
- **Curation** (`curate-knowledge-base` skill) uses Datascript queries (`logseq query`),
  block-level writes (`logseq upsert block`), tag/property schema bootstrap, and works around
  Logseq's tag/property **propagation pollution** and random-per-graph ident suffixes.

Obsidian's model is fundamentally different and **much simpler**:

- A **vault is just a directory of `.md` files.** There is no database and **no official CLI.**
  Provisioning is `mkdir`; clearing is `rmtree`. Links are `[[wikilinks]]`; metadata is YAML
  frontmatter; there are no block ids and no tag/property propagation.
- Obsidian does **not** folder-scan. Known vaults live in `obsidian.json`, and the app opens
  one vault per window. So "auto-appear in a switcher" has no direct equivalent.

Because there is no CLI and no DB, most of the Logseq-specific machinery (the CLI runner,
`graph list`, `VaultBusyError`, Datascript, block ids, the propagation warnings) **deletes**
rather than translates. The port is largely a simplification.

**Note:** the curate-kb skill *had* an Obsidian version (recoverable at git `c969e61`), but it was
vendored from a different host ("kiwi") — it references a fictional `obsidian` CLI and
`/mnt/user-data/` paths that **do not exist in atom**. Its pure-stdlib file-walk *scripts* are
reusable; its SKILL.md prose must be re-grounded on atom's reality (file tools, no CLI).

## 3. Approaches considered

### 3a. Desktop visibility (the one real design fork)

`expose_to_logseq: true` today makes a workflow's vault auto-appear in Logseq's switcher.
Three ways to approximate that for Obsidian:

| Option | What it does | Trade-off |
|---|---|---|
| **A. Co-locate + open once** *(chosen default)* | Provision at `~/obsidian/atom.<slug>/`. User does "Open folder as vault" once; Obsidian then remembers it forever. | Safe, minimal code, never touches Obsidian's config. Loses only the *first-time* zero-touch appear. |
| B. Auto-register in `obsidian.json` | atom writes a vault entry into Obsidian's real config so it appears with zero manual steps. | True parity, but invasive, platform-specific, and risks corrupting the user's Obsidian config. Would need a validation spike like the Logseq one did. |
| C. Drop it — isolated only | Only `$ATOM_HOME/notes/<slug>/` vaults; user opens manually if ever. | Simplest, but loses the visibility `expose_to_logseq` gave. |

**Chosen: Option A.** It preserves the *spirit* of expose (a predictable, easy-to-open,
namespaced location) with none of Option B's risk, and it structurally mirrors the existing
Logseq exposed/isolated split so the code and tests map over cleanly. Option B is recorded as a
possible follow-up if zero-touch appearance is later wanted.

> **Open question for review:** confirm Option A, or pick B/C. This is the only decision that
> materially changes `atom.notes` and the config schema.

### 3b. Port strategy

**Chosen: structural mirror.** Keep the existing shape of `atom.notes` (exposed vs. isolated
mode, the `atom.` name-prefix safety guard, `NotesBinding`, `ensure_vault`/`clear_vault`) and
swap the *substrate* from "Logseq CLI graph" to "Obsidian directory". This minimizes churn in
`engine.py`, `cli.py`, `api/app.py`, and the tests, and keeps the load-bearing safety invariants
(path confinement + `atom.` prefix guard) intact. Rejected: a clean-slate rewrite of the notes
module (more churn, loses the proven guard structure for no benefit).

## 4. Terminology & config mapping

| Logseq | Obsidian |
|---|---|
| graph | vault |
| `NotesBinding.graph` | `NotesBinding.vault` |
| `NotesConfig.provider: Literal["logseq"]` | `Literal["obsidian"]` |
| `NotesConfig.graph` (workflow-YAML override) | `NotesConfig.vault` |
| `notes.expose_to_logseq` | `notes.expose_to_obsidian` |
| `notes.logseq_root_dir` (default `~/logseq`) | `notes.obsidian_root_dir` (default `~/obsidian`) |
| `resolve_logseq_root` | `resolve_obsidian_root` |
| `$LOGSEQ_GRAPHS_DIR` env | `$OBSIDIAN_VAULTS_DIR` env (points at the vaults home directly) |
| `ATOM_GRAPH_PREFIX = "atom."` | `ATOM_VAULT_PREFIX = "atom."` (unchanged value; still the safety guard) |
| `_atom_graph_name` | `_atom_vault_name` |
| prompt ctx `notes.graph` | prompt ctx `notes.vault` |

**`root_dir` semantics change (important):** For Logseq, `NotesBinding.root_dir` was the CLI
`--root-dir` (the *parent* home), and `graph` named the DB within it. For Obsidian there is no
CLI indirection: `root_dir` becomes **the vault directory itself** (the folder holding the `.md`
files), which is exactly what the agent's file tools operate on. `vault` is just its display name.

- Exposed mode: `root_dir = ~/obsidian/atom.<slug>`, `vault = atom.<slug>`.
- Isolated mode: `root_dir = $ATOM_HOME/notes/<slug>`, `vault = <slug>` (or the workflow override).

## 5. File-by-file plan

### Core: `src/atom/notes.py` (rewrite)
- Delete: `CLIRunner`, `_default_runner`, `_list_graph_names`, `VaultBusyError`, `_is_busy`,
  the `runner=` parameter, and all `subprocess`/`json`/`shutil.which` CLI plumbing.
- `resolve_obsidian_root(override)`: override → `$OBSIDIAN_VAULTS_DIR` → `~/obsidian`. (The env
  var points **directly** at the vaults home, unlike `$LOGSEQ_GRAPHS_DIR` which pointed at a
  `graphs/` subdir — no `.parent` hack.)
- `_atom_vault_name(workflow_name, override)` → `atom.<slug>` (unchanged logic).
- `NotesBinding(provider, root_dir, vault)`; `as_prompt_ctx()` → `{provider, root_dir, vault}`.
- `ensure_vault(...)`: pick root+name per exposed/isolated mode, then `root_dir.mkdir(parents=True,
  exist_ok=True)`. Inherently idempotent (no list-then-create). `provider` guard now rejects
  anything != `"obsidian"`. Signature: `ensure_vault(home, workflow_name, notes_cfg, *,
  expose_to_obsidian=False, obsidian_root_dir=None) -> NotesBinding`.
- `clear_vault(...)`: both modes become a **path-confined `rmtree`** of the vault directory,
  returning whether it existed. Preserve BOTH guards: (1) the resolved dir must be strictly under
  its base (`is_relative_to`, and `!= base`); (2) in exposed mode the vault name must start with
  `ATOM_VAULT_PREFIX` (belt-and-suspenders so a personal vault sharing `~/obsidian` is never
  touched). No `VaultBusyError` (Obsidian doesn't hard-lock; deleting an open vault's folder is
  safe). Signature drops `runner`; keeps `expose_to_obsidian`, `obsidian_root_dir`, `vault_override`.
- Module docstring + comments re-grounded on Obsidian.

### `src/atom/config/schema.py`
- `NotesRuntimeConfig`: `expose_to_obsidian: bool = False`, `obsidian_root_dir: Optional[str] = None`,
  comments rewritten. (Pydantic default stays `False`; shipped `config.yaml` turns it on.)

### `src/atom/workflow/schema.py`
- `NotesConfig`: `provider: Literal["obsidian"] = "obsidian"`, rename `graph` → `vault`, docstring
  updated. *(Breaking: any workflow YAML that set `notes.graph` must rename to `notes.vault`. The
  shipped `notes-smoke.yaml` doesn't set it.)*

### `src/atom/workflow/engine.py`
- `ensure_vault(...)` call: kwargs → `expose_to_obsidian=self.cfg.notes.expose_to_obsidian,
  obsidian_root_dir=self.cfg.notes.obsidian_root_dir`. (No other logic changes; the halt-on-
  notes-failure path is unchanged.)

### Prompts
- `src/atom/prompts/lead_system.md`: retitle `# Persistent notes (Obsidian)`. New body: the vault
  is a folder of markdown notes at `{{ notes.root_dir }}` (name `{{ notes.vault }}`); work with it
  using the file tools (`read_file`/`write_file`/`edit_file`/`ls`/`grep`/`glob`); notes are `.md`
  files, linked with `[[wikilinks]]`, metadata in YAML frontmatter; read prior runs first, record
  durable notes as you go. **Drop** `load_skill("logseq-cli")`.
- `src/atom/prompts/subagent_bash.md`: analogous Obsidian rewrite; drop the `logseq` CLI line.
  (Bash children can also use file tools; mention both `bash` and file tools over the vault dir.)

### Small source touch-ups
- `src/atom/subagent.py:68` — comment: `per-workflow Obsidian vault ctx (root_dir/vault)`.
- `src/atom/tools/search.py:85` — docstring example `"logseq-cli"` → a neutral example
  (`"curate-knowledge-base"`).
- `src/atom/cli.py` — Typer help + confirm text "Logseq vault" → "Obsidian vault"; kwargs renamed;
  `graph_override` → `vault_override` (reads `.notes.vault`); remove the `VaultBusyError` import and
  its `except` branch; keep the `has_active_runs` gate; wrap `clear_vault` in a generic OSError
  guard for filesystem errors.
- `src/atom/api/app.py` — same treatment for `DELETE /api/workflows/{name}/notes`: docstring,
  kwargs, `vault_override`, drop `VaultBusyError` handling (keep the 409 active-run guard).

### Skill: `skill_library/curate-knowledge-base/` (re-port)
- **Scripts** (restore the pure-stdlib file-walk versions from `c969e61`, adapted):
  - `scripts/_vault_ids.py` — restored verbatim (node-id/hidden/collect helpers; already atom-agnostic).
  - `scripts/find-disconnected-notes.py` — restored, **extended** to also emit `orphans` (no inbound)
    and `deadends` (no outbound) derived from a directed pass, and `unresolved` (wikilink targets
    that don't resolve), so the report shape matches what §3 Sense consumes today — replacing the
    fictional `obsidian orphans/deadends/unresolved` CLI. Takes the **vault directory path** as its
    arg (the `root_dir` atom passes), not `/mnt/user-data/<NAME>`.
  - `scripts/find-recently-modified-notes.py` — restored (`--since` mtime + `--since-git` modes).
  - **Delete** `scripts/find-disconnected-pages.py` (the Logseq/query-fed version).
- **SKILL.md** — rewrite for Obsidian, keeping the map-reduce methodology (Sense → Partition → Map →
  Reduce → Verify → Apply → Report → converge) intact. Substrate changes:
  - Unit of work is an **Obsidian vault**, named `vault=<NAME>` with `root_dir=<PATH>` (the vault
    directory). No `logseq` CLI anywhere.
  - §3 Sense: run `<skill_dir>/scripts/find-disconnected-notes.py <root_dir> --json` for
    islands/isolated/orphans/deadends/unresolved; derive tag/frontmatter stats with `grep`/file
    tools (no CLI). Incremental mode uses `find-recently-modified-notes.py`.
  - §5/§7/§11 workers read notes by reading the `.md` file directly (not `logseq show`); no block ids
    — the offending claim is located by quoted text / heading, and annotations attach inline after
    the claim (fallback: a `## Curator flags` section).
  - §8/§9 annotations become Obsidian **`> [!curator]` callouts** written directly into the `.md`
    file. `[[wikilinks]]` to the other note are **allowed** in callout content (plain markdown — no
    propagation). **Delete** the entire Logseq apparatus: schema bootstrap (`upsert tag/property`),
    `curator-*` properties, the "no wikilinks in flag content" warning, the random-ident-suffix
    warning, and the Datascript enumeration query. Idempotency: enumerate existing flags by grepping
    the vault for `[!curator]`, match by (host note, other note, type); prune a resolved flag by
    editing the file to remove that callout block.
  - `description:` frontmatter re-grounded on Obsidian.

### Config & workflows
- `config.yaml` — `notes:` block:
  ```yaml
  notes:
    expose_to_obsidian: true   # co-locate each workflow's vault at ~/obsidian/atom.<slug>/ so it's
                               # easy to open in Obsidian ("Open folder as vault" once, then remembered).
                               # false -> isolated vaults at ~/.atom/notes/<slug>/ (never surfaced).
    # obsidian_root_dir: ~/obsidian   # override only if your Obsidian vaults live elsewhere
  ```
- `workflows/notes-smoke.yaml` — rewrite both tasks to use file tools on the vault dir (no
  `load_skill("logseq-cli")`): "list every note already in this workflow's Obsidian vault …";
  "append a new dated note `{{ entry }} ({{ date }})` … confirm by listing again". Two-run
  recall/record semantics preserved.

### `README.md`
- Rewrite the persistent-notes section. **Remove** the "the `logseq` CLI must be installed"
  prerequisite entirely (net win: no external dependency — vaults are plain markdown). Document
  `expose_to_obsidian` (co-locate at `~/obsidian/atom.<slug>`, open once) and the notes-smoke flow.

### Tests (port all; keep the suite green)
- `tests/test_notes.py` (26) — rewrite filesystem-based (no fake CLI runner): isolated `ensure_vault`
  creates `$ATOM_HOME/notes/<slug>`; exposed creates `~/obsidian/atom.<slug>`; idempotent second
  call; binding fields (`provider="obsidian"`, `root_dir`, `vault`); `resolve_obsidian_root`
  override/env/default; `_atom_vault_name`; `clear_vault` isolated + exposed rmtree with both guards
  (refuse outside base; refuse non-`atom.` name); returns `False` when absent. Drop all
  `VaultBusyError` tests.
- `tests/test_config.py` (5) — `expose_to_obsidian` / `obsidian_root_dir` fields + defaults.
- `tests/test_workflow_schema.py` (1) — provider literal `"obsidian"`, `vault` field.
- `tests/test_prompts.py` (10) — notes ctx uses `vault` not `graph`, provider `"obsidian"`, the
  Obsidian notes block renders; rename the incidental `logseq-cli` example skill.
- `tests/test_search.py` (9) — rename the incidental `logseq-cli` fake skill to a neutral name.
- `tests/test_subagent.py` (4) — notes ctx `vault`; comment.
- `tests/test_workflow_engine.py` (4) — `ensure_vault` kwargs; provider.
- `tests/test_workflow_cli.py` (4) / `tests/test_workflow_api.py` (2) — Obsidian wording, kwargs,
  no `VaultBusyError`; `has_active_runs` gate retained.
- `tests/test_library.py` (4) — rename incidental `logseq` skill names.
- `tests/test_curate_disconnected_pages.py` → **rename** `tests/test_curate_disconnected_notes.py`:
  build a tmp vault of `.md` files with `[[wikilinks]]`, assert islands/isolated/orphans/deadends/
  main_component from `find-disconnected-notes.py`; add coverage for `find-recently-modified-notes.py`
  (`--since` mtime + `--since-git`).

## 6. Legacy data (clean cutover)

Existing Logseq graphs are **not** auto-migrated (the notes are per-workflow scratch memory, and
Logseq is being dropped). After the cutover:
- Exposed Logseq graphs under `~/logseq/atom.*` are orphaned; the user can remove them via the
  Logseq app or delete the folders.
- Isolated dirs under `$ATOM_HOME/notes/<slug>/` may contain Logseq artifacts; a fresh Obsidian run
  simply won't find markdown there. Safe to delete stale ones. Documented, not automated.

## 7. Non-goals / out of scope

- Auto-registration in `obsidian.json` (Option B) — deferred.
- Converting existing Logseq vault *content* into Obsidian markdown.
- Any `obsidian` CLI — none exists; file tools + the file-walk scripts are the whole surface.
- Expanding notes context to the general (non-bash) subagent — unchanged from today.

## 8. Verification plan

- `pytest` full suite green (baseline 565).
- Manual smoke: run `notes-smoke` twice; second run's Recall sees the first run's note. Confirm the
  vault materializes at `~/obsidian/atom.notes-smoke/` and opens in Obsidian.
- curate-kb: `find-disconnected-notes.py` / `find-recently-modified-notes.py` covered by unit tests;
  SKILL.md is reviewed prose (no runner).

## 9. Risks

- **`root_dir` semantic flip** (parent-home → vault-dir) touches the prompt contract and every notes
  test — the largest single source of churn; mitigated by the exact file map above.
- **Renamed config keys** (`expose_to_obsidian`, `obsidian_root_dir`) and the workflow field
  (`graph`→`vault`) are breaking for any hand-written config/YAML — acceptable for a clean cutover;
  called out in README.
- **curate-kb SKILL.md is prose**, not executable — correctness is by review, not tests. The
  executable risk (the scripts) is unit-tested.
