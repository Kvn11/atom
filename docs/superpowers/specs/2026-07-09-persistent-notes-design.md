# Persistent notes for workflows (Logseq vault) + skill catalog/load refactor — design

**Date:** 2026-07-09
**Status:** Approved (brainstorm complete), pending implementation plan
**Author:** atom / Kevin

## Summary

Two coupled changes:

1. **Persistent notes (workflow-only).** An opt-in per-workflow **Logseq vault** keyed by
   workflow name, **shared across every run** of that workflow. When enabled, a small
   system-prompt snippet tells the workflow's lead agent the vault exists and how to reach it.
   Agents use the `logseq` CLI (via the `logseq-cli` skill) to read what earlier runs left and
   record durable notes/tasks for future runs.

2. **Skill catalog + explicit load (general).** Rework skill surfacing to keep prompts lean:
   - Auto-discover every skill in `$ATOM_HOME/skills/` and inject only its **lightweight
     frontmatter (name + description)** as an always-on **catalog** — in both lead and
     sub-agent prompts. Not the full body.
   - `search_skills(query)` becomes **discovery-only**: it returns the name+description of
     matching `skill_library/` skills. It no longer injects bodies.
   - A new **`load_skill(name)`** tool is the single way to pull a skill's full body into
     context (on demand, by exact name). Available to **lead and sub-agents**.

## Motivation

- Workflows are stateless across runs today (fresh per-run workspace under
  `workflows/runs/<run_id>/workspace`). Persistent notes give a workflow long-term memory.
- Logseq is the notes substrate; its CLI (`logseq`) is a **guaranteed prerequisite** on any
  device running this platform. A `logseq-cli` skill already exists at
  `$ATOM_HOME/skills/logseq-cli/SKILL.md`.
- Injecting full skill bodies up front is expensive (`logseq-cli` is ~7 KB). A name+description
  catalog plus explicit `load_skill` keeps base prompts small and prompt-cache-stable while
  still making every skill discoverable and loadable — including by sub-agents.
- Overloading `search_skills` to both find *and* load is unintuitive; splitting discovery
  (`search_skills`) from loading (`load_skill`) makes the model obvious.

## Goals

1. Per-workflow opt-in notes via workflow YAML config.
2. One persistent Logseq vault per workflow, reused across all runs (and shared by all tasks in
   a run).
3. Inject a concise vault snippet into the **workflow task (lead) system prompt** when notes
   are enabled: graph, root-dir, how to reach it, and a hint to `load_skill("logseq-cli")`.
4. Auto-discover `$ATOM_HOME/skills/` and inject a name+description **catalog** into **every**
   lead and sub-agent system prompt.
5. `search_skills` returns lightweight frontmatter only (discovery); `load_skill(name)` is the
   only loader; both leads and sub-agents can `load_skill`.
6. A test workflow that exercises and debugs notes end-to-end, provably persisting across runs.
7. Document the `logseq` CLI prerequisite in the README.

## Non-goals

- Notes for the single-agent CLI (`atom run`). Notes are **workflow-only** (matches the
  observability precedent). The skill catalog/load change *is* general and reaches `atom run`.
- A new virtual filesystem mount for the vault (not needed — bash is unconfined; see below).
- Concurrency control around vault writes (Logseq handles concurrency).
- `search_skills` for sub-agents. Sub-agents get the always-on catalog + `load_skill` (they can
  load anything by name); fuzzy `skill_library/` discovery stays lead-only to keep the
  sub-agent surface small. (Easy to extend later.)
- Sync / e2ee / remote graphs.

## Background: relevant current architecture

- **Lead assembly** (`src/atom/agent.py`): `build_lead_agent` → `render_lead_system_prompt`
  renders `prompts/lead_system.md`, which has a `# Skills (always available)` block fed by
  `frequent_skills` = `load_named_skills(home, profile.skills.frequent)` (currently full body).
  Always-on tools `search_tools`/`search_skills` are appended in `build_lead_agent` and listed
  in the prompt's `extras`.
- **Skill loading** (`src/atom/library.py`): `load_skill_entries(dir)` loads all `SKILL.md`
  under a dir; `load_named_skills(home, names)` loads specific ones (searching `skills/` then
  `skill_library/`). `SkillEntry` carries `name`, `description`, `keywords`, `tier`, `body`.
  The library index (`search_skills`) is built over `skill_library/` **only**.
- **`search_skills` today** (`src/atom/tools/search.py`): BM25-matches `skill_library/`,
  records names in `state.promoted_skills`, returns a short confirmation.
- **`SkillLibraryMiddleware`** (`src/atom/middleware/skill_library.py`): each model call,
  injects the bodies of `state.promoted_skills` transiently. Its `_bodies()` already resolves
  from **both** `skill_library/` and `skills/`. `promoted_skills` is a reducer-merged list
  channel on `ThreadState` (`merge_name_list`).
- **`SkillActivationMiddleware`**: injects a body when the newest human message starts with
  `/<skill-name>` (user-driven; unchanged, kept).
- **Sub-agent assembly** (`src/atom/subagent.py`): `SubagentRunner._child_system` renders
  `subagent_general.md` / `subagent_bash.md` (no skills block, no `frequent_skills` in ctx).
  `_child_tools` = filesystem (+ bash for the bash type); `_child_middleware` is minimal;
  children use `ThreadState` (so `promoted_skills` exists) and carry `home` in context.
- **Workflow engine** (`src/atom/workflow/engine.py`): each task calls
  `run_agent(workspace=<per-run dir>, thread_id=..., trace=..., ...)`. Workspace is per-run.
- **Bash is not filesystem-confined** (`src/atom/sandbox/provider.py`): only file tools are
  confined; `bash` runs with `cwd=workspace` but can reference absolute paths. `_scrubbed_env`
  keeps `PATH` (so `logseq` at `~/.local/bin` resolves).
- **Logseq CLI**: `logseq graph create --graph <name> --root-dir <path>` creates a vault;
  `logseq graph list --root-dir <path>` lists graphs; `--graph`/`--root-dir` target commands.

## Design

### Component 1 — Skill catalog + `load_skill` (general refactor)

**Two tiers:**
- `skills/` = **always-on catalog** tier: auto-discovered, advertised by name+description
  in every prompt, loaded on demand.
- `skill_library/` = **deferred** tier: discovered by `search_skills` (name+description),
  loaded on demand.

**Catalog builder** — `src/atom/library.py`:
`load_skill_catalog(home, extra_names) -> list[SkillEntry]`:
1. `entries = load_skill_entries(Path(home)/"skills")` (deterministic, sorted by dir name).
2. Append each `load_named_skills(home, extra_names)` entry whose `name` is not already present
   (so `skills.frequent` can advertise specific `skill_library/` skills in the always-on
   catalog too; deduped by name).
Only `name` + `description` are ever rendered from these.

**`search_skills(query, max_results)` — discovery-only** (`src/atom/tools/search.py`):
- BM25-match `skill_library/` via the index (unchanged search).
- Return a **name + description listing** of matches. **Do not** touch `promoted_skills`, do
  not inject bodies. The message instructs the agent to `load_skill("<name>")` to load one.

**`load_skill(name)` — new tool** (`src/atom/tools/search.py`, `verb_noun`):
- Sanitize `name` (reject path separators / `..`).
- Resolve on disk: `skills/<name>/SKILL.md` then `skill_library/<name>/SKILL.md`. If absent,
  return a clear error (suggest the catalog / `search_skills`).
- Return a `Command` that merges `name` into `promoted_skills` (reuse `merge_name_list`) and a
  short confirmation. `SkillLibraryMiddleware` then injects the full body transiently — **no
  middleware change needed** (it already resolves `skills/`).

**Lead wiring** (`src/atom/agent.py`):
- `skill_catalog = load_skill_catalog(home, profile.skills.frequent)`.
- `render_lead_system_prompt` passes `skill_catalog=[{"name","description"}, …]` (not bodies).
- Append `load_skill` to the tool set and `extras`/`tool_names` whenever any skill is loadable
  (`skill_catalog` non-empty **or** `library.has_skills`). Keep `search_skills` gated on
  `library.has_skills` (deferred tier present).
- Gate `SkillLibraryMiddleware` on "any skills present" (`skill_catalog` non-empty **or**
  `library.has_skills`), since bodies can now be loaded from `skills/` even with an empty
  `skill_library/`.

**Prompt template** (`prompts/lead_system.md`): replace the full-body block with a catalog:
```jinja
{% if skill_catalog %}
# Skills (load before use)
These skills are available. Each lists a name and what it's for. Before using one, load its
full instructions with `load_skill("<name>")`.
{% for s in skill_catalog %}
- **{{ s.name }}** — {{ s.description }}
{% endfor %}{% endif %}
```

**Sub-agent wiring** (`src/atom/subagent.py`):
- Pass the same `skill_catalog` into `SubagentRunner` (a `skill_catalog: list = []` field).
- `_child_system` adds `skill_catalog=[{name,description}…]` to the render ctx (always present).
- Add the same catalog block to `subagent_general.md` and `subagent_bash.md` (with a
  `load_skill` hint). Because rendering uses `StrictUndefined`, ctx must always pass it.
- `_child_tools`: append `load_skill` (both sub-agent types). **Not** `search_skills`.
- `_child_middleware`: append `SkillLibraryMiddleware(home)` so a child's `load_skill` body
  injects. (Children already use `ThreadState`, so `promoted_skills` exists.)

**Config:** `skills.frequent` is retained but re-interpreted as "extra skills to advertise in
the always-on catalog" (name+description, load on demand) — no longer full-body injection.
Default `[]` → no change for existing configs.

### Component 2 — Notes config on the workflow schema

`src/atom/workflow/schema.py`: add `NotesConfig` and a `notes` field on `WorkflowDef`.

```python
class NotesConfig(_Base):
    enabled: bool = False
    provider: Literal["logseq"] = "logseq"
    graph: Optional[str] = None   # default: slug of the workflow name
```
`notes: NotesConfig = Field(default_factory=NotesConfig)`. A `_slug(name)` helper (lowercase,
non-alphanumerics → `-`, collapse/strip) yields filesystem- and graph-safe ids.

### Component 3 — Vault lifecycle: new module `src/atom/notes.py`

Neutral top-level module (no `workflow` package dependency → no import cycle for
`agent.py`/`runtime.py`).

- `NotesBinding` dataclass: `provider`, `root_dir` (abs str), `graph`; `as_prompt_ctx()` →
  `{"provider","root_dir","graph"}`.
- `notes_root(home, workflow_name)` = `atom_home(home)/"notes"/_slug(workflow_name)`.
- `ensure_vault(home, workflow_name, notes_cfg, *, runner=<subprocess default>) -> NotesBinding`:
  - `root = notes_root(...)`; `graph = notes_cfg.graph or _slug(workflow_name)`; `root.mkdir`.
  - `logseq graph list --root-dir <root> --output json`; if `graph` absent →
    `logseq graph create --graph <graph> --root-dir <root>`. Idempotent (reuse across runs).
  - Injectable command `runner` (default: real `logseq` via subprocess) so unit tests use a
    fake — no real `logseq` needed. Raise a clear error if `logseq` is missing (only when notes
    enabled). Tolerate the known stderr process-scan warning (verify via a follow-up
    `graph list` / zero return code).
  - `provider != "logseq"` → `NotImplementedError`.

### Component 4 — Engine wiring

`src/atom/workflow/engine.py`:
- In `execute(run_id)`, before the step loop: if `workflow.notes.enabled`, call
  `ensure_vault(cfg.home, workflow.name, workflow.notes)` **once**; hold the `NotesBinding`.
  On failure, halt the run with a clear error (same terminal-state discipline).
- In `_run_task`, pass `notes=binding.as_prompt_ctx() if binding else None` into `run_agent`.

### Component 5 — Threading notes into the prompt

- `src/atom/runtime.py` `run_agent(..., notes: dict | None = None)` → forwards to
  `build_lead_agent(..., notes=notes)`.
- `src/atom/agent.py` `build_lead_agent(..., notes=None)` → `render_lead_system_prompt(...,
  notes=notes)` sets `ctx["notes"]` (dict or `None`).
- `prompts/lead_system.md` — gated block (after Workspace, before How-to-work):
  ```jinja
  {% if notes %}
  # Persistent notes (Logseq)
  A Logseq vault persists across every run of this workflow — treat it as long-term memory.
  Graph `{{ notes.graph }}` lives at root-dir `{{ notes.root_dir }}`. Reach it with the logseq
  CLI: `logseq --root-dir {{ notes.root_dir }} --graph {{ notes.graph }} <command>`. Load the
  `logseq-cli` skill (`load_skill("logseq-cli")`) for command details. Before you start, read
  what earlier runs left; as you work, record durable notes/tasks there for future runs.
  {% endif %}
  ```
- Sub-agents don't receive the notes snippet (notes is lead/workflow-scoped); a delegating lead
  passes graph/root-dir in the `delegate_task` prompt, and the sub-agent can
  `load_skill("logseq-cli")` for command details.
- Observability: the snippet + catalog change `system_prompt_sha` — desirable; no special code.

### Component 6 — README prerequisites

Add a **Prerequisites** subsection: the `logseq` CLI must be installed and on `PATH` for
persistent-notes workflows (guaranteed on target devices); point to the `notes:` block.

### Component 7 — Test workflow `workflows/notes-smoke.yaml`

```yaml
name: notes-smoke
description: Smoke-test persistent notes — recall prior entries, then record a new one.
notes:
  enabled: true
inputs:
  - name: entry
    required: false
    default: "hello from a notes-smoke run"
steps:
  - title: Recall
    tasks:
      - id: recall
        prompt: >
          Load the logseq-cli skill, then list every task already recorded in this workflow's
          persistent Logseq vault and report how many exist and what they say. If none exist
          yet, say so plainly.
        model: haiku
        thinking: low
  - title: Record
    tasks:
      - id: record
        prompt: >
          Load the logseq-cli skill, then append a new dated task to this workflow's persistent
          Logseq vault with content "{{ entry }} ({{ date }})". Confirm by listing tasks again,
          and present a short confirmation file under {{ outputs }}.
        model: haiku
        thinking: low
```
- Single-task steps keep the debug trace clean. **Persistence proof:** run twice — run 1's
  Recall finds nothing; run 2's Recall reports run 1's entry. Vault persists at
  `$ATOM_HOME/notes/notes-smoke/`. Copied to `$ATOM_HOME/workflows/` for live runs.

## Testing

### Unit / integration (`.venv/bin/python -m pytest`)

- **Catalog builder:** `load_skill_catalog` returns `skills/` entries + deduped
  `skills.frequent` extras. (`test_library.py`.)
- **Lead prompt:** with a temp `home/skills/<name>/SKILL.md` and `skills.frequent=[]`, the
  rendered lead prompt contains the skill **name + description** but **not** its body; includes
  `load_skill` in the tool list. (`test_prompts.py`.)
- **`search_skills`:** returns name+description of `skill_library/` matches and does **not**
  mutate `promoted_skills`. (`test_search`/`test_library`.)
- **`load_skill`:** valid name → `Command` merging `promoted_skills`; unknown/traversal name →
  error, no mutation. (New `test_search`/tool test.)
- **Body injection unchanged:** a name in `promoted_skills` still injects via
  `SkillLibraryMiddleware` from `skills/`. (`test_middleware`/existing.)
- **Sub-agent:** `_child_system` includes the catalog; `_child_tools` includes `load_skill`;
  `_child_middleware` includes `SkillLibraryMiddleware`. (`test_subagent.py`.)
- **Notes schema:** `notes` defaults `enabled=False`; a `notes:` block parses; `provider`
  defaults `logseq`; `graph` optional. (`test_workflow_schema.py`.)
- **Notes snippet:** `render_lead_system_prompt(..., notes={...})` includes graph + root-dir;
  `notes=None` omits it. (`test_prompts.py`.)
- **Vault ensure:** `ensure_vault` with an injected fake runner creates only when absent and
  returns the expected binding. (New `test_notes.py`.)
- **Engine wiring:** notes enabled → `ensure_vault` called once, binding dict forwarded to each
  task (stubbed `run_agent`). (`test_workflow_engine.py`.)

### Live end-to-end (debug)

- Copy `notes-smoke.yaml` into `$ATOM_HOME/workflows/`; `atom workflow run notes-smoke` twice.
- Verify: run 1 Recall empty → Record writes; `$ATOM_HOME/notes/notes-smoke/` holds the graph;
  run 2 Recall reports run 1's entry. Inspect:
  `logseq list task --root-dir $ATOM_HOME/notes/notes-smoke --graph notes-smoke`.
- Debug via per-run chat transcripts under `workflows/runs/<run_id>/chats/`.

## Risks & mitigations

- **`logseq` missing at runtime:** fails only when notes enabled; `ensure_vault` raises a
  clear, actionable error; README documents the prerequisite.
- **Graph-create stderr warnings in sandboxed envs:** tolerated by verifying presence via a
  follow-up `graph list` / zero return code.
- **Agent forgets to `load_skill` before using a skill:** the catalog line and (for notes) the
  snippet both instruct loading first; the `logseq-cli` skill body, once loaded, is re-injected
  every turn (survives compaction) via `SkillLibraryMiddleware`.
- **Behavior change to `search_skills`:** it no longer loads. Callers/tests updated; the split
  is the intended, clearer model.
- **Prompt weight:** now minimal — only name+description per skill up front.

## Rollout / files touched

- **New:** `src/atom/notes.py`, `workflows/notes-smoke.yaml`, `tests/test_notes.py`.
- **Changed:** `src/atom/library.py` (`load_skill_catalog`), `src/atom/tools/search.py`
  (`search_skills` discovery-only + new `load_skill`), `src/atom/agent.py` (catalog + tool +
  middleware gating + `notes` param + ctx), `src/atom/runtime.py` (`notes` param),
  `src/atom/subagent.py` (`skill_catalog` field, ctx, `load_skill` tool, `SkillLibraryMiddleware`),
  `src/atom/workflow/schema.py` (`NotesConfig`), `src/atom/workflow/engine.py` (ensure +
  forward), `prompts/lead_system.md` (catalog + notes block),
  `prompts/subagent_general.md` + `prompts/subagent_bash.md` (catalog block), `README.md`,
  and the tests listed above.
```
