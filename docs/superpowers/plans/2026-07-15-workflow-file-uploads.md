# Workflow File-Upload Inputs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a workflow declare `type: file` inputs that the user uploads (UI/API/CLI); each upload lands in a shared per-run `uploads/` dir bound to the `{{ uploads }}` mount, and its `{{ name }}` resolves to the file's virtual path in task prompts.

**Architecture:** A file input is "just another named input" carrying `type: file`; its resolved value is the virtual path `/mnt/user-data/uploads/<name>.<ext>`. Uploads persist under `runs/<run_id>/uploads/` and are bound to every task via a new per-run `uploads_override`, mirroring the existing per-run `workspace` override. The on-disk name is derived deterministically from the (unique) input name, so the path stored in `inputs` always matches what gets written.

**Tech Stack:** Python 3.11+, LangChain/LangGraph v1, pydantic v2, FastAPI + `python-multipart`, React/TypeScript (Vite), Jinja2.

## Global Constraints

- Python `>=3.11`; `langchain>=1.0,<2`; `pydantic>=2.7`. One line, exact.
- **Run tests with `.venv/bin/python -m pytest` — NOT `.venv/bin/pytest`** (the bare script drops the repo root from `sys.path`, breaking `from tests.conftest import ...`).
- Config-driven: new knobs live in `src/atom/config/schema.py`, never hardcoded.
- **Deterministic naming:** the on-disk upload name is `stored_name(input_name, original_filename)` = `<sanitized input name><sanitized ext>`; input names are unique keys → collision-free. The API/CLI and `save_upload` MUST both derive the path from this function so they can never diverge.
- **Confinement:** the `uploads` mount is confined by `LocalSandbox.resolve()` to its own root; it is NOT subject to `sandbox.allowed_workspace_roots` (that guards only the external *workspace*). Do not add uploads to `allowed_workspace_roots`.
- Backward compatibility: `POST /api/runs` must still accept `application/json` (existing automation + tests). Existing text-only workflows and manifests (no `uploads_path`, no `type`) must keep working.

---

### Task 1: Config — `UploadsConfig`

**Files:**
- Modify: `src/atom/config/schema.py` (add `UploadsConfig`; add `AtomConfig.uploads`)
- Test: `tests/test_workflow_config.py` (append)

**Interfaces:**
- Produces: `UploadsConfig(max_file_bytes: int = 26_214_400, allowed_extensions: list[str] = [], max_files_per_run: int = 20)`; `AtomConfig.uploads: UploadsConfig`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_workflow_config.py`:

```python
def test_uploads_config_defaults():
    from atom.config.schema import AtomConfig
    cfg = AtomConfig()
    assert cfg.uploads.max_file_bytes == 26_214_400
    assert cfg.uploads.allowed_extensions == []
    assert cfg.uploads.max_files_per_run == 20


def test_uploads_config_override():
    from atom.config.schema import UploadsConfig
    uc = UploadsConfig(max_file_bytes=1024, allowed_extensions=["pdf", "txt"], max_files_per_run=3)
    assert uc.max_file_bytes == 1024
    assert uc.allowed_extensions == ["pdf", "txt"]
    assert uc.max_files_per_run == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_workflow_config.py::test_uploads_config_defaults -v`
Expected: FAIL with `AttributeError: 'AtomConfig' object has no attribute 'uploads'`

- [ ] **Step 3: Implement**

In `src/atom/config/schema.py`, add this class immediately after `QueueConfig` (around line 75):

```python
class UploadsConfig(_Base):
    # Limits for workflow file-input uploads. The API is unauthenticated with open CORS, so
    # these caps are the primary guard on an otherwise unbounded input surface.
    max_file_bytes: int = 26_214_400        # 25 MiB per file
    allowed_extensions: list[str] = Field(default_factory=list)  # empty = allow any; else lowercase, no dot
    max_files_per_run: int = 20
```

In `AtomConfig`, add the field right after the `queue:` line (line 143):

```python
    queue: QueueConfig = Field(default_factory=QueueConfig)
    uploads: UploadsConfig = Field(default_factory=UploadsConfig)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_workflow_config.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add src/atom/config/schema.py tests/test_workflow_config.py
git commit -m "feat(config): UploadsConfig for workflow file-input limits"
```

---

### Task 2: `uploads.py` pure helpers

**Files:**
- Create: `src/atom/workflow/uploads.py`
- Test: `tests/test_workflow_uploads.py`

**Interfaces:**
- Produces:
  - `safe_extension(original_filename: str) -> str` → sanitized lowercased suffix incl. dot, or `""`.
  - `stored_name(input_name: str, original_filename: str) -> str` → `<sanitized name><safe ext>`.
  - `virtual_upload_path(input_name: str, original_filename: str) -> str` → `/mnt/user-data/uploads/<stored_name>`.
  - `check_size(nbytes: int, limit: int) -> None` → raises `UploadTooLarge`.
  - `check_extension(original_filename: str, allowed: list[str]) -> None` → raises `UploadTypeNotAllowed`.
  - Exceptions `UploadTooLarge(ValueError)`, `UploadTypeNotAllowed(ValueError)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_workflow_uploads.py`:

```python
"""Pure upload helpers: deterministic naming + size/type checks."""
from __future__ import annotations

import pytest

from atom.workflow.uploads import (
    UploadTooLarge, UploadTypeNotAllowed,
    check_extension, check_size, safe_extension, stored_name, virtual_upload_path,
)


def test_safe_extension_cases():
    assert safe_extension("report.PDF") == ".pdf"
    assert safe_extension("archive.tar.gz") == ".gz"
    assert safe_extension("noext") == ""
    assert safe_extension("") == ""
    assert safe_extension("../../evil.sh") == ".sh"      # only the basename's suffix is taken


def test_stored_name_is_deterministic_and_collision_free():
    assert stored_name("doc", "q3-results.pdf") == "doc.pdf"
    # distinct input names -> distinct stored names even with identical client filenames
    assert stored_name("a", "data.csv") == "a.csv"
    assert stored_name("b", "data.csv") == "b.csv"


def test_stored_name_sanitizes_and_falls_back():
    assert stored_name("my input!", "x.txt") == "my-input.txt"
    assert stored_name("", "x.txt") == "upload.txt"


def test_virtual_upload_path():
    assert virtual_upload_path("doc", "q3.pdf") == "/mnt/user-data/uploads/doc.pdf"


def test_check_size():
    check_size(50, 50)          # equal is OK
    check_size(999, 0)          # 0 = no limit
    with pytest.raises(UploadTooLarge):
        check_size(51, 50)


def test_check_extension():
    check_extension("x.txt", [])            # empty allowlist = allow any
    check_extension("x.PDF", ["pdf"])       # case-insensitive
    with pytest.raises(UploadTypeNotAllowed):
        check_extension("x.txt", ["pdf"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_workflow_uploads.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'atom.workflow.uploads'`

- [ ] **Step 3: Implement**

Create `src/atom/workflow/uploads.py`:

```python
"""Pure helpers for workflow file-input uploads: safe naming + limit checks.

No I/O — callers (the API, the CLI, RunStore.save_upload) do the reading/writing and use these
to derive the on-disk name and enforce limits. The on-disk name is derived from the (unique)
workflow input NAME, so it is deterministic and collision-free by construction: a caller can
compute the stored path before writing and it is guaranteed to match what save_upload writes.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

from atom.sandbox.paths import VIRTUAL_UPLOADS


class UploadTooLarge(ValueError):
    """Raised when an uploaded file exceeds the configured size limit."""


class UploadTypeNotAllowed(ValueError):
    """Raised when an uploaded file's extension is not in the configured allowlist."""


_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _basename(filename: str) -> str:
    """Final path component, treating both / and \\ as separators (client-supplied names)."""
    name = str(filename or "").strip().replace("\\", "/")
    return PurePosixPath(name).name


def safe_extension(original_filename: str) -> str:
    """The sanitized, lowercased suffix (incl. dot) of ``original_filename``, or '' if none."""
    ext = PurePosixPath(_basename(original_filename)).suffix.lower()
    ext = _SAFE.sub("", ext).strip(".")
    return f".{ext}" if ext else ""


def _sanitize_stem(input_name: str) -> str:
    stem = _SAFE.sub("-", str(input_name or "").strip()).strip("-.")
    return stem or "upload"


def stored_name(input_name: str, original_filename: str) -> str:
    """Deterministic on-disk name: ``<sanitized input name><sanitized original extension>``."""
    return _sanitize_stem(input_name) + safe_extension(original_filename)


def virtual_upload_path(input_name: str, original_filename: str) -> str:
    """The virtual mount path an agent sees, e.g. /mnt/user-data/uploads/doc.pdf."""
    return f"{VIRTUAL_UPLOADS}/{stored_name(input_name, original_filename)}"


def check_size(nbytes: int, limit: int) -> None:
    if limit and nbytes > limit:
        raise UploadTooLarge(f"file is {nbytes} bytes; limit is {limit}")


def check_extension(original_filename: str, allowed: list[str]) -> None:
    if not allowed:
        return
    ext = safe_extension(original_filename).lstrip(".")
    allow = {a.lower().lstrip(".") for a in allowed}
    if ext not in allow:
        raise UploadTypeNotAllowed(
            f"file type '.{ext or '(none)'}' not allowed; allowed: {', '.join(sorted(allow))}"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_workflow_uploads.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/atom/workflow/uploads.py tests/test_workflow_uploads.py
git commit -m "feat(workflow): pure upload helpers (deterministic naming + limit checks)"
```

---

### Task 3: Schema — `InputDef.type` + `resolve_inputs` file branch

**Files:**
- Modify: `src/atom/workflow/schema.py` (`InputDef`, `resolve_inputs`)
- Test: `tests/test_workflow_schema.py` (append)

**Interfaces:**
- Produces: `InputDef.type: Literal["text", "file"] = "text"`; `resolve_inputs` skips text `default` for file inputs.
- Consumes: nothing new (`Literal` already imported in `schema.py`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_workflow_schema.py`:

```python
FILE_DEMO = """
name: filedemo
inputs:
  - name: document
    type: file
    required: true
  - name: notes
    type: file
    required: false
steps:
  - title: Read
    tasks:
      - id: t1
        prompt: "summarize {{ document }}"
"""


def test_input_type_parses_file_and_defaults_text(atom_home):
    _write(atom_home, "filedemo", FILE_DEMO)
    wf = load_workflow("filedemo", str(atom_home))
    by_name = {i.name: i for i in wf.inputs}
    assert by_name["document"].type == "file"
    assert by_name["notes"].type == "file"
    # a workflow without a type: field stays text
    _write(atom_home, "demo", DEMO)
    wf2 = load_workflow("demo", str(atom_home))
    assert all(i.type == "text" for i in wf2.inputs)


def test_resolve_inputs_required_file_missing_raises(atom_home):
    _write(atom_home, "filedemo", FILE_DEMO)
    wf = load_workflow("filedemo", str(atom_home))
    with pytest.raises(MissingInputError):
        resolve_inputs(wf, {})                       # required file 'document' absent


def test_resolve_inputs_file_path_used_optional_blank(atom_home):
    _write(atom_home, "filedemo", FILE_DEMO)
    wf = load_workflow("filedemo", str(atom_home))
    resolved = resolve_inputs(wf, {"document": "/mnt/user-data/uploads/document.pdf"})
    assert resolved["document"] == "/mnt/user-data/uploads/document.pdf"
    assert resolved["notes"] == ""                   # optional file not provided -> ""


def test_resolve_inputs_ignores_text_default_for_file_input():
    wf = WorkflowDef.model_validate({
        "name": "w",
        "inputs": [{"name": "doc", "type": "file", "required": True, "default": "ignored.txt"}],
        "steps": [{"title": "s", "tasks": [{"prompt": "{{ doc }}"}]}],
    })
    with pytest.raises(MissingInputError):        # default must NOT satisfy a required file input
        resolve_inputs(wf, {})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_workflow_schema.py::test_input_type_parses_file_and_defaults_text -v`
Expected: FAIL with `AttributeError: 'InputDef' object has no attribute 'type'`

- [ ] **Step 3: Implement**

In `src/atom/workflow/schema.py`, change `InputDef` (lines 19-23) to:

```python
class InputDef(_Base):
    name: str
    type: Literal["text", "file"] = "text"
    required: bool = False
    description: Optional[str] = None
    default: Optional[str] = None
```

In `resolve_inputs` (lines 109-124), change the `elif inp.default` branch to skip the default for file inputs:

```python
        if inp.name in provided and provided[inp.name] is not None and str(provided[inp.name]).strip() != "":
            resolved[inp.name] = provided[inp.name]
        elif inp.type != "file" and inp.default is not None:   # a text default is meaningless for a file input
            resolved[inp.name] = inp.default
        elif inp.required:
            missing.append(inp.name)
        else:
            resolved[inp.name] = ""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_workflow_schema.py -v`
Expected: PASS (all tests, including the pre-existing ones)

- [ ] **Step 5: Commit**

```bash
git add src/atom/workflow/schema.py tests/test_workflow_schema.py
git commit -m "feat(workflow): InputDef.type=file + resolve_inputs file semantics"
```

---

### Task 4: RunStore — uploads dir, `uploads_path`, `save_upload`

**Files:**
- Modify: `src/atom/workflow/run_store.py` (`RunManifest`, `RunStore`)
- Test: `tests/test_workflow_run_store.py` (append)

**Interfaces:**
- Consumes: `atom.workflow.uploads.stored_name`, `virtual_upload_path` (Task 2).
- Produces: `RunManifest.uploads_path: Optional[str] = None`; `RunStore.uploads_dir(run_id) -> Path`; `RunStore.save_upload(run_id, input_name, original_filename, data: bytes) -> str` (returns the virtual path).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_workflow_run_store.py`:

```python
def test_uploads_dir_created_by_create(atom_home):
    store = RunStore(str(atom_home))
    store.create(_manifest("ru", store.workspace_dir("ru")))
    assert store.uploads_dir("ru") == store.run_dir("ru") / "uploads"
    assert store.uploads_dir("ru").is_dir()


def test_save_upload_deterministic_name_and_virtual_path(atom_home):
    store = RunStore(str(atom_home))
    store.create(_manifest("rup", store.workspace_dir("rup")))
    vpath = store.save_upload("rup", "document", "q3-results.pdf", b"PDFDATA")
    assert vpath == "/mnt/user-data/uploads/document.pdf"
    assert (store.uploads_dir("rup") / "document.pdf").read_bytes() == b"PDFDATA"


def test_manifest_uploads_path_roundtrips(atom_home):
    store = RunStore(str(atom_home))
    m = _manifest("rpp", store.workspace_dir("rpp"))
    m.uploads_path = str(store.uploads_dir("rpp"))
    store.create(m)
    assert store.load("rpp").uploads_path == str(store.uploads_dir("rpp"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_workflow_run_store.py::test_uploads_dir_created_by_create -v`
Expected: FAIL with `AttributeError: 'RunStore' object has no attribute 'uploads_dir'`

- [ ] **Step 3: Implement**

In `src/atom/workflow/run_store.py`:

Add the import near the top (after the existing `from atom.sandbox.paths import atom_home` line):

```python
from atom.sandbox.paths import atom_home
from atom.workflow.uploads import stored_name, virtual_upload_path
```

Add `uploads_path` to `RunManifest` (after the `workspace_path: str` line, ~line 53):

```python
    workspace_path: str
    uploads_path: Optional[str] = None
    steps: list[StepState] = Field(default_factory=list)
```

Add `uploads_dir` right after `workspace_dir` (~line 128):

```python
    def uploads_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "uploads"
```

Add the uploads mkdir to `create` (~line 143):

```python
    def create(self, manifest: RunManifest) -> RunManifest:
        self.workspace_dir(manifest.run_id).mkdir(parents=True, exist_ok=True)
        self.uploads_dir(manifest.run_id).mkdir(parents=True, exist_ok=True)
        (self.run_dir(manifest.run_id) / "chats").mkdir(parents=True, exist_ok=True)
        self.save(manifest)
        return manifest
```

Add `save_upload` right after `create` (before `_summary_path`):

```python
    def save_upload(self, run_id: str, input_name: str, original_filename: str, data: bytes) -> str:
        """Write an uploaded file's bytes into the run's uploads dir and return its virtual path.

        The on-disk name comes from uploads.stored_name(input_name, ...) so it is deterministic
        and equals uploads.virtual_upload_path (what the caller stores in RunManifest.inputs).
        """
        d = self.uploads_dir(run_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / stored_name(input_name, original_filename)).write_bytes(data)
        return virtual_upload_path(input_name, original_filename)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_workflow_run_store.py -v`
Expected: PASS (all tests; pre-existing `_manifest`-based tests still pass since `uploads_path` defaults to `None`)

- [ ] **Step 5: Commit**

```bash
git add src/atom/workflow/run_store.py tests/test_workflow_run_store.py
git commit -m "feat(workflow): per-run uploads dir + save_upload + manifest.uploads_path"
```

---

### Task 5: Mount plumbing — paths override + WorkspaceContext + runtime

**Files:**
- Modify: `src/atom/sandbox/paths.py` (`thread_paths`, `thread_paths_from_context`)
- Modify: `src/atom/state.py` (`WorkspaceContext`)
- Modify: `src/atom/runtime.py` (`_build_context`, `run_agent`)
- Test: `tests/test_sandbox.py` (append), `tests/test_runtime_context.py` (create)

**Interfaces:**
- Produces: `thread_paths(..., uploads_override=None)`; `thread_paths_from_context` reads `ctx["uploads_path"]`; `WorkspaceContext["uploads_path"]`; `run_agent(..., uploads: str | None = None)`; `_build_context(..., uploads=None)` sets `"uploads_path"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_sandbox.py`:

```python
def test_thread_paths_uploads_override(atom_home, tmp_path):
    shared = tmp_path / "shared_uploads"
    shared.mkdir()
    tp = thread_paths("u", "tup", uploads_override=str(shared))
    assert tp.uploads == shared.resolve()
    assert tp.virtual_map()["/mnt/user-data/uploads"] == tp.uploads


def test_thread_paths_uploads_default_is_per_thread(atom_home):
    tp = thread_paths("u", "tnormal")
    assert tp.uploads.name == "uploads"
    assert "threads/tnormal" in str(tp.uploads)


def test_sandbox_reads_from_overridden_uploads_mount(atom_home, tmp_path):
    shared = tmp_path / "run_uploads"
    shared.mkdir()
    (shared / "doc.txt").write_text("hello from upload\n")
    sb = LocalSandboxProvider().acquire(thread_paths("u", "tread", uploads_override=str(shared)))
    assert sb.read_text("/mnt/user-data/uploads/doc.txt") == "hello from upload\n"
```

Create `tests/test_runtime_context.py`:

```python
"""_build_context wires the per-run uploads dir into the WorkspaceContext."""
from __future__ import annotations

from atom.runtime import _build_context

_CAPS = {"supports_vision": False}


def test_build_context_sets_uploads_path(base_config):
    ctx = _build_context(
        base_config, user_id="u", thread_id="t", profile_name="default",
        home="/tmp/h", workspace="new", uploads="/runs/r1/uploads",
        caps=_CAPS, window=1000,
    )
    assert ctx["uploads_path"].endswith("/runs/r1/uploads")


def test_build_context_uploads_none_when_absent(base_config):
    ctx = _build_context(
        base_config, user_id="u", thread_id="t", profile_name="default",
        home="/tmp/h", workspace="new", uploads=None,
        caps=_CAPS, window=1000,
    )
    assert ctx["uploads_path"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_sandbox.py::test_thread_paths_uploads_override tests/test_runtime_context.py -v`
Expected: FAIL (`thread_paths()` has no `uploads_override`; `_build_context()` has no `uploads`)

- [ ] **Step 3: Implement**

In `src/atom/sandbox/paths.py`, change `thread_paths` (lines 78-106) to add the override:

```python
def thread_paths(
    user_id: str,
    thread_id: str,
    *,
    home: str | os.PathLike[str] | None = None,
    workspace_override: str | os.PathLike[str] | None = None,
    uploads_override: str | os.PathLike[str] | None = None,
) -> ThreadPaths:
    """Compute :class:`ThreadPaths` for a thread.

    ``workspace_override`` (an absolute path) binds an *existing* external directory as the
    workspace (reuse mode); otherwise a fresh per-thread ``workspace/`` is used (new mode).
    ``uploads_override`` (an absolute path) binds a shared external ``uploads/`` (per-run
    uploads) instead of the per-thread default. Directories are not created here — call
    :meth:`ThreadPaths.ensure`.
    """
    h = atom_home(home)
    base = h / "users" / user_id / "threads" / thread_id / "user-data"
    external = workspace_override is not None
    workspace = Path(workspace_override).expanduser().resolve() if external else base / "workspace"
    uploads = (
        Path(uploads_override).expanduser().resolve()
        if uploads_override is not None
        else base / "uploads"
    )
    return ThreadPaths(
        home=h,
        user_id=user_id,
        thread_id=thread_id,
        workspace=workspace,
        uploads=uploads,
        outputs=base / "outputs",
        skills=h / "skills",
        skill_library=h / "skill_library",
        tool_library=h / "tool_library",
        workspace_is_external=external,
    )
```

In the same file, change `thread_paths_from_context` (lines 109-118) to pass the override:

```python
def thread_paths_from_context(ctx: dict, home_default: str | None = None) -> ThreadPaths:
    """Build :class:`ThreadPaths` from a WorkspaceContext dict (used by middleware)."""
    mode = ctx.get("workspace_mode", "new")
    override = ctx.get("workspace_path") if mode == "existing" else None
    return thread_paths(
        ctx.get("user_id", "default"),
        ctx["thread_id"],
        home=ctx.get("home") or home_default,
        workspace_override=override,
        uploads_override=ctx.get("uploads_path"),
    )
```

In `src/atom/state.py`, add `uploads_path` to `WorkspaceContext` (after the `workspace_path` line, ~line 68):

```python
    workspace_path: Optional[str]
    # Shared per-run uploads dir bound to /mnt/user-data/uploads (set for workflow runs). When
    # absent, uploads resolves per-thread (chat/subagent runs).
    uploads_path: Optional[str]
```

In `src/atom/runtime.py`, change `_build_context` (lines 44-59) to accept and set uploads:

```python
def _build_context(cfg: AtomConfig, *, user_id, thread_id, profile_name, home, workspace, caps, window, uploads=None) -> WorkspaceContext:
    if workspace in (None, "new"):
        mode, wpath = "new", None
    else:
        mode, wpath = "existing", str(Path(workspace).expanduser().resolve())
    return {
        "user_id": user_id,
        "thread_id": thread_id,
        "profile_name": profile_name,
        "home": home,
        "workspace_mode": mode,
        "workspace_path": wpath,
        "uploads_path": str(Path(uploads).expanduser().resolve()) if uploads else None,
        "allow_bash": cfg.sandbox.bash_enabled,
        "supports_vision": bool(caps.get("supports_vision")),
        "context_window": window,
    }
```

In `run_agent` (lines 74-89), add the `uploads` parameter after `workspace`:

```python
    thread_id: str | None = None,
    workspace: str = "new",
    uploads: str | None = None,
    user_id: str | None = None,
```

And pass it through in the `_build_context(...)` call (lines 102-111):

```python
    context = _build_context(
        cfg,
        user_id=user_id,
        thread_id=thread_id,
        profile_name=profile_name,
        home=home,
        workspace=workspace,
        uploads=uploads,
        caps=prepared.caps,
        window=prepared.context_window,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_sandbox.py tests/test_runtime_context.py -v`
Expected: PASS (all tests; pre-existing sandbox tests unaffected)

- [ ] **Step 5: Commit**

```bash
git add src/atom/sandbox/paths.py src/atom/state.py src/atom/runtime.py tests/test_sandbox.py tests/test_runtime_context.py
git commit -m "feat(sandbox): per-run uploads_override plumbed through run_agent/context"
```

---

### Task 6: Engine — set `uploads_path`, forward `uploads=` to `run_agent`

**Files:**
- Modify: `src/atom/workflow/engine.py` (`create_run`, `_run_task`)
- Test: `tests/test_workflow_engine.py` (append)

**Interfaces:**
- Consumes: `RunStore.uploads_dir` (Task 4); `run_agent(uploads=)` (Task 5).
- Produces: `RunManifest.uploads_path` set to `store.uploads_dir(run_id)`; each task's `run_agent` call receives `uploads=manifest.uploads_path`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_workflow_engine.py` (note: `_one_task_wf` already exists in this file):

```python
@pytest.mark.asyncio
async def test_create_run_sets_uploads_path(base_config, atom_home):
    engine = WorkflowEngine(base_config)
    m = engine.create_run(_one_task_wf(), {"topic": "sea"}, "run_up", "2026-07-15T00:00:00")
    assert m.uploads_path == str(engine.store.uploads_dir("run_up"))
    assert engine.store.load("run_up").uploads_path == str(engine.store.uploads_dir("run_up"))


@pytest.mark.asyncio
async def test_run_task_forwards_uploads_to_run_agent(base_config, atom_home, monkeypatch):
    from atom.runtime import RunResult
    captured = {}

    async def spy(prompt, **kwargs):
        captured["uploads"] = kwargs.get("uploads")
        return RunResult(thread_id=kwargs.get("thread_id", "t"), messages=[], final_text="ok", state={})

    monkeypatch.setattr(engine_mod, "run_agent", spy)
    engine = WorkflowEngine(base_config)
    engine.create_run(_one_task_wf(), {"topic": "sea"}, "run_upfwd", "2026-07-15T00:00:00")
    await engine.execute("run_upfwd")
    assert captured["uploads"] == str(engine.store.uploads_dir("run_upfwd"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_workflow_engine.py::test_create_run_sets_uploads_path -v`
Expected: FAIL with `AssertionError` (`uploads_path` is `None`)

- [ ] **Step 3: Implement**

In `src/atom/workflow/engine.py`, `create_run` (lines 110-114), add `uploads_path`:

```python
        manifest = RunManifest(
            run_id=run_id, workflow=workflow.name, inputs=resolved,
            created_at=created_at or _now(),
            workspace_path=str(self.store.workspace_dir(run_id)),
            uploads_path=str(self.store.uploads_dir(run_id)),
            steps=steps,
        )
```

In `_run_task`, the `run_agent(...)` call (lines 394-400), add `uploads=`:

```python
            coro = run_agent(
                prompt, config=self._task_cfg, profile=self.profile,
                override_model=td.model, override_thinking=td.thinking,
                workspace=manifest.workspace_path, uploads=manifest.uploads_path,
                thread_id=ts.thread_id, trace=trace, prepared=prepared,
                notes=notes.as_prompt_ctx() if notes else None,
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_workflow_engine.py -v`
Expected: PASS (all tests, including pre-existing)

- [ ] **Step 5: Commit**

```bash
git add src/atom/workflow/engine.py tests/test_workflow_engine.py
git commit -m "feat(workflow): engine sets uploads_path + forwards it to each task"
```

---

### Task 7: API — multipart submit (JSON preserved) + limits

**Files:**
- Modify: `pyproject.toml` (add `python-multipart`)
- Modify: `src/atom/api/app.py` (`submit_run` → content-type dispatch; helpers)
- Test: `tests/test_workflow_api.py` (append)

**Interfaces:**
- Consumes: `UploadsConfig` (Task 1); `uploads.check_size/check_extension/virtual_upload_path`, `UploadTooLarge`, `UploadTypeNotAllowed` (Task 2); `RunStore.save_upload` (Task 4); `resolve_inputs` file semantics (Task 3); `create_run` uploads_path (Task 6).
- Produces: `POST /api/runs` accepts `multipart/form-data` (`workflow` field, `inputs` JSON field, file parts keyed by input name) and `application/json` (unchanged). Errors: undeclared file field → 400; missing required → 422; oversize → 413; disallowed type → 415.

- [ ] **Step 1: Add the dependency and install it**

In `pyproject.toml`, add to `[project].dependencies` under the FastAPI section:

```toml
    # --- Workflow API (FastAPI automation surface) ---
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "python-multipart>=0.0.9",
```

Run: `.venv/bin/python -m pip install -e ".[dev]"`
Expected: installs `python-multipart` (or reports it already satisfied).

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_workflow_api.py`:

```python
def _seed_filewf(home):
    d = home / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    (d / "docwf.yaml").write_text(
        "name: docwf\n"
        "inputs:\n"
        "  - name: doc\n    type: file\n    required: true\n"
        "  - name: extra\n    type: file\n    required: false\n"
        "steps:\n  - title: Read\n    tasks:\n      - id: t1\n        prompt: \"summarize {{ doc }}\"\n"
    )


def _reader_provider(td, sd, wf):
    return make_prepared([
        AIMessage(content="", tool_calls=[{
            "name": "read_file",
            "args": {"description": "r", "path": "/mnt/user-data/uploads/doc.txt"},
            "id": "c1", "type": "tool_call"}]),
        AIMessage(content="done"),
    ])


@pytest.mark.asyncio
async def test_multipart_upload_lands_and_run_completes(base_config, atom_home):
    _seed_filewf(atom_home)
    engine = WorkflowEngine(base_config, prepared_provider=_reader_provider)
    app = create_app(base_config, engine=engine)
    async with _client(app) as client:
        r = await client.post(
            "/api/runs",
            data={"workflow": "docwf", "inputs": "{}"},
            files={"doc": ("report.txt", b"the tide returns\n", "text/plain")},
        )
        assert r.status_code == 202
        run_id = r.json()["run_id"]
        manifest = await _poll(client, run_id)
        assert manifest["status"] == "complete"
        assert manifest["inputs"]["doc"] == "/mnt/user-data/uploads/doc.txt"
        assert (engine.store.uploads_dir(run_id) / "doc.txt").read_bytes() == b"the tide returns\n"
        msgs = (await client.get(f"/api/runs/{run_id}/tasks/0/t1/messages")).json()
        assert any("the tide returns" in m["text"] for m in msgs if m["role"] == "tool")


@pytest.mark.asyncio
async def test_json_submit_still_works(base_config, atom_home):
    _seed(atom_home)
    engine = WorkflowEngine(base_config, prepared_provider=_provider)
    app = create_app(base_config, engine=engine)
    async with _client(app) as client:
        r = await client.post("/api/runs", json={"workflow": "demo", "inputs": {"topic": "x"}})
        assert r.status_code == 202 and r.json()["status"] == "queued"


@pytest.mark.asyncio
async def test_multipart_undeclared_file_field_is_400(base_config, atom_home):
    _seed_filewf(atom_home)
    app = create_app(base_config, engine=WorkflowEngine(base_config, prepared_provider=_reader_provider))
    async with _client(app) as client:
        r = await client.post("/api/runs", data={"workflow": "docwf", "inputs": "{}"},
                              files={"ghost": ("x.txt", b"x", "text/plain")})
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_multipart_missing_required_file_is_422(base_config, atom_home):
    _seed_filewf(atom_home)
    app = create_app(base_config, engine=WorkflowEngine(base_config, prepared_provider=_reader_provider))
    async with _client(app) as client:
        # send only the optional 'extra' file -> required 'doc' absent
        r = await client.post("/api/runs", data={"workflow": "docwf", "inputs": "{}"},
                              files={"extra": ("notes.txt", b"x", "text/plain")})
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_multipart_oversize_is_413(base_config, atom_home):
    _seed_filewf(atom_home)
    base_config.uploads.max_file_bytes = 5
    app = create_app(base_config, engine=WorkflowEngine(base_config, prepared_provider=_reader_provider))
    async with _client(app) as client:
        r = await client.post("/api/runs", data={"workflow": "docwf", "inputs": "{}"},
                              files={"doc": ("report.txt", b"way too big", "text/plain")})
        assert r.status_code == 413


@pytest.mark.asyncio
async def test_multipart_disallowed_extension_is_415(base_config, atom_home):
    _seed_filewf(atom_home)
    base_config.uploads.allowed_extensions = ["pdf"]
    app = create_app(base_config, engine=WorkflowEngine(base_config, prepared_provider=_reader_provider))
    async with _client(app) as client:
        r = await client.post("/api/runs", data={"workflow": "docwf", "inputs": "{}"},
                              files={"doc": ("report.txt", b"hello", "text/plain")})
        assert r.status_code == 415
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_workflow_api.py::test_multipart_upload_lands_and_run_completes -v`
Expected: FAIL (the JSON-only `submit_run` returns 422/error on a multipart body)

- [ ] **Step 4: Implement**

In `src/atom/api/app.py`, update the imports (lines 8-21 region):

```python
import datetime
import json
import mimetypes
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from atom.api.models import ExportRequest, RunRequest
from atom.config import load_config
from atom.config.schema import AtomConfig
from atom.workflow.engine import WorkflowEngine
from atom.workflow.schema import MissingInputError, list_workflows, load_workflow
from atom.workflow.uploads import (
    UploadTooLarge, UploadTypeNotAllowed, check_extension, check_size, virtual_upload_path,
)
```

Replace the whole `submit_run` handler (lines 69-81) with a content-type dispatcher plus two closure helpers:

```python
    def _create_and_enqueue(wf, inputs: dict, files: dict) -> dict:
        # files: {input_name: (original_filename, data_bytes)}
        run_id = uuid.uuid4().hex[:12]
        merged = dict(inputs)
        for name, (filename, _data) in files.items():
            merged[name] = virtual_upload_path(name, filename)
        try:
            engine.create_run(wf, merged, run_id, _now())
        except MissingInputError as exc:
            raise HTTPException(422, str(exc))
        for name, (filename, data) in files.items():
            store.save_upload(run_id, name, filename, data)
        engine.enqueue(run_id)
        return {"run_id": run_id, "status": "queued"}

    async def _submit_multipart(request: Request) -> dict:
        form = await request.form()
        workflow_name = form.get("workflow")
        if not workflow_name:
            raise HTTPException(422, "missing 'workflow' field")
        raw_inputs = form.get("inputs")
        try:
            text_inputs = json.loads(raw_inputs) if raw_inputs else {}
        except json.JSONDecodeError:
            raise HTTPException(422, "'inputs' must be a JSON object")
        if not isinstance(text_inputs, dict):
            raise HTTPException(422, "'inputs' must be a JSON object")
        try:
            wf = load_workflow(workflow_name, cfg.home)
        except FileNotFoundError:
            raise HTTPException(404, f"workflow '{workflow_name}' not found")
        file_input_names = {i.name for i in wf.inputs if i.type == "file"}

        files: dict = {}
        for key, value in form.multi_items():
            if not isinstance(value, UploadFile):
                continue
            if key not in file_input_names:
                raise HTTPException(400, f"'{key}' is not a declared file input of workflow '{workflow_name}'")
            if key in files:
                raise HTTPException(400, f"multiple files supplied for input '{key}'")
            data = await value.read()
            try:
                check_size(len(data), cfg.uploads.max_file_bytes)
                check_extension(value.filename or "", cfg.uploads.allowed_extensions)
            except UploadTooLarge as exc:
                raise HTTPException(413, str(exc))
            except UploadTypeNotAllowed as exc:
                raise HTTPException(415, str(exc))
            files[key] = (value.filename or key, data)
        if len(files) > cfg.uploads.max_files_per_run:
            raise HTTPException(413, f"too many files ({len(files)} > {cfg.uploads.max_files_per_run})")
        return _create_and_enqueue(wf, text_inputs, files)

    @app.post("/api/runs", status_code=202)
    async def submit_run(request: Request) -> dict:
        ctype = request.headers.get("content-type", "")
        if ctype.startswith("multipart/form-data"):
            return await _submit_multipart(request)
        # JSON path (backward-compatible).
        try:
            body = await request.json()
            req = RunRequest.model_validate(body)
        except Exception:  # noqa: BLE001 — malformed JSON body -> 422 like FastAPI's auto-validation
            raise HTTPException(422, "invalid JSON body for RunRequest")
        try:
            wf = load_workflow(req.workflow, cfg.home)
        except FileNotFoundError:
            raise HTTPException(404, f"workflow '{req.workflow}' not found")
        return _create_and_enqueue(wf, req.inputs, {})
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_workflow_api.py -v`
Expected: PASS (all tests, including the pre-existing JSON ones)

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/atom/api/app.py tests/test_workflow_api.py
git commit -m "feat(api): one-step multipart uploads on POST /api/runs (JSON preserved)"
```

---

### Task 8: CLI — `workflow run --file NAME=PATH`

**Files:**
- Modify: `src/atom/cli.py` (`workflow_run`)
- Test: `tests/test_workflow_cli.py` (append)

**Interfaces:**
- Consumes: `uploads.check_size/check_extension/virtual_upload_path` + exceptions (Task 2); `RunStore.save_upload` (Task 4); `UploadsConfig` (Task 1); file-input schema (Task 3).
- Produces: `--file / -f NAME=PATH` repeatable option; persists the local file into the run's uploads dir and sets `inputs[name]` to its virtual path.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_workflow_cli.py`:

```python
def _seed_filewf(home):
    d = home / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    (d / "docwf.yaml").write_text(
        "name: docwf\n"
        "inputs:\n  - name: doc\n    type: file\n    required: true\n"
        "steps:\n  - title: Read\n    tasks:\n      - id: t1\n        prompt: \"summarize {{ doc }}\"\n"
    )


def _patch_fake_agent(monkeypatch):
    import atom.workflow.engine as engine_mod
    from atom.runtime import RunResult
    from langchain_core.messages import AIMessage

    async def fake_run_agent(prompt, **kwargs):
        return RunResult(thread_id=kwargs.get("thread_id", "t"),
                         messages=[AIMessage(content="did it")], final_text="did it", state={})
    monkeypatch.setattr(engine_mod, "run_agent", fake_run_agent)


def test_workflow_run_with_file_persists_and_resolves(atom_home, tmp_path, monkeypatch):
    _seed_filewf(atom_home)
    _patch_fake_agent(monkeypatch)
    src = tmp_path / "report.txt"
    src.write_text("hello\n")

    result = runner.invoke(app, ["workflow", "run", "docwf", "--file", f"doc={src}"])
    assert result.exit_code == 0, result.stdout

    from atom.workflow.run_store import RunStore
    store = RunStore(str(atom_home))
    runs = store.list()
    assert len(runs) == 1
    m = runs[0]
    assert m.inputs["doc"] == "/mnt/user-data/uploads/doc.txt"
    assert (store.uploads_dir(m.run_id) / "doc.txt").read_bytes() == b"hello\n"


def test_workflow_run_file_undeclared_input_errors(atom_home, tmp_path):
    _seed_filewf(atom_home)
    src = tmp_path / "x.txt"; src.write_text("x")
    result = runner.invoke(app, ["workflow", "run", "docwf", "--file", f"ghost={src}"])
    assert result.exit_code != 0
    assert "ghost" in result.stdout


def test_workflow_run_missing_required_file_errors(atom_home):
    _seed_filewf(atom_home)
    result = runner.invoke(app, ["workflow", "run", "docwf"])   # required file 'doc' not provided
    assert result.exit_code != 0
    assert "doc" in result.stdout or "missing" in result.stdout.lower()


def test_workflow_run_malformed_file_token_errors(atom_home):
    _seed_filewf(atom_home)
    result = runner.invoke(app, ["workflow", "run", "docwf", "--file", "doc"])  # missing =path
    assert result.exit_code != 0
    assert "NAME=PATH" in result.stdout or "=" in result.stdout
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_workflow_cli.py::test_workflow_run_with_file_persists_and_resolves -v`
Expected: FAIL (`--file` is not a recognized option → non-zero exit)

- [ ] **Step 3: Implement**

In `src/atom/cli.py`, add the `file` option to `workflow_run` (after the `input` option, line 163):

```python
    input: list[str] = typer.Option(None, "--input", "-i", help="key=value (repeatable)."),
    file: list[str] = typer.Option(None, "--file", "-f", help="name=path for a file input (repeatable)."),
    profile: str = typer.Option(None, "--profile", "-p"),
```

Inside `workflow_run`, extend the imports block (lines 170-171):

```python
    from atom.workflow.engine import WorkflowEngine
    from atom.workflow.schema import load_workflow, MissingInputError
    from atom.workflow.uploads import (
        UploadTooLarge, UploadTypeNotAllowed, check_extension, check_size, virtual_upload_path,
    )
    from pathlib import Path
```

After the existing `--input` malformed-token check (lines 176-180) and after `cfg = load_config(config)` + `wf = load_workflow(...)` (through line 188), insert file parsing + staging just before the `inputs = dict(...)` line (line 190). Replace lines 190-199 with:

```python
    inputs = dict(kv.split("=", 1) for kv in (input or []) if "=" in kv)

    # Parse + stage --file NAME=PATH tokens (bytes read now; written after the run dir exists).
    file_input_names = {i.name for i in wf.inputs if i.type == "file"}
    staged: dict[str, tuple[str, bytes]] = {}
    for token in (file or []):
        if "=" not in token:
            console.print(f"[red]Error: --file must be NAME=PATH, got: {token}[/red]")
            raise typer.Exit(1)
        fname, fpath = token.split("=", 1)
        p = Path(fpath).expanduser()
        if fname not in file_input_names:
            console.print(f"[red]Error: '{fname}' is not a file input of workflow '{name}'[/red]")
            raise typer.Exit(1)
        if not p.is_file():
            console.print(f"[red]Error: file not found: {p}[/red]")
            raise typer.Exit(1)
        data = p.read_bytes()
        try:
            check_size(len(data), cfg.uploads.max_file_bytes)
            check_extension(p.name, cfg.uploads.allowed_extensions)
        except (UploadTooLarge, UploadTypeNotAllowed) as e:
            console.print(f"[red]Error: {e}[/red]")
            raise typer.Exit(1)
        staged[fname] = (p.name, data)
        inputs[fname] = virtual_upload_path(fname, p.name)

    engine = WorkflowEngine(cfg, profile=profile)
    run_id = uuid.uuid4().hex[:12]

    try:
        engine.create_run(wf, inputs, run_id, datetime.datetime.now().isoformat(timespec="seconds"))
    except MissingInputError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)

    for fname, (orig, data) in staged.items():
        engine.store.save_upload(run_id, fname, orig, data)

    engine.enqueue(run_id)
```

(The remainder of `workflow_run` — the `console.status` block and artifact listing — is unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_workflow_cli.py -v`
Expected: PASS (all tests, including pre-existing)

- [ ] **Step 5: Commit**

```bash
git add src/atom/cli.py tests/test_workflow_cli.py
git commit -m "feat(cli): workflow run --file NAME=PATH parity with the API"
```

---

### Task 9: UI — file input rendering + FormData submit

**Files:**
- Modify: `atom-ui/src/api.ts` (`InputDef.type`, `api.submit`)
- Modify: `atom-ui/src/Workflows.tsx` (`RunForm` file input + files state)

**Interfaces:**
- Consumes: the `type` field now present on `GET /api/workflows` inputs (Task 3), and the multipart `POST /api/runs` (Task 7).
- Produces: `api.submit(workflow, inputs, files?)` — multipart when files are present, JSON otherwise.

- [ ] **Step 1: Implement the api.ts changes**

In `atom-ui/src/api.ts`, change the `InputDef` interface (line 1):

```ts
export interface InputDef { name: string; type?: "text" | "file"; required: boolean; description?: string; default?: string; }
```

Replace `api.submit` (lines 35-39) with:

```ts
  submit: (
    workflow: string,
    inputs: Record<string, string>,
    files?: Record<string, File>,
  ): Promise<{ run_id: string }> => {
    const fileEntries = files ? Object.entries(files) : [];
    if (fileEntries.length === 0) {
      return fetch("/api/runs", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workflow, inputs }),
      }).then(j);
    }
    const fd = new FormData();
    fd.append("workflow", workflow);
    fd.append("inputs", JSON.stringify(inputs));
    for (const [name, file] of fileEntries) fd.append(name, file);
    return fetch("/api/runs", { method: "POST", body: fd }).then(j);  // browser sets multipart boundary
  },
```

- [ ] **Step 2: Implement the Workflows.tsx changes**

In `atom-ui/src/Workflows.tsx`, in `RunForm`, add a `files` state next to `values` (line 31) and pass it to `submit` (line 36):

```tsx
  const [values, setValues] = useState<Record<string, string>>({});
  const [files, setFiles] = useState<Record<string, File>>({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setError(""); setBusy(true);
    try { const { run_id } = await api.submit(workflow.name, values, files); onStarted(run_id); }
    catch (e) { setError(String(e instanceof Error ? e.message : e)); setBusy(false); }
  };
```

Replace the input rendering (lines 44-51) so file inputs render a file picker:

```tsx
      {workflow.inputs.map((i) => (
        <label key={i.name} className="field">
          <span className="field-label">{i.name}{i.required && <span className="req">required</span>}</span>
          {i.description && <span className="field-hint">{i.description}</span>}
          {i.type === "file" ? (
            <input type="file"
              onChange={(e) => {
                const f = e.target.files?.[0];
                setFiles((prev) => {
                  const next = { ...prev };
                  if (f) next[i.name] = f; else delete next[i.name];
                  return next;
                });
              }} />
          ) : (
            <input placeholder={i.default ?? ""} value={values[i.name] ?? ""}
              onChange={(e) => setValues((v) => ({ ...v, [i.name]: e.target.value }))} />
          )}
        </label>
      ))}
```

- [ ] **Step 3: Typecheck / build to verify (no unit-test harness exists)**

Run:
```bash
cd atom-ui && npm install >/dev/null 2>&1; npm run build
```
Expected: build succeeds (TypeScript compiles with no errors, Vite emits `dist/`).

- [ ] **Step 4: Commit**

```bash
git add atom-ui/src/api.ts atom-ui/src/Workflows.tsx
git commit -m "feat(ui): file-input picker + multipart submit for workflow runs"
```

---

### Task 10: Example workflow + end-to-end integration

**Files:**
- Create: `workflows/summarize-doc.yaml`
- Test: `tests/test_workflow_uploads_e2e.py` (create)

**Interfaces:**
- Consumes: everything above. Proves `{{ name }}` resolves to the uploads path and the agent reads the file from the shared mount end-to-end (no HTTP).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_workflow_uploads_e2e.py`:

```python
"""End-to-end: an uploaded file is shared across a run and readable from the {{ uploads }} mount."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from langchain_core.messages import AIMessage

import atom.workflow.engine as engine_mod
from atom.workflow.engine import WorkflowEngine
from atom.workflow.schema import WorkflowDef
from tests.conftest import make_prepared

UP = "/mnt/user-data/uploads"


def test_example_summarize_doc_workflow_valid():
    data = yaml.safe_load(Path("workflows/summarize-doc.yaml").read_text())
    wf = WorkflowDef.model_validate(data)
    assert wf.name == "summarize-doc"
    doc = next(i for i in wf.inputs if i.name == "document")
    assert doc.type == "file" and doc.required is True


def _file_wf() -> WorkflowDef:
    return WorkflowDef.model_validate({
        "name": "docwf",
        "inputs": [{"name": "document", "type": "file", "required": True}],
        "steps": [{"title": "Read", "tasks": [{"id": "t1", "prompt": "read {{ document }}"}]}],
    })


@pytest.mark.asyncio
async def test_uploaded_file_readable_from_mount_and_path_resolved(base_config, atom_home, monkeypatch):
    captured = {}
    real = engine_mod.run_agent

    async def spy(prompt, **kwargs):
        captured["prompt"] = prompt
        return await real(prompt, **kwargs)

    monkeypatch.setattr(engine_mod, "run_agent", spy)

    def provider(td, sd, wf):
        return make_prepared([
            AIMessage(content="", tool_calls=[{
                "name": "read_file",
                "args": {"description": "r", "path": f"{UP}/document.txt"},
                "id": "c1", "type": "tool_call"}]),
            AIMessage(content="done"),
        ])

    engine = WorkflowEngine(base_config, prepared_provider=provider)
    run_id = "run_e2e"
    engine.create_run(_file_wf(), {"document": f"{UP}/document.txt"}, run_id, "2026-07-15T00:00:00")
    engine.store.save_upload(run_id, "document", "myreport.txt", b"the tide returns\n")

    manifest = await engine.execute(run_id)

    assert manifest.status == "complete"
    assert f"{UP}/document.txt" in captured["prompt"]          # {{ document }} resolved to the mount path
    chat = engine.store.load_chat(run_id, 0, "t1")
    tool_texts = "\n".join(m["text"] for m in chat if m["role"] == "tool")
    assert "the tide returns" in tool_texts                    # agent read the file from the shared mount
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_workflow_uploads_e2e.py -v`
Expected: FAIL — `test_example_summarize_doc_workflow_valid` fails with `FileNotFoundError` (the example YAML does not exist yet).

- [ ] **Step 3: Create the example workflow**

Create `workflows/summarize-doc.yaml`:

```yaml
name: summarize-doc
description: Summarize an uploaded document.
inputs:
  - name: document
    type: file
    required: true
    description: The document to summarize (a text file).
steps:
  - title: Summarize
    tasks:
      - id: summarizer
        prompt: |
          Read the uploaded document at {{ document }}, then write a concise summary to
          {{ outputs }}/summary.md and present it with present_files.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_workflow_uploads_e2e.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Run the full suite (regression gate)**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS (the entire suite green — pre-existing + new).

- [ ] **Step 6: Commit**

```bash
git add workflows/summarize-doc.yaml tests/test_workflow_uploads_e2e.py
git commit -m "feat(workflow): summarize-doc example + end-to-end upload mount test"
```

---

## Notes for the executor

- **README:** after Task 10, add a short "File inputs" subsection to the workflow docs in `README.md` (how to declare `type: file`, the `--file` flag, and that `{{ name }}` resolves to `/mnt/user-data/uploads/<name>.<ext>`). Fold this into the Task 10 commit or a trailing docs commit; it is documentation, not a code task, so it has no test.
- **Branch:** all work lands on `feat/workflow-file-uploads` (already created; the spec commit is its first commit).
- **Do not** add uploads to `sandbox.allowed_workspace_roots` — the uploads mount is confined independently (see Global Constraints).
