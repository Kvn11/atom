# Persistent notes for workflows (Logseq vault) — design

**Date:** 2026-07-09
**Status:** Approved (brainstorm complete), pending implementation plan
**Author:** atom / Kevin

## Summary

Add an opt-in **persistent notes** capability to atom workflows. When a workflow enables
notes, atom provisions a **Logseq vault (graph) keyed by workflow name** that is **shared
across every run** of that workflow, and it tells the workflow's agents — via a small
system-prompt snippet — that the vault exists and how to reach it. Agents use the `logseq`
CLI (documented by the existing `logseq-cli` skill) to read what earlier runs left and to
record durable notes/tasks for future runs.

Alongside this, promote the always-on skills folder to a first-class, **auto-discovered**
tier: every skill under `$ATOM_HOME/skills/` is injected into every agent's system prompt
(lead **and** sub-agents), regardless of whether it is listed in `skills.frequent`.

## Motivation

- Workflows are stateless across runs today: each run gets a fresh per-run workspace under
  `workflows/runs/<run_id>/workspace`. There is no memory that survives from one run to the
  next. Persistent notes give a workflow long-term memory.
- Logseq is the chosen notes substrate. Its CLI (`logseq`) is a **guaranteed prerequisite**
  on any device running this platform. A `logseq-cli` skill already exists at
  `$ATOM_HOME/skills/logseq-cli/SKILL.md`.
- The skills folder was previously only surfaced when a skill was named in
  `skills.frequent`. Making the folder auto-discovered matches its purpose (always-on) and
  guarantees the logseq guide is present wherever an agent might touch the vault.

## Goals

1. Per-workflow opt-in notes via workflow YAML config.
2. One persistent Logseq vault per workflow, reused across all runs (and shared by all tasks
   within a run).
3. Inject a concise vault snippet into the **workflow task (lead) system prompt** when notes
   are enabled: graph name, root-dir, and how to reach it.
4. Auto-discover every skill in `$ATOM_HOME/skills/` and inject it into **every** lead and
   sub-agent system prompt (independent of the notes feature).
5. A test workflow that exercises and debugs the feature end-to-end, provably persisting
   across runs.
6. Document the `logseq` CLI prerequisite in the README.

## Non-goals

- Notes for the single-agent CLI (`atom run`). The feature is **workflow-only**, matching the
  observability precedent. (The always-on skills change *does* reach `atom run`, since that is
  a general skills-tier change.)
- A new virtual filesystem mount for the vault. Not needed — see "How the agent reaches the
  vault".
- Concurrency control around vault writes. Logseq handles concurrent writes; atom does not
  serialize them.
- Sync / multi-device Logseq sync, e2ee, remote graphs. Out of scope.

## Background: relevant current architecture

- **Lead agent assembly** (`src/atom/agent.py`): `build_lead_agent` →
  `render_lead_system_prompt` renders `prompts/lead_system.md`. It already has a
  `# Skills (always available)` block fed by `frequent_skills`, which today come *only* from
  `load_named_skills(home, profile.skills.frequent)`.
- **Skill loading** (`src/atom/library.py`): `load_skill_entries(dir)` already loads *all*
  `SKILL.md` under a directory; `load_named_skills(home, names)` loads specific ones by name
  (searching `skills/` then `skill_library/`).
- **Sub-agent assembly** (`src/atom/subagent.py`): `SubagentRunner._child_system` renders
  `prompts/subagent_general.md` / `subagent_bash.md`. These templates have **no** skills block
  and the render ctx does **not** include `frequent_skills` today.
- **Workflow engine** (`src/atom/workflow/engine.py`): each task calls
  `run_agent(workspace=<per-run dir>, thread_id=..., trace=..., ...)`. The workspace is
  per-run; anything shared across runs must live outside it.
- **Bash is not filesystem-confined** (`src/atom/sandbox/provider.py`): only the file tools
  are confined to virtual mounts; `bash` runs with `cwd=workspace` but can reference absolute
  paths. `_scrubbed_env` drops secret-looking vars but keeps `PATH` (so `logseq` at
  `~/.local/bin` resolves) and would keep a non-secret var like `LOGSEQ_CLI_ROOT_DIR`.
- **Logseq CLI**: `logseq graph create --graph <name> --root-dir <path>` creates a graph
  (vault) under a root-dir (default `~/logseq`); `logseq graph list --root-dir <path>` lists
  them. `--graph` + `--root-dir` select the target on every command.

## Design

### Component 1 — Always-on skill auto-discovery (general)

**Change:** the `skills/` folder becomes the auto-discovered always-on tier.

- `src/atom/library.py`: add
  `load_always_on_skills(home, extra_names: list[str]) -> list[SkillEntry]`:
  1. `skills = load_skill_entries(Path(home)/"skills")` — all always-on skills, sorted by dir
     name (deterministic).
  2. Append any `load_named_skills(home, extra_names)` entry whose `name` is not already
     present (dedupe by name). This preserves `skills.frequent` as a way to pull specific
     `skill_library/` skills into always-on, without duplicating a skill that is already in
     `skills/`.
- `src/atom/agent.py` `build_lead_agent`: replace
  `frequent_skills = load_named_skills(home, profile.skills.frequent)` with
  `frequent_skills = load_always_on_skills(home, profile.skills.frequent)`.
  The `lead_system.md` `# Skills (always available)` block already renders these — **no
  template change** for the lead.
- **Sub-agents** (`src/atom/subagent.py`): thread the same merged `frequent_skills` list into
  `SubagentRunner` so children get an identical always-on set (parity with the lead, and no
  re-loading from disk):
  - `build_lead_agent` passes `frequent_skills` into `_build_middlewares`, which passes it to
    `SubagentRunner(frequent_skills=frequent_skills)`.
  - `SubagentRunner` gains a `frequent_skills: list = field(default_factory=list)` field.
  - `_child_system` adds `"frequent_skills": [{"name": s.name, "body": s.body} for s in
    self.frequent_skills]` to the render ctx (always present, possibly empty).
  - Add a `{% if frequent_skills %} # Skills (always available) … {% endif %}` block to
    **both** `subagent_general.md` and `subagent_bash.md`, mirroring `lead_system.md`. Because
    the render uses `StrictUndefined`, the ctx must always pass `frequent_skills` once the
    template references it.

**Effect:** with `logseq-cli` present in `skills/`, its body is now injected into every lead
and sub-agent system prompt. Tests use temp homes with an empty `skills/`, so existing tests
see no behavioral change.

### Component 2 — Notes config on the workflow schema

`src/atom/workflow/schema.py`: add a `NotesConfig` block to `WorkflowDef`.

```python
class NotesConfig(_Base):
    enabled: bool = False
    provider: Literal["logseq"] = "logseq"
    graph: Optional[str] = None   # default: slug of the workflow name
```

`WorkflowDef` gains `notes: NotesConfig = Field(default_factory=NotesConfig)`. YAML:

```yaml
notes:
  enabled: true
  provider: logseq     # optional; only provider today
  graph: my-graph      # optional; defaults to slugified workflow name
```

A small `_slug(name)` helper (lowercase, non-alphanumerics → `-`, collapse/strip dashes)
produces filesystem- and graph-safe identifiers.

### Component 3 — Vault lifecycle: new module `src/atom/notes.py`

Neutral top-level module (no dependency on the `workflow` package, so `agent.py`/`runtime.py`
can share types without an import cycle).

- `NotesBinding` dataclass: `provider: str`, `root_dir: str` (absolute), `graph: str`.
  `as_prompt_ctx()` → `{"provider", "root_dir", "graph"}` dict for the Jinja ctx.
- `notes_root(home, workflow_name) -> Path` = `atom_home(home)/"notes"/_slug(workflow_name)`.
- `ensure_vault(home, workflow_name, notes_cfg, *, runner=subprocess-based) -> NotesBinding`:
  - `root = notes_root(home, workflow_name)`; `graph = notes_cfg.graph or _slug(workflow_name)`.
  - `root.mkdir(parents=True, exist_ok=True)`.
  - `logseq graph list --root-dir <root> --output json` → parse; if `graph` absent, run
    `logseq graph create --graph <graph> --root-dir <root>`. Idempotent: a graph that already
    exists (from a prior run) is reused.
  - The command runner is an **injectable callable** (default runs `logseq` via `subprocess`)
    so unit tests substitute a fake and need no real `logseq`. Raise a clear error if the
    `logseq` binary is missing (prerequisite) — but only when notes are enabled.
  - Tolerate the known stderr process-scan warning: treat success as "graph present in a
    subsequent `graph list`" or a zero return code, per the skill's guidance.
- `only for provider == "logseq"` today; a different provider raises `NotImplementedError`.

### Component 4 — Engine wiring

`src/atom/workflow/engine.py`:

- In `execute(run_id)`, before the step loop: if `workflow.notes.enabled`, call
  `ensure_vault(self.cfg.home, workflow.name, workflow.notes)` **once** and hold the resulting
  `NotesBinding` for the run. On failure, halt the run with a clear error (same terminal-state
  discipline as other failures).
- In `_run_task`, pass the binding down: `run_agent(..., notes=binding.as_prompt_ctx() if
  binding else None)`. A plain dict crosses the boundary (no cross-package type coupling).

### Component 5 — Threading notes into the prompt

- `src/atom/runtime.py` `run_agent(..., notes: dict | None = None)` → forwards to
  `build_lead_agent(..., notes=notes)`.
- `src/atom/agent.py` `build_lead_agent(..., notes=None)` → forwards to
  `render_lead_system_prompt(..., notes=notes)`, which sets `ctx["notes"] = notes` (dict or
  `None`).
- `prompts/lead_system.md`: add a gated block (placed after Workspace, before How-to-work):

  ```jinja
  {% if notes %}
  # Persistent notes (Logseq)
  A Logseq vault persists across every run of this workflow — treat it as long-term memory.
  Graph `{{ notes.graph }}` lives at root-dir `{{ notes.root_dir }}`. Reach it with the logseq
  CLI: `logseq --root-dir {{ notes.root_dir }} --graph {{ notes.graph }} <command>` (the
  logseq-cli skill above documents the commands). Before you start, read what earlier runs
  left; as you work, record durable notes and tasks there so future runs can build on them.
  {% endif %}
  ```

- **Sub-agents** do not receive the notes snippet directly (notes is lead/workflow-scoped).
  A lead that delegates vault work includes the graph/root-dir in the `delegate_task` prompt;
  the sub-agent already has the `logseq-cli` skill (now always-on) to interpret it.
- Observability: the added snippet + always-on skill bodies change the system prompt, so
  `system_prompt_sha` shifts accordingly — desirable for the eval phase; no special handling.

### Component 6 — README prerequisites

Add a **Prerequisites** subsection noting that the `logseq` CLI must be installed and on
`PATH` for persistent-notes workflows (guaranteed on target devices), plus a short pointer to
the `notes:` workflow block.

### Component 7 — Test workflow `workflows/notes-smoke.yaml`

Notes-enabled workflow that proves cross-run persistence.

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
          List every task already recorded in the persistent Logseq vault for this
          workflow and report how many exist and what they say. If none exist yet, say so.
        model: haiku
        thinking: low
  - title: Record
    tasks:
      - id: record
        prompt: >
          Append a new dated task to the persistent Logseq vault with this content:
          "{{ entry }} ({{ date }})". Then confirm it was written by listing tasks again,
          and present a short confirmation file under {{ outputs }}.
        model: haiku
        thinking: low
```

- Single-task steps keep the debug trace clean (concurrency is fine, but not the point here).
- **Persistence proof:** run it twice. Run 1's Recall finds nothing; Run 2's Recall reports
  Run 1's entry. The physical vault at `$ATOM_HOME/notes/notes-smoke/` persists between runs.
- Copied to `$ATOM_HOME/workflows/` for live runs (as with `parallel-poems.yaml`).

## Testing

### Unit / integration (pytest — `.venv/bin/python -m pytest`)

- **Schema:** `notes` defaults to `enabled=False`; a YAML `notes:` block parses; `provider`
  defaults to `logseq`; `graph` optional. (`test_workflow_schema.py`.)
- **Always-on skills (lead):** with a skill written into a temp `home/skills/<name>/SKILL.md`
  and `skills.frequent=[]`, the rendered lead system prompt contains that skill body.
  (`test_prompts.py` or `test_library.py` for `load_always_on_skills` dedupe.)
- **Always-on skills (sub-agent):** `SubagentRunner._child_system` includes the skill body
  when `frequent_skills` is set; empty list renders no skills block. (`test_subagent.py`.)
- **Notes snippet:** `render_lead_system_prompt(..., notes={...})` includes the graph name and
  root-dir; `notes=None` omits the block. (`test_prompts.py`.)
- **Vault ensure:** `ensure_vault` with an injected fake runner creates the graph only when
  absent and returns the expected binding (no real `logseq`). (`test_notes.py` — new.)
- **Engine wiring:** with notes enabled and a stubbed `run_agent`/`ensure_vault`, the engine
  calls `ensure_vault` once and forwards the binding dict to each task. (`test_workflow_engine.py`.)

### Live end-to-end (debug)

- Copy `notes-smoke.yaml` into `$ATOM_HOME/workflows/`.
- `atom workflow run notes-smoke` twice.
- Verify: run 1 Recall empty → Record writes; `$ATOM_HOME/notes/notes-smoke/` exists with the
  graph; run 2 Recall reports run 1's entry. Inspect with
  `logseq list task --root-dir $ATOM_HOME/notes/notes-smoke --graph notes-smoke`.
- Debug prompt/CLI-usage issues by reading the per-run chat transcripts under
  `workflows/runs/<run_id>/chats/`.

## Risks & mitigations

- **Prompt weight:** always-on skills now load into every prompt (incl. `atom run` and every
  sub-agent). `logseq-cli` is ~7 KB. Accepted per explicit decision. Mitigation lever exists
  later (a config flag) but is out of scope now (YAGNI).
- **`logseq` missing at runtime:** only fails when notes are enabled; `ensure_vault` raises a
  clear, actionable error. README documents the prerequisite.
- **Graph-create stderr warnings in sandboxed envs:** tolerated by verifying presence via a
  follow-up `graph list` / zero return code rather than treating any stderr as failure.
- **Concurrent writes:** none added by atom; Logseq handles concurrency (per decision).

## Rollout / files touched

- New: `src/atom/notes.py`, `workflows/notes-smoke.yaml`, `tests/test_notes.py`.
- Changed: `src/atom/library.py` (`load_always_on_skills`), `src/atom/agent.py`
  (skills loader + `notes` param + render ctx), `src/atom/runtime.py` (`notes` param),
  `src/atom/subagent.py` (`frequent_skills` field + child ctx), `src/atom/workflow/schema.py`
  (`NotesConfig`), `src/atom/workflow/engine.py` (ensure + forward), `prompts/lead_system.md`
  (notes block), `prompts/subagent_general.md` + `prompts/subagent_bash.md` (skills block),
  `README.md` (prerequisites + notes block), relevant tests.
```
