# Expose Workflow Vaults to the Logseq Desktop App — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make each workflow's persistent Logseq vault appear automatically in the user's Logseq desktop graph switcher — no manual export — by provisioning it as a namespaced graph inside the desktop app's own graph home.

**Architecture:** Instead of an isolated per-workflow Logseq "home" at `$ATOM_HOME/notes/<slug>/`, provision the vault as a DB graph named `atom.<slug>` inside the desktop app's graph home (default `~/logseq`). The switcher is a disk scan of `<home>/graphs/*`, so presence there = visibility; and because atom's `logseq` CLI *is* the desktop app, a run and an open GUI share one `db-worker-node` on the `db.sqlite`, so concurrent access is safe. A global `expose_to_logseq` flag (shipped on) toggles this; when off, behavior is exactly as today. `clear` is reworked to surgically `graph remove` only the `atom.`-namespaced graph.

**Tech Stack:** Python 3, Pydantic v2 (config), Typer (CLI), FastAPI (API), pytest. The external `logseq` CLI (Logseq desktop v2.0.1, DB-graph generation) is a runtime prerequisite; unit tests inject a fake CLI runner and never shell out.

**Spec:** `docs/superpowers/specs/2026-07-19-expose-vault-to-logseq-design.md`

## Global Constraints

- **DB graph, CLI-only.** The vault is a Logseq DB graph (`db.sqlite`), provisioned and removed **only** via the `logseq` CLI (`graph list|create|remove`). Never hand-write or `rmtree` a graph directory in exposed mode.
- **Namespace is load-bearing.** The exposed graph name is ALWAYS `atom.` + `_slug(override or workflow_name)`. `clear` must NEVER `graph remove` a name that doesn't start with `atom.` — this is the safety guard protecting the user's personal graphs that share the home.
- **Resolve the home dynamically.** Never hardcode `~/logseq`. Resolution order: explicit override → `$LOGSEQ_GRAPHS_DIR`'s parent → `~/logseq`.
- **Default posture.** The `AtomConfig.notes.expose_to_logseq` *Pydantic field default is `False`* (keeps programmatic/test configs isolated and existing tests green); the shipped `config.yaml` sets it `true` (product is default-on, since `load_config` auto-discovers `config.yaml`). Do not change the field default to `True`.
- **Local-host only.** Concurrency safety relies on atom's `logseq` CLI being the SAME install as the desktop app (one shared db-worker). Phase-2 container is out of scope.
- **Lifecycle ops refuse GUI-open graphs.** `graph remove` fails (error 97, stderr ~"owned by another process") if the graph is open in the GUI; surface this as a clean, catchable `VaultBusyError`, not a stack trace.
- TDD, frequent commits, exact file paths, no placeholders.

---

### Task 1: Live-GUI feasibility spike (human-in-loop, no code)

Converts the high-confidence static analysis into observed fact before code depends on it. **Requires the user** (only they can see the Logseq GUI). Run it first; it also decides the namespace separator character used in Task 3.

**Files:**
- Modify (record outcomes): `docs/superpowers/specs/2026-07-19-expose-vault-to-logseq-design.md` (append a "Spike results" note)

**Interfaces:**
- Produces: confirmed facts consumed by Task 3 (`ATOM_GRAPH_PREFIX` = `"atom."` unless the spike shows `.` renders/encodes badly, then `"atom-"`) and by the Global Constraints (home = `~/logseq` unless the user's app differs).

- [ ] **Step 1: Confirm the app's graph home**

Ask the user to open Logseq → graph switcher, and compare with:

Run: `logseq graph list --output json`
Expected: the `data.graphs` names match what the user sees in the switcher (confirms `~/logseq` is the GUI's home). If they differ, record the real home path for the `logseq_root_dir` override.

- [ ] **Step 2: Confirm a CLI-created graph appears after restart, not live**

Run (with Logseq **running**): `logseq graph create --graph atom.spike --root-dir ~/logseq`
Ask the user: does `atom.spike` appear in the switcher *without* restarting? Expected: **no** (disk scan only re-runs on launch). Then ask them to fully quit and reopen Logseq. Expected: `atom.spike` now appears. Record both observations.

- [ ] **Step 3: Confirm `.` in the graph name renders cleanly**

While confirming Step 2, note whether `atom.spike` displays correctly in the switcher (not mangled/split). If it looks wrong, the naming separator becomes `atom-` in Task 3.

- [ ] **Step 4: Confirm concurrent write is safe + live**

Ask the user to open `atom.spike` in the GUI. Then run:
`logseq server list --root-dir ~/logseq` (expect one worker for the graph)
`logseq upsert page --graph atom.spike --root-dir ~/logseq --title "SpikeNote"` (adjust to a valid upsert per `logseq example upsert page`)
Ask the user: did "SpikeNote" appear live in the open GUI, with no lock/error? Expected: yes.

- [ ] **Step 5: Clean up the spike graph**

Ask the user to switch the GUI off `atom.spike` first (else remove hits error 97), then:
Run: `logseq graph remove --graph atom.spike --root-dir ~/logseq`
Expected: success. Note the **exact stderr string** if you deliberately try removing it while still open — it calibrates the `VaultBusyError` match in Task 5.

- [ ] **Step 6: Record outcomes + commit**

Append a short "Spike results (2026-07-19)" section to the design spec with the five observations and the chosen separator char.

```bash
git add docs/superpowers/specs/2026-07-19-expose-vault-to-logseq-design.md
git commit -m "docs(spec): record logseq auto-view spike results"
```

---

### Task 2: Global `NotesRuntimeConfig` (config schema + config.yaml)

Adds the deployment-level toggle and optional home override.

**Files:**
- Modify: `src/atom/config/schema.py` (add `NotesRuntimeConfig`, add `notes` field to `AtomConfig`)
- Modify: `config.yaml` (ship the flag on)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `AtomConfig.notes.expose_to_logseq: bool` (field default `False`) and `AtomConfig.notes.logseq_root_dir: Optional[str]` — consumed by Tasks 4 and 6.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
def test_notes_runtime_config_defaults():
    from atom.config.schema import AtomConfig
    cfg = AtomConfig()
    assert cfg.notes.expose_to_logseq is False       # field default is off; config.yaml turns it on
    assert cfg.notes.logseq_root_dir is None


def test_notes_runtime_config_from_yaml():
    from atom.config.schema import AtomConfig
    cfg = AtomConfig.model_validate(
        {"notes": {"expose_to_logseq": True, "logseq_root_dir": "~/logseq"}}
    )
    assert cfg.notes.expose_to_logseq is True
    assert cfg.notes.logseq_root_dir == "~/logseq"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_config.py::test_notes_runtime_config_defaults tests/test_config.py::test_notes_runtime_config_from_yaml -v`
Expected: FAIL with `AttributeError: 'AtomConfig' object has no attribute 'notes'`.

- [ ] **Step 3: Add the config class + field**

In `src/atom/config/schema.py`, add after `class TodosConfig` (near line 122):

```python
class NotesRuntimeConfig(_Base):
    # Surface each workflow's Logseq vault directly in the desktop app's graph home so it appears
    # in the graph switcher with no manual export. When True, a workflow's vault is provisioned as
    # `atom.<slug>` under `logseq_root_dir` (the app's home). When False, the vault stays isolated
    # at $ATOM_HOME/notes/<slug>/ (invisible to the GUI) — the legacy behavior. The Pydantic
    # default is False so programmatic/embedded configs stay isolated; the shipped config.yaml
    # turns it on for the normal desktop deployment.
    expose_to_logseq: bool = False
    # The desktop app's graph home (the --root-dir whose graphs/ the app scans). None ->
    # $LOGSEQ_GRAPHS_DIR's parent, else ~/logseq. Must equal the app's actual home, or the
    # shared-db-worker safety is bypassed. Set this only if the app uses a non-default home.
    logseq_root_dir: Optional[str] = None
```

In `class AtomConfig`, add the field after the `todos` line (near line 197):

```python
    notes: NotesRuntimeConfig = Field(default_factory=NotesRuntimeConfig)
```

- [ ] **Step 4: Ship the flag on in config.yaml**

In `config.yaml`, add a top-level block after the `workflow:` block:

```yaml
notes:
  expose_to_logseq: true    # show each workflow's Logseq vault in the desktop app's graph switcher
                            # (as `atom.<slug>`, no manual export). false -> isolated ~/.atom/notes.
  # logseq_root_dir: ~/logseq   # override only if your Logseq app's graph home isn't ~/logseq
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_config.py -v`
Expected: PASS (all, including the two new tests).

- [ ] **Step 6: Commit**

```bash
git add src/atom/config/schema.py config.yaml tests/test_config.py
git commit -m "feat(config): add notes.expose_to_logseq runtime toggle"
```

---

### Task 3: notes.py primitives (prefix, home resolver, graph-name, busy error)

Pure, dependency-free helpers that Tasks 4–5 build on.

**Files:**
- Modify: `src/atom/notes.py` (add `import os`; `ATOM_GRAPH_PREFIX`; `VaultBusyError`; `resolve_logseq_root`; `_atom_graph_name`)
- Test: `tests/test_notes.py`

**Interfaces:**
- Produces:
  - `ATOM_GRAPH_PREFIX: str` (= `"atom."`, or `"atom-"` if Task 1 Step 3 required it)
  - `resolve_logseq_root(override: str | None = None) -> pathlib.Path`
  - `_atom_graph_name(workflow_name: str, override: str | None = None) -> str`
  - `_list_graph_names(run: CLIRunner, root_dir: pathlib.Path) -> list[str]` (shared by `ensure_vault` + `clear_vault`)
  - `class VaultBusyError(RuntimeError)`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_notes.py`:

```python
def test_atom_graph_name_prefixes_and_slugs():
    from atom.notes import ATOM_GRAPH_PREFIX, _atom_graph_name
    assert _atom_graph_name("Research Agent", None) == f"{ATOM_GRAPH_PREFIX}research-agent"
    assert _atom_graph_name("wf", "My Custom Graph") == f"{ATOM_GRAPH_PREFIX}my-custom-graph"
    # the name is always namespaced, so it is safe to feed to `graph remove`
    assert _atom_graph_name("anything", None).startswith(ATOM_GRAPH_PREFIX)


def test_resolve_logseq_root_override_wins(tmp_path, monkeypatch):
    from atom.notes import resolve_logseq_root
    monkeypatch.delenv("LOGSEQ_GRAPHS_DIR", raising=False)
    assert resolve_logseq_root(str(tmp_path / "home")) == (tmp_path / "home").resolve()


def test_resolve_logseq_root_env_points_at_graphs_dir(tmp_path, monkeypatch):
    from atom.notes import resolve_logseq_root
    # $LOGSEQ_GRAPHS_DIR is the graphs/ dir; the CLI root-dir is its parent.
    monkeypatch.setenv("LOGSEQ_GRAPHS_DIR", str(tmp_path / "gg" / "graphs"))
    assert resolve_logseq_root(None) == (tmp_path / "gg").resolve()


def test_list_graph_names_parses_json_and_tolerates_garbage(tmp_path):
    from atom.notes import _list_graph_names
    ok = lambda args: (0, '{"data":{"graphs":["atom.wf","Demo"]}}', "")
    assert _list_graph_names(ok, tmp_path) == ["atom.wf", "Demo"]
    garbage = lambda args: (0, "not json", "")
    assert _list_graph_names(garbage, tmp_path) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_notes.py::test_atom_graph_name_prefixes_and_slugs tests/test_notes.py::test_resolve_logseq_root_override_wins tests/test_notes.py::test_resolve_logseq_root_env_points_at_graphs_dir -v`
Expected: FAIL with `ImportError: cannot import name 'ATOM_GRAPH_PREFIX'`.

- [ ] **Step 3: Implement the primitives**

In `src/atom/notes.py`, add `import os` to the imports (after `import json`), then add below the `_slug` function (near line 34):

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_notes.py -v`
Expected: PASS (new + all existing notes tests).

- [ ] **Step 5: Commit**

```bash
git add src/atom/notes.py tests/test_notes.py
git commit -m "feat(notes): add logseq-home resolver, atom.<slug> namer, VaultBusyError"
```

---

### Task 4: `ensure_vault` exposed branch + engine wiring

Provision into the app's home as `atom.<slug>` when exposed; unchanged when not.

**Files:**
- Modify: `src/atom/notes.py` (`ensure_vault` signature + branch)
- Modify: `src/atom/workflow/engine.py:331` (pass config flags)
- Test: `tests/test_notes.py`

**Interfaces:**
- Consumes: `resolve_logseq_root`, `_atom_graph_name` (Task 3); `AtomConfig.notes.*` (Task 2).
- Produces: `ensure_vault(home, workflow_name, notes_cfg, *, expose_to_logseq: bool = False, logseq_root_dir: str | None = None, runner=None) -> NotesBinding`. When exposed, `NotesBinding.root_dir` is the app's home and `.graph` is `atom.<slug>`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_notes.py`:

```python
def test_ensure_vault_exposed_provisions_namespaced_in_home(atom_home, tmp_path):
    home = tmp_path / "logseq"
    calls = []

    def fake_runner(args):
        calls.append(args)
        if args[1:3] == ["graph", "list"]:
            return 0, '{"data":{"graphs":[]}}', ""
        return 0, 'Created graph "atom.research-agent"', ""

    cfg = SimpleNamespace(provider="logseq", graph=None)
    binding = ensure_vault(
        str(atom_home), "Research Agent", cfg,
        expose_to_logseq=True, logseq_root_dir=str(home), runner=fake_runner,
    )
    assert binding.graph == "atom.research-agent"
    assert binding.root_dir == str(home.resolve())
    create = next(a for a in calls if a[1:3] == ["graph", "create"])
    assert create[create.index("--graph") + 1] == "atom.research-agent"
    assert create[create.index("--root-dir") + 1] == str(home.resolve())


def test_ensure_vault_exposed_reuses_when_present(atom_home, tmp_path):
    calls = []

    def fake_runner(args):
        calls.append(args)
        if args[1:3] == ["graph", "list"]:
            return 0, '{"data":{"graphs":["atom.wf","Demo"]}}', ""
        return 0, "", ""

    cfg = SimpleNamespace(provider="logseq", graph=None)
    ensure_vault(str(atom_home), "wf", cfg,
                 expose_to_logseq=True, logseq_root_dir=str(tmp_path), runner=fake_runner)
    assert not any(a[1:3] == ["graph", "create"] for a in calls)  # reused


def test_ensure_vault_default_is_isolated(atom_home):
    # No expose flag -> legacy isolated location + bare slug graph name (backward compatible).
    def fake_runner(args):
        if args[1:3] == ["graph", "list"]:
            return 0, '{"data":{"graphs":[]}}', ""
        return 0, "", ""

    cfg = SimpleNamespace(provider="logseq", graph=None)
    b = ensure_vault(str(atom_home), "wf", cfg, runner=fake_runner)
    assert b.graph == "wf"
    assert b.root_dir == str(atom_home / "notes" / "wf")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_notes.py::test_ensure_vault_exposed_provisions_namespaced_in_home -v`
Expected: FAIL with `TypeError: ensure_vault() got an unexpected keyword argument 'expose_to_logseq'`.

- [ ] **Step 3: Rewrite `ensure_vault`**

Replace the current `ensure_vault` in `src/atom/notes.py` (lines 49-66) with:

```python
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
```

- [ ] **Step 4: Wire the engine call site**

In `src/atom/workflow/engine.py`, replace line 331:

```python
                    notes_binding = ensure_vault(self.cfg.home, workflow.name, workflow.notes)
```

with:

```python
                    notes_binding = ensure_vault(
                        self.cfg.home, workflow.name, workflow.notes,
                        expose_to_logseq=self.cfg.notes.expose_to_logseq,
                        logseq_root_dir=self.cfg.notes.logseq_root_dir,
                    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_notes.py tests/test_workflow_engine.py -v`
Expected: PASS. (`test_workflow_engine.py` stubs `ensure_vault`, so it is unaffected by the new kwargs.)

- [ ] **Step 6: Commit**

```bash
git add src/atom/notes.py src/atom/workflow/engine.py tests/test_notes.py
git commit -m "feat(notes): provision vault into logseq home as atom.<slug> when exposed"
```

---

### Task 5: `clear_vault` exposed branch (namespace guard + busy error)

Surgically remove only the `atom.<slug>` graph via the CLI; keep the legacy rmtree path when not exposed.

**Files:**
- Modify: `src/atom/notes.py` (`clear_vault` signature + exposed branch + `_is_busy` helper)
- Test: `tests/test_notes.py`

**Interfaces:**
- Consumes: `resolve_logseq_root`, `_atom_graph_name`, `ATOM_GRAPH_PREFIX`, `VaultBusyError` (Task 3).
- Produces: `clear_vault(home, workflow_name, *, expose_to_logseq: bool = False, logseq_root_dir: str | None = None, graph_override: str | None = None, runner=None) -> bool`. Raises `VaultBusyError` if the graph is open in the GUI. The legacy positional call `clear_vault(home, name)` still works (isolated path).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_notes.py`:

```python
def test_clear_vault_exposed_removes_namespaced_graph(tmp_path):
    from atom.notes import clear_vault
    calls = []

    def fake_runner(args):
        calls.append(args)
        if args[1:3] == ["graph", "list"]:
            return 0, '{"data":{"graphs":["atom.wf","Demo"]}}', ""
        return 0, "removed", ""

    assert clear_vault("ignored", "wf", expose_to_logseq=True,
                       logseq_root_dir=str(tmp_path), runner=fake_runner) is True
    remove = next(a for a in calls if a[1:3] == ["graph", "remove"])
    assert remove[remove.index("--graph") + 1] == "atom.wf"


def test_clear_vault_exposed_absent_is_false_and_never_removes(tmp_path):
    from atom.notes import clear_vault

    def fake_runner(args):
        if args[1:3] == ["graph", "list"]:
            # a personal graph literally named "wf" is present; atom.wf is NOT.
            return 0, '{"data":{"graphs":["wf","Demo"]}}', ""
        raise AssertionError("must not `graph remove` when atom.<slug> is absent")

    assert clear_vault("x", "wf", expose_to_logseq=True,
                       logseq_root_dir=str(tmp_path), runner=fake_runner) is False


def test_clear_vault_exposed_busy_raises(tmp_path):
    from atom.notes import clear_vault, VaultBusyError

    def fake_runner(args):
        if args[1:3] == ["graph", "list"]:
            return 0, '{"data":{"graphs":["atom.wf"]}}', ""
        return 1, "", "server is owned by another process"

    with pytest.raises(VaultBusyError):
        clear_vault("x", "wf", expose_to_logseq=True,
                    logseq_root_dir=str(tmp_path), runner=fake_runner)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_notes.py::test_clear_vault_exposed_removes_namespaced_graph -v`
Expected: FAIL with `TypeError: clear_vault() got an unexpected keyword argument 'expose_to_logseq'`.

- [ ] **Step 3: Rewrite `clear_vault`**

Replace the current `clear_vault` in `src/atom/notes.py` (lines 69-83) with:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_notes.py -v`
Expected: PASS (new exposed-clear tests + all existing isolated-clear tests unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/atom/notes.py tests/test_notes.py
git commit -m "feat(notes): exposed clear_vault via graph remove with atom.<slug> guard + VaultBusyError"
```

---

### Task 6: Wire the clear call sites (CLI + API) and fix affected integration tests

Thread the config flags + per-workflow graph override into both clear surfaces; map `VaultBusyError` to CLI exit 1 / HTTP 409; keep the isolated-path integration tests green now that `config.yaml` ships exposed.

**Files:**
- Modify: `src/atom/cli.py:176-198` (`workflow_notes_clear`)
- Modify: `src/atom/api/app.py:109-120` (`clear_workflow_notes`)
- Test: `tests/test_workflow_cli.py`, `tests/test_workflow_api.py`

**Interfaces:**
- Consumes: `clear_vault(..., expose_to_logseq, logseq_root_dir, graph_override)` + `VaultBusyError` (Task 5); `cfg.notes.*` (Task 2); `WorkflowDef.notes.graph`.

- [ ] **Step 1: Update the CLI clear command**

Replace the body of `workflow_notes_clear` in `src/atom/cli.py` (lines 182-198, from `"""Delete...` onward) with:

```python
    """Delete a workflow's persistent Logseq vault (a fresh one is provisioned on the next run)."""
    from atom.notes import VaultBusyError, clear_vault
    from atom.workflow.run_store import RunStore
    from atom.workflow.schema import load_workflow

    cfg = load_config(config)
    if RunStore(cfg.home).has_active_runs(name):
        console.print(
            f"[red]Refusing to clear notes for '{name}': a run is active. "
            f"Wait for it to finish or cancel it first.[/red]"
        )
        raise typer.Exit(1)
    if not yes:
        typer.confirm(f"Delete the persistent Logseq vault for workflow '{name}'?", abort=True)
    graph_override = None
    try:
        graph_override = load_workflow(name, cfg.home).notes.graph
    except FileNotFoundError:
        pass
    try:
        cleared = clear_vault(
            cfg.home, name,
            expose_to_logseq=cfg.notes.expose_to_logseq,
            logseq_root_dir=cfg.notes.logseq_root_dir,
            graph_override=graph_override,
        )
    except VaultBusyError as exc:
        console.print(f"[red]Cannot clear notes for '{name}': {exc}[/red]")
        raise typer.Exit(1)
    if cleared:
        console.print(f"[green]Cleared notes vault for '{name}'.[/green]")
    else:
        console.print(f"[dim]No notes vault existed for '{name}'.[/dim]")
```

- [ ] **Step 2: Update the API clear route**

Replace `clear_workflow_notes` in `src/atom/api/app.py` (lines 109-120) with:

```python
    @app.delete("/api/workflows/{name}/notes")
    def clear_workflow_notes(name: str) -> dict:
        """Delete a workflow's persistent Logseq vault (re-provisioned on its next run)."""
        from atom.notes import VaultBusyError, clear_vault

        try:
            wf = load_workflow(name, cfg.home)
        except FileNotFoundError:
            raise HTTPException(404, f"workflow '{name}' not found")
        if engine.store.has_active_runs(name):
            raise HTTPException(409, f"workflow '{name}' has an active run; cannot clear notes")
        try:
            cleared = clear_vault(
                cfg.home, name,
                expose_to_logseq=cfg.notes.expose_to_logseq,
                logseq_root_dir=cfg.notes.logseq_root_dir,
                graph_override=wf.notes.graph,
            )
        except VaultBusyError as exc:
            raise HTTPException(409, str(exc))
        return {"workflow": name, "cleared": cleared}
```

- [ ] **Step 3: Fix the CLI isolated-path tests + add a busy test**

In `tests/test_workflow_cli.py`, replace `test_workflow_notes_clear_removes_vault` and `test_workflow_notes_clear_noop_when_absent` with versions that pin the isolated path via an explicit config, and add a busy-error test:

```python
def _isolated_cfg(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text("notes:\n  expose_to_logseq: false\n")
    return str(p)


def test_workflow_notes_clear_removes_vault(atom_home, tmp_path):
    from atom.notes import notes_root
    root = notes_root(str(atom_home), "demo")
    (root / "pages").mkdir(parents=True)
    result = runner.invoke(
        app, ["workflow", "notes", "clear", "demo", "--yes", "--config", _isolated_cfg(tmp_path)])
    assert result.exit_code == 0
    assert not root.exists()
    assert "Cleared" in result.stdout


def test_workflow_notes_clear_noop_when_absent(atom_home, tmp_path):
    result = runner.invoke(
        app, ["workflow", "notes", "clear", "ghost", "--yes", "--config", _isolated_cfg(tmp_path)])
    assert result.exit_code == 0
    assert "No notes vault" in result.stdout


def test_workflow_notes_clear_busy_exits_1(atom_home, tmp_path, monkeypatch):
    import atom.notes as notes_mod

    def _busy(*a, **k):
        raise notes_mod.VaultBusyError("graph 'atom.demo' is open in the Logseq desktop app")

    monkeypatch.setattr(notes_mod, "clear_vault", _busy)
    result = runner.invoke(
        app, ["workflow", "notes", "clear", "demo", "--yes", "--config", _isolated_cfg(tmp_path)])
    assert result.exit_code == 1
    assert "open in the Logseq desktop app" in result.stdout
```

(The existing `test_workflow_notes_clear_refuses_when_active_run` is unchanged — its gate short-circuits before `clear_vault`, so the exposure setting is irrelevant.)

- [ ] **Step 4: Add the API busy-error test**

In `tests/test_workflow_api.py`, append (near the other notes tests):

```python
@pytest.mark.asyncio
async def test_delete_workflow_notes_409_when_graph_open(base_config, atom_home, monkeypatch):
    _seed_notes_wf(atom_home)
    import atom.notes as notes_mod

    def _busy(*a, **k):
        raise notes_mod.VaultBusyError("graph 'atom.notewf' is open in the Logseq desktop app")

    monkeypatch.setattr(notes_mod, "clear_vault", _busy)
    app = create_app(base_config, engine=WorkflowEngine(base_config, prepared_provider=_provider))
    async with _client_no_worker(app) as c:
        r = await c.delete("/api/workflows/notewf/notes")
    assert r.status_code == 409
    assert "open in the Logseq desktop app" in r.json()["detail"]
```

(`test_delete_workflow_notes_clears_vault` needs NO change: it uses the `base_config` fixture, whose `notes.expose_to_logseq` is the field default `False`, so it exercises the isolated rmtree path exactly as before.)

- [ ] **Step 5: Run the affected suites**

Run: `python -m pytest tests/test_workflow_cli.py tests/test_workflow_api.py -v`
Expected: PASS (all, including the updated + new tests).

- [ ] **Step 6: Commit**

```bash
git add src/atom/cli.py src/atom/api/app.py tests/test_workflow_cli.py tests/test_workflow_api.py
git commit -m "feat(notes): thread expose flag into clear (CLI+API); map VaultBusyError to exit1/409"
```

---

### Task 7: Docs + full-suite verification

Document the new behavior and confirm the whole suite is green.

**Files:**
- Modify: `README.md` (persistent-notes section, ~lines 74-86)
- Test: full suite

**Interfaces:** none (documentation + verification).

- [ ] **Step 1: Update the README persistent-notes section**

In `README.md`, replace the paragraph at lines 83-86 (`When enabled, atom ensures a Logseq graph at ...`) with:

```markdown
When enabled, atom provisions a Logseq graph for the workflow (once, reused across runs) and injects
a snippet into each task's system prompt telling the agent where the vault is and to
`load_skill("logseq-cli")` for the CLI commands. Try it with `workflows/notes-smoke.yaml` (run it
twice — the second run recalls the first run's entry).

By default (`notes.expose_to_logseq: true` in `config.yaml`) the vault is provisioned **inside your
Logseq desktop app's graph home** as `atom.<workflow-slug>`, so it shows up in the app's graph
switcher with no manual export — a **new** vault appears the next time you launch Logseq, and writes
to an already-open graph appear live. Set `notes.expose_to_logseq: false` to keep vaults isolated
at `$ATOM_HOME/notes/<slug>/` (invisible to the GUI) for headless/remote deployments; use
`notes.logseq_root_dir` only if your app's graph home isn't `~/logseq`. This co-location assumes
atom and Logseq share one host (Phase-1); it does not apply to the future Docker sandbox.
```

- [ ] **Step 2: Run the full test suite**

Run: `python -m pytest -q`
Expected: PASS (no failures, no errors). If anything unrelated to notes fails, it is pre-existing; confirm by `git stash` comparison only if in doubt.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: document notes.expose_to_logseq auto-view behavior"
```

- [ ] **Step 4: Final manual smoke (with the user, optional but recommended)**

With `expose_to_logseq: true`, run `workflows/notes-smoke.yaml` once, then:
Run: `logseq graph list --root-dir ~/logseq --output json`
Expected: `atom.notes-smoke` appears in `data.graphs`. Ask the user to relaunch Logseq and confirm `atom.notes-smoke` is in the switcher with the run's note inside. Then `atom workflow notes clear notes-smoke --yes` and confirm it is removed from `graph list`.

---

## Self-Review

**1. Spec coverage:**
- Namespace constant (spec §1) → Task 3. ✓
- Dynamic home resolution (spec §2) → Task 3 (`resolve_logseq_root`) + Task 2 (`logseq_root_dir`). ✓
- Provisioning re-point (spec §3) → Task 4. ✓
- Concurrency (spec: nothing to build) → inherent; validated in Task 1 Step 4. ✓
- `clear_vault` rework + namespace guard + error-97 (spec §4) → Task 5; wired in Task 6. ✓
- Config/opt-out (spec §5) → Task 2. ✓
- First-appearance = accept + document (spec §6) → Task 7 README. ✓
- Migration = fresh start (spec §7) → no task needed (nothing to build); existing isolated vaults left in place. ✓ (Confirmed no code path reads them once exposed.)
- Validation: live spike + unit tests + smoke (spec Validation) → Task 1 (spike), Tasks 2-6 (unit/integration), Task 7 Step 4 (smoke). ✓

**2. Placeholder scan:** No "TBD"/"add error handling"/"similar to Task N". Every code step shows full code; the only conditional is the `ATOM_GRAPH_PREFIX` separator, which Task 1 resolves to a concrete value before Task 3. ✓

**3. Type consistency:** `ensure_vault` and `clear_vault` keyword names (`expose_to_logseq`, `logseq_root_dir`, `graph_override`, `runner`) are identical across notes.py, engine.py, cli.py, api/app.py, and tests. `resolve_logseq_root`/`_atom_graph_name`/`VaultBusyError`/`ATOM_GRAPH_PREFIX` names match every reference. `NotesRuntimeConfig` field names (`expose_to_logseq`, `logseq_root_dir`) match `cfg.notes.*` reads. ✓
