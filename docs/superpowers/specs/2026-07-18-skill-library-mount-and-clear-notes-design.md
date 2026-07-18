# Skill-library mount + clear-notes — design

Date: 2026-07-18
Status: approved (ready for implementation plan)

## Summary

Two independent improvements to the atom harness:

1. **`/mnt/skill_library` mount** — deferred skills live in `$ATOM_HOME/skill_library/<name>/`,
   but that directory is exposed to *no* virtual mount, so when a loaded skill's `SKILL.md`
   references a bundled file (a `reference.md`, a `scripts/extract.py`), the agent's file tools
   can't reach it. Expose `skill_library/` at a new virtual mount `/mnt/skill_library` (a sibling
   of the existing `/mnt/skills`), and tell the agent where a loaded skill's files live.

2. **Clear a workflow's Logseq vault** — a workflow's persistent notes vault
   (`$ATOM_HOME/notes/<workflow-slug>/`) is shared across every run and accumulates indefinitely;
   nothing resets it. Add a `clear_vault` core plus three surfaces (CLI, REST, UI button) to wipe
   one workflow's vault, guarded against wiping a vault while a run is live.

The two features share no code and can be implemented, reviewed, and merged independently, but are
specced together as one maintenance pass.

## Background: how the pieces work today

(Established by reading `sandbox/paths.py`, `sandbox/provider.py`, `sandbox/__init__.py`,
`agent.py`, `prompts/lead_system.md`, `prompts/render.py`, `tools/search.py`,
`middleware/skill_library.py`, `middleware/skill_activation.py`, `library.py`, `notes.py`,
`cli.py`, `api/app.py`, `workflow/run_store.py`, `atom-ui/src/api.ts`, `atom-ui/src/Workflows.tsx`.)

### Virtual filesystem / mounts

- The agent works in *virtual* paths; `ThreadPaths.virtual_map()` (`sandbox/paths.py`) maps each
  virtual mount prefix to a *physical* directory. Today there are four mounts:
  `VIRTUAL_WORKSPACE` → `workspace`, `VIRTUAL_UPLOADS` → `uploads`, `VIRTUAL_OUTPUTS` → `outputs`,
  `VIRTUAL_SKILLS` (`/mnt/skills`) → `skills` (i.e. `$ATOM_HOME/skills`).
- `ThreadPaths` already carries a resolved `skill_library` field (`$ATOM_HOME/skill_library`), and
  `ensure()` already `mkdir`s it — it is simply absent from `virtual_map()`.
- `LocalSandbox` (`sandbox/provider.py`) is constructed with `path_mappings = tp.virtual_map()`,
  sorted **longest-prefix-first** so a longer mount wins over a shorter one. `resolve()` walks the
  mounts, matches `p == prefix or p.startswith(prefix.rstrip("/") + "/")`, maps to that mount's
  physical root, and confines the realpath within that root (rejecting `..`/symlink escapes).
  `_rewrite_virtual()` (used only by `bash`) string-replaces each mount prefix with its physical
  path. **All of this is generic over the mount set** — adding a mapping needs no `provider.py`
  logic change.
- `path_mappings` / `virtual_map()` are consumed **only** in `sandbox/provider.py` plus two test
  assertions (`tests/test_sandbox.py`). No sandbox tool reads them directly.
- Prompt path-vars are injected in `agent.py:render_lead_system_prompt`, which builds a ctx dict
  `{"workspace": VIRTUAL_WORKSPACE, "uploads": ..., "outputs": ..., "skills": VIRTUAL_SKILLS, ...}`
  and renders `prompts/lead_system.md` (Jinja, `StrictUndefined` — every referenced var must be
  provided).

### Skills: always-on vs deferred

- Always-on skills: `$ATOM_HOME/skills/<name>/SKILL.md`, advertised up front as a name+description
  catalog.
- Deferred skills: `$ATOM_HOME/skill_library/<name>/SKILL.md`, discovered via
  `search_skills(query)` and loaded with `load_skill(name)` (`tools/search.py`). `load_skill`
  checks `(skills, skill_library)` for the `SKILL.md`, then records the name in the
  `promoted_skills` state channel.
- `SkillLibraryMiddleware` (`middleware/skill_library.py`) re-injects each promoted skill's
  `SKILL.md` **body** into every model call (transiently, so it survives compaction). Its
  `_bodies()` searches `(skill_library, skills)` per name. `SkillActivationMiddleware`
  (`middleware/skill_activation.py`) does the analogous transient injection for a `/skill-name`
  slash in the latest user message, searching `(skills, skill_library)`.
- **Neither the prompt nor the injected body ever tells the agent the filesystem path of a skill's
  bundled files.** That is the gap: even once `skill_library/` is mounted, the agent needs to be
  told the files are at `/mnt/skill_library/<name>/`.

### Persistent notes (Logseq vault)

- `notes.py`: a vault is `$ATOM_HOME/notes/<slug>/` where `slug = _slug(workflow_name)` (lowercased,
  non-alnum→`-`, fallback `"workflow"`). `notes_root(home, workflow_name)` returns that path.
  `ensure_vault(...)` is idempotent (list-then-create via the `logseq` CLI) and is called by the
  workflow engine when `workflow.notes.enabled`. The vault is keyed by workflow name and shared
  across **every run** of that workflow — long-term memory by design.
- Runs: `RunStore` (`workflow/run_store.py`) stores one dir per run; `_scan_summaries()` yields a
  `RunSummary` (with `.workflow` and `.status`) per run. `_ACTIVE = ("pending","queued","running")`
  is the module's set of non-terminal statuses.
- CLI: `atom` is a typer app (`cli.py`) with a `workflow` sub-typer (`workflow list|run|runs|
  export`). Notes config lives on the workflow manifest (`workflow.notes.enabled`,
  `workflow.notes.graph`).
- API: `api/app.py` exposes `/api/workflows` (list; each item currently `{name, description,
  inputs}`) and `/api/workflows/{name}`, plus run routes. Errors use `HTTPException`.
- UI: `atom-ui/src/api.ts` is the fetch client (`Workflow` interface = `{name, description,
  inputs}`); `Workflows.tsx` renders the workflow picker and `RunForm` (a page already scoped to a
  single `workflow.name` — the natural home for a per-workflow "Clear notes" action). atom-ui has
  **no test runner** — changes are verified by TypeScript typecheck / Vite build.

---

## Feature 1 — `/mnt/skill_library` mount

### 1.1 Add the mount

- `sandbox/paths.py`:
  - Add `VIRTUAL_SKILL_LIBRARY = "/mnt/skill_library"` next to the other `VIRTUAL_*` constants.
  - In `virtual_map()`, add `VIRTUAL_SKILL_LIBRARY: self.skill_library`.
  - No change to `ThreadPaths.ensure()` — it already creates `skill_library`.
- `sandbox/__init__.py`: import and re-export `VIRTUAL_SKILL_LIBRARY` (add to `__all__`).
- `sandbox/provider.py`: **no code change.** The generic mount loop handles the new mapping. The
  longest-prefix-first sort means `/mnt/skill_library` (longer) is tested before `/mnt/skills`, and
  `/mnt/skills` cannot false-match a `/mnt/skill_library/...` path because
  `"/mnt/skill_library/x".startswith("/mnt/skills/")` is `False`. Both facts are pinned by tests
  (§1.4).

### 1.2 Tell the agent where a loaded skill's files are

- `agent.py:render_lead_system_prompt`: add `"skill_library": VIRTUAL_SKILL_LIBRARY` to the ctx
  dict (import the constant alongside the existing `VIRTUAL_*` imports).
- `prompts/lead_system.md` — in the `# Workspace` list, add:
  - `` - `{{ skill_library }}` — bundled files for deferred skills you load. ``
  and a short clarifying line so the model knows the convention: a loaded skill's bundled files
  live under `` `{{ skills }}/<skill-name>/` `` for always-on skills and
  `` `{{ skill_library }}/<skill-name>/` `` for skills loaded via `load_skill`.
- `tools/search.py:load_skill`: the success `ToolMessage` currently says only "Loaded skill
  '<name>'. Follow its instructions...". Enrich it to name the mount: determine which base holds
  the skill (`skills/` → `/mnt/skills/<name>/`, else `skill_library/` → `/mnt/skill_library/<name>/`)
  using the existence check already performed there, and append: `Its bundled files are at
  <mount>/<name>/.`
- `middleware/skill_library.py`: `_bodies()` already knows which base dir (`skill_library` or
  `skills`) each `SKILL.md` came from. Return that base (or the derived virtual mount) alongside
  the body, and in `_inject()` prefix each skill's section with a line naming its files' location,
  e.g. `# Skill: <name> (bundled files: /mnt/skill_library/<name>/)`. This is the injection-time
  reinforcement that actually closes the "can't find bundled files" gap for loaded skills.
- `middleware/skill_activation.py`: mirror the same one-line location hint for slash-activated
  skills, for consistency. (Small; keeps the two injection paths symmetric.)

### 1.3 What is deliberately NOT changed

- No overlay/union resolution and no change to `resolve()` / `_rewrite_virtual` — Option B keeps
  each mount mapped to exactly one physical dir (decided with the user).
- Skills remain readable-and-writable through the sandbox exactly as `/mnt/skills` is today; no
  new read-only enforcement is introduced (out of scope).

### 1.4 Tests (Feature 1)

- `tests/test_sandbox.py`:
  - `tp.virtual_map()[VIRTUAL_SKILL_LIBRARY] == tp.skill_library`.
  - Write `skill_library/pdf-extract/reference.md` on disk; assert
    `LocalSandbox.read_text("/mnt/skill_library/pdf-extract/reference.md")` returns its contents.
  - `ls("/mnt/skill_library/pdf-extract")` lists `reference.md`.
  - Confinement: `read_text("/mnt/skill_library/../../etc/passwd")` raises `PathEscapeError`.
  - Distinct roots / no prefix bleed: a path under `/mnt/skills/...` resolves under `tp.skills` and
    a path under `/mnt/skill_library/...` under `tp.skill_library` (never crossed).
- `tests/test_prompts.py`: the rendered lead prompt contains the `skill_library` mount line (and
  still renders under `StrictUndefined`, i.e. the new var is always provided).
- `tests/test_search.py` (or `test_library.py`): `load_skill("pdf-extract")` success message names
  `/mnt/skill_library/pdf-extract/`.
- A middleware test asserting the injected body for a `skill_library` skill contains its
  `/mnt/skill_library/<name>/` location hint.

---

## Feature 2 — Clear a workflow's Logseq vault

### 2.1 Core

- `notes.py:clear_vault(home, workflow_name) -> bool`:
  - Compute `root = notes_root(home, workflow_name)`.
  - **Guard:** resolve `root` and assert it is strictly under `atom_home(home) / "notes"`
    (i.e. equal to or a descendant); raise `ValueError` otherwise. `_slug` already sanitizes the
    name, so this is defense-in-depth against any future caller.
  - If `root` exists, `shutil.rmtree(root)` and return `True`; else return `False`.
  - **Delete-only** — no re-provision. The next notes-enabled run re-creates the graph via the
    existing idempotent `ensure_vault`.
- `workflow/run_store.py:RunStore.has_active_runs(workflow_name) -> bool`:
  - `return any(s.workflow == workflow_name and s.status in _ACTIVE for s in self._scan_summaries())`.
  - This is the safety gate shared by CLI and API (wiping a vault mid-run would corrupt it).

### 2.2 CLI

- `cli.py`: add a `notes` sub-typer under the existing `workflow_app`, with one command:
  - `atom workflow notes clear <name>` — options `--yes/-y` (skip confirmation) and `--config/-c`.
  - Behavior: load config; if `RunStore(cfg.home).has_active_runs(name)` → print an error and
    `raise typer.Exit(1)` (the active-run guard is a **hard refuse**, independent of `--yes`).
    Otherwise, unless `--yes`, prompt for confirmation (`typer.confirm`); on confirm, call
    `clear_vault(cfg.home, name)` and print whether a vault was removed or none existed.

### 2.3 REST API

- `api/app.py`:
  - `DELETE /api/workflows/{name}/notes`:
    - `404` if the workflow doesn't exist on disk (reuse `load_workflow`/`resolve_workflow_path`
      pattern, matching `get_workflow`).
    - `409` (`HTTPException`) if `RunStore(cfg.home).has_active_runs(name)`.
    - else `clear_vault(cfg.home, name)` → `{"workflow": name, "cleared": bool}`.
  - `get_workflows`: add `notes_enabled: <wf>.notes.enabled` to each returned item so the UI can
    conditionally show the button.

### 2.4 UI

- `atom-ui/src/api.ts`:
  - Add `notes_enabled?: boolean` to the `Workflow` interface.
  - Add `clearNotes(name: string): Promise<{ workflow: string; cleared: boolean }>` doing
    `fetch("/api/workflows/" + encodeURIComponent(name) + "/notes", { method: "DELETE" })` with the
    same `{detail}`-surfacing error handling used by `cancel`/`selfImprove`.
- `atom-ui/src/Workflows.tsx` (`RunForm`): when `workflow.notes_enabled`, render a secondary
  "Clear notes" button that `window.confirm`s, calls `api.clearNotes(workflow.name)`, and shows a
  success/`cleared=false`/error message inline. Verify via `tsc`/`vite build`.

### 2.5 What is deliberately NOT built (YAGNI)

- No bulk "clear all vaults" operation.
- No content-only / partial clear — the whole `notes/<slug>/` dir is removed.
- No re-provision-on-clear, no undo/backup/trash.

### 2.6 Tests (Feature 2)

- `tests/test_notes.py`:
  - `clear_vault` removes an existing vault dir and returns `True`.
  - Returns `False` when the vault is absent (idempotent).
  - Raises `ValueError` if asked to operate on a path resolving outside `$ATOM_HOME/notes/`
    (guard).
- `tests/` for `RunStore.has_active_runs`: `True` when a `pending`/`queued`/`running` run of the
  workflow exists; `False` for terminal-only or a different workflow.
- API test (`tests/test_api*.py`): `DELETE /api/workflows/{name}/notes` → `200 {cleared}`; `409`
  when an active run exists; `404` for an unknown workflow; `get_workflows` includes
  `notes_enabled`.
- CLI test: `atom workflow notes clear <name> --yes` removes the vault; refuses (exit 1) when a run
  is active.

---

## Rollout

- Test-driven per superpowers TDD: red test → implement → green, one behavior at a time.
- Two independent branches (e.g. `feat/skill-library-mount`, `feat/clear-workflow-notes`), each its
  own PR/merge, so either can land without the other.
- Python changes verified with the existing pytest suite; atom-ui changes verified by typecheck /
  build (no test runner there).
