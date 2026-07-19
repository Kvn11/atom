# Expose workflow vaults to the Logseq desktop app (auto-view) — design

**Date:** 2026-07-19
**Status:** Approved (brainstorm complete), pending implementation plan — three decisions confirmed 2026-07-19 (see **Decisions**)
**Author:** atom / Kevin

## Summary

Today a workflow's persistent Logseq vault lives in an atom-managed, per-workflow location
(`$ATOM_HOME/notes/<slug>/graphs/<slug>/db.sqlite`) that the user's Logseq **desktop app never
sees**. To look at a vault, a user must manually get Logseq to open that specific graph — an
undocumented, by-hand step.

This feature makes a workflow's vault **automatically present in the user's Logseq graph
switcher**, with no per-view export step, by **co-locating** the vault inside the desktop app's
own graph home (default `~/logseq/graphs/`) under a namespaced graph name (`atom.<slug>`). Because
atom's `logseq` CLI *is* the same desktop app binary, writing into the app's own graph home means
a workflow run and an open GUI **share one `db-worker-node`** — so co-location is simultaneously
the *visibility* mechanism and the *concurrency-safety* mechanism.

## Motivation

- Vaults are useful to the user, not just the agent. A workflow accumulates durable notes/tasks
  across runs; the user should be able to browse them in Logseq without ceremony.
- The current handoff is a manual, undocumented dance (point the app at a non-default graph home).
  There is **no export feature to remove** — the `atom workflow export` command is unrelated
  (it dumps run-trace JSON). This is net-new plumbing.
- The enabling fact: atom runs as a local host process on the **same machine and filesystem** as
  the user's Logseq app, and the `logseq` CLI atom shells out to is literally
  `/Applications/Logseq.app` run in Node mode (desktop v2.0.1, DB-graph generation). Substrate and
  GUI are one install.

## Goals

1. A workflow's vault appears in the Logseq desktop switcher **without any manual export/open
   step** — provisioned directly into the app's graph home as `atom.<slug>`.
2. Runs may write to a vault while the user has it open in Logseq, **safely** (no `db.sqlite`
   corruption), with updates reflected live in the GUI.
3. `clear notes` still works — surgically removing **only** the atom-owned graph, never a personal
   graph, via the CLI's own `graph remove` verb.
4. A global escape hatch to disable co-location (revert to today's isolated location) for
   headless/remote/privacy deployments.
5. Dynamic resolution of the Logseq graph home (never hardcode `~/logseq`).
6. A live-GUI validation spike that confirms the (high-confidence but statically-derived) behavior
   before we rely on it.

## Non-goals

- **Container / Phase-2 bridge.** Co-location assumes atom and Logseq share a host filesystem. When
  atom moves into the Phase-2 Docker sandbox, the vault leaves the host's `~/logseq` and this
  approach needs a separate bridge (bind-mount or export-out). Out of scope here; documented as a
  known future liability.
- **Forcing a live, mid-session first-appearance** in an already-open GUI (see First-appearance).
- **Logical write-ordering guarantees** between concurrent GUI edits and run upserts (the shared
  worker prevents corruption; fine-grained edit interleaving is Logseq's concern, not atom's).
- **Sync / e2ee / remote graphs.**
- **Notes for the single-agent CLI** (`atom run`) — notes remain workflow-only, per the existing
  precedent.

## Background: verified facts (from reading the Logseq 2.0.1 bundle + a live vault)

These are the load-bearing findings the design rests on. High confidence; the two feared risks are
retired, one narrow gap remains.

- **Vault format = Logseq DB graph** (a single app-managed `db.sqlite` under
  `<root-dir>/graphs/<graph>/`), *not* markdown-under-git. Confirmed by live inspection of
  `~/.atom/notes/notes-smoke/graphs/notes-smoke/`.
- **Switcher membership = disk presence.** The GUI builds its switcher from a **disk scan** of
  `<root-dir>/graphs/*` (electron `getGraphs` = `readdirSync` ∪ db-worker `list-db`). The
  `logseq_db_*` Local-Storage keys are only sort/last-seen metadata — **not** a gate. So a graph
  directory existing under the home is sufficient to be a switcher entry. *(Retires the "persisted
  registry may block it" risk.)*
- **Concurrency is safe via a shared worker.** `db-worker-node` is keyed by `(root-dir, graph)`.
  The GUI owns a worker when a graph is open (holding an O_EXCL `db-worker.lock`, publishing
  `<pid> <port>` in `<root-dir>/server-list`). When the CLI writes to that graph in the **same
  root-dir**, it **discovers and reuses** the GUI's worker (reuse ignores owner) and POSTs writes
  over HTTP. One worker → one writer to `db.sqlite`. A hard O_EXCL lock backstops any race.
  *(Retires the dual-writer corruption risk.)*
- **The one gap — live first-appearance.** The disk scan only re-runs on **app startup**, sync, and
  RTC login. There is no file watcher, no external "rescan" command, and the CLI has **no IPC
  channel to the renderer**. So a brand-new graph dropped on disk appears **on the next Logseq
  launch** (reliable) but **not instantly** in an already-open session. This is a *one-time,
  per-workflow* event — a graph first-appears once, then is always present, and its *contents*
  update live via the shared worker.
- **Lifecycle ops refuse GUI-open graphs.** `graph remove`/`graph import` call `stop_server` first,
  which returns **error 97** against an electron-owned (GUI-open) worker. Plain `upsert` writes are
  unaffected. `clear` must handle 97 gracefully.
- **CLI verbs (exact):** `graph create --graph <n> --root-dir <p>` (DB graph is the default; no
  format flag), `graph remove --graph <n> --root-dir <p>`,
  `graph list --root-dir <p> --output json` → `{data:{graphs:[…], graph-items:[{graph-name,
  graph-dir}]}}`, `graph export/import --type sqlite|edn`.

## Background: relevant current architecture

- **Provisioning** — `src/atom/notes.py`: `notes_root(home, workflow_name) =
  atom_home(home)/"notes"/_slug(name)`; `ensure_vault(...)` runs `graph list` then `graph create
  --graph <slug> --root-dir <notes_root>` if absent (idempotent), returns
  `NotesBinding(provider, root_dir, graph)`. `_default_runner` shells out to `logseq` (raises if
  not on PATH). `NotesBinding.as_prompt_ctx()` exposes `{provider, root_dir, graph}` to prompts.
- **Wiring** — `src/atom/workflow/engine.py:328-341` calls `ensure_vault` once per notes-enabled
  run (halts the run on failure); forwards the binding to each task's `run_agent(notes=…)`.
- **Schema** — `src/atom/workflow/schema.py:27-32` `NotesConfig{enabled=False,
  provider="logseq", graph: Optional[str]=None}` on `WorkflowDef`.
- **Clear** — `clear_vault(...)` (`notes.py:69-83`) does a path-confined `rmtree` of
  `$ATOM_HOME/notes/<slug>/`; surfaced via `atom workflow notes clear` (`cli.py:172-198`),
  `DELETE /api/workflows/{name}/notes` (`api/app.py:109-120`), and a UI button
  (`atom-ui/src/Workflows.tsx`), all gated on `RunStore.has_active_runs`.
- **Home** — `src/atom/sandbox/paths.py`: `atom_home()` resolves `ATOM_HOME` env → config → `~/.atom`
  (overridable — resolve dynamically, never hardcode).

## Design

### 1. Namespace constant

Introduce a module-level constant in `notes.py`:

```python
ATOM_GRAPH_PREFIX = "atom."   # load-bearing: identifies atom-owned graphs; clear only removes these
```

The co-located graph name is `ATOM_GRAPH_PREFIX + (notes.graph override or _slug(workflow_name))`,
e.g. `atom.research-agent`. Always namespaced, even when `notes.graph` is overridden, so the
clear-safety invariant (below) always holds. The `.` reads clearly in the switcher and marks the
graph as atom-managed; the live spike confirms `.` encodes/renders cleanly, with `atom-<slug>` as a
trivial fallback if not.

### 2. Resolve the Logseq graph home (dynamic)

New helper `logseq_root_dir(config)` resolving the root-dir whose `graphs/` the desktop app scans:

1. atom config override `notes.logseq_root_dir` (for non-default installs), else
2. `~/logseq` (the CLI + desktop default).

**Invariant:** atom's `--root-dir` MUST equal the desktop app's graph home, or the shared-worker
coordination is bypassed and the dual-writer risk returns. Default `~/logseq` matches the observed
GUI home (its built-in `Demo` graph lives at `~/logseq/graphs/Demo`); the spike confirms it, and the
override exists for anyone whose app uses a different home. (`$LOGSEQ_GRAPHS_DIR`, if set, points at
the *graphs* dir — root-dir is its parent; handle that mapping in the resolver.)

### 3. Provisioning re-point (`ensure_vault`)

Behavior branches on the global `expose_to_logseq` flag:

- **Exposed (default):** `root_dir = logseq_root_dir(config)`; `graph = atom.<slug>`. `graph
  create --graph atom.<slug> --root-dir <home>` if not already in `graph list`. `NotesBinding`
  now points agents at the shared home + `atom.<slug>`; the prompt snippet updates for free.
- **Isolated (flag off / legacy):** unchanged — `root_dir = $ATOM_HOME/notes/<slug>`,
  `graph = <slug>`.

Idempotent list-then-create is preserved; tolerates the known `graph create` stderr process-scan
warning (verify by zero return + follow-up list, as today).

### 4. `clear_vault` rework (surgical + namespace-guarded)

Old model rmtree'd an isolated directory. Co-located clear must delete exactly one graph from a
home shared with personal graphs:

- **Exposed:** compute `atom.<slug>`; `graph list --root-dir <home> --output json`; **refuse**
  unless the target exists *and* its name starts with `ATOM_GRAPH_PREFIX` (the guard — atom never
  removes a non-`atom.` graph, protecting personal ones; this replaces the old path-confinement as
  the load-bearing safety check). Then `graph remove --graph atom.<slug> --root-dir <home>`.
  - Keep the existing **active-run gate** (`has_active_runs`).
  - Handle **error 97** (graph currently open in the GUI) gracefully: return a clear,
    non-fatal message ("close the graph in Logseq, then retry") rather than a stack trace; surface
    as a 409-style response on the REST path.
- **Isolated:** unchanged path-confined `rmtree`.

### 5. Config / opt-out

A **global** deployment flag (in atom's global config model, alongside `bash_enabled` etc.):

```yaml
notes:
  expose_to_logseq: true          # default; false → today's isolated ~/.atom/notes behavior
  # logseq_root_dir: ~/logseq     # optional override; must equal the desktop app's graph home
```

Global (not per-workflow) because it's a deployment/environment concern: headless or remote atom
(no local GUI) sets it false; local desktop users leave it on.

### 6. First-appearance handling — accept + document *(assumed default; see Open decisions)*

Do not build machinery to force a live mid-session rescan. Document (README) that a **new** vault
appears in the switcher on the next Logseq launch, and that **content** updates to an already-open
graph are live. The run view / CLI shows the graph name (`atom.<slug>`) so the user knows what to
open. A one-time "first-run nudge" (a hint after a vault is first created) is a cheap, deferred
follow-up if the restart edge proves annoying.

### 7. Migration of existing isolated vaults — fresh start (confirmed)

No migration path at all. New runs provision fresh co-located graphs; pre-existing isolated vaults
(`~/.atom/notes/<slug>/`, currently only the disposable `notes-smoke`/`smoke-test` test graphs) are
left on disk, unread, and simply orphaned. No auto-migrate, no `notes migrate` command. (If a real
accumulated vault ever needs moving later, a one-off `graph export --type sqlite` + `graph import`
does it by hand; not worth a shipped command now.)

## Concurrency & safety summary

- Co-location ⇒ one shared `db-worker-node` per `(home, atom.<slug>)` ⇒ one writer to `db.sqlite`.
  Safe. Backstopped by the worker's O_EXCL `db-worker.lock`.
- `upsert`/write ops during a run are unaffected by GUI-open state. Only `graph remove` (clear) and
  `graph import` (migrate) refuse a GUI-open graph (error 97) — both are user-triggered and handled
  gracefully.
- **Assumption to preserve:** atom's `logseq` CLI and the desktop GUI are the **same install /
  revision** (true today — the CLI *is* the app). A divergent standalone CLI could break worker
  sharing; document as a prerequisite (the existing README "logseq CLI on PATH, guaranteed on
  target devices" already implies this).

## Validation

**Step 1 — live GUI spike (user-in-loop), before relying on the static analysis:**

1. With Logseq **closed**, `graph create --graph atom.spike --root-dir <home>`; launch Logseq;
   confirm `atom.spike` appears in the switcher. Then, with Logseq **running**, create
   `atom.spike2` and confirm it does **not** appear until restart (validates disk-scan trigger set).
2. Open `atom.spike` in the GUI; run a CLI `upsert` against it; confirm (a) `server-list` shows one
   worker, (b) the write appears live in the GUI, (c) no lock/corruption error.
3. Confirm `.` in the graph name renders/encodes cleanly (else fall back to `atom-<slug>`).
4. Confirm `~/logseq` is the desktop app's actual graph home (switcher shows the same graphs as
   `graph list`); `graph remove --graph atom.spike` cleans up.

**Step 2 — unit tests:** provisioning path selection (exposed vs isolated), home resolution
(default/override/`LOGSEQ_GRAPHS_DIR` parent), the `atom.` namespace guard in `clear_vault`
(refuses non-prefixed names — the critical safety test), idempotent create, error-97 handling
surfaced as a clean message. Reuse the fake `runner` injection already used by `notes.py` tests.

**Step 3 — smoke:** adapt `workflows/notes-smoke.yaml`; verify the graph lands under the home and is
listed by `graph list --root-dir <home>`.

## Risks & constraints

- **Root-dir mismatch** bypasses worker sharing → dual-writer risk. Mitigated by defaulting to the
  app's home and documenting the invariant; the spike confirms it.
- **Namespace guard is load-bearing** for clear-safety — never remove a non-`atom.` graph.
- **Error 97** on `remove`/`import` against GUI-open graphs — handled, not fatal.
- **Phase-2 container** breaks co-location (out of scope; documented).
- **Personal-home pollution** — atom graphs sit beside personal graphs in `~/logseq`; the `atom.`
  prefix keeps them visually and operationally distinct.

## Files touched (anticipated)

- `src/atom/notes.py` — prefix constant, `logseq_root_dir`, `ensure_vault` branch, `clear_vault`
  rework + namespace guard.
- `src/atom/workflow/engine.py` — pass config/flag through to `ensure_vault` (minimal).
- global config model + `config.yaml` — `notes.expose_to_logseq` (+ optional `logseq_root_dir`).
- `src/atom/cli.py` — `atom workflow notes clear` message handling.
- `src/atom/api/app.py` — error-97 → 409-style on the clear route.
- `README.md` — behavior, prerequisite/invariant, first-appearance note, opt-out flag.
- `tests/` — provisioning/clear/guard/home-resolution tests.
- *(No atom-ui change in v1; the clear button keeps working. A first-run nudge would touch
  `RunView`/`Workflows.tsx` — deferred.)*

## Decisions (confirmed 2026-07-19)

1. **First-appearance:** **Accept + document.** New vaults appear on the next Logseq launch;
   already-open graphs update live. No first-run nudge in v1 (cheap deferred follow-up if the
   restart edge annoys).
2. **Migration:** **Fresh start.** No auto-migrate and no `notes migrate` command — existing
   isolated vaults are disposable test graphs, orphaned in place.
3. **Scope/opt-out:** **Default-on + global `expose_to_logseq` switch** — matches the "always
   present" intent while staying escapable for headless/remote/privacy deployments.
