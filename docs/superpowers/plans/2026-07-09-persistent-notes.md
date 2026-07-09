# Persistent Notes + Skill Catalog/Load Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in per-workflow persistent Logseq vault (shared across runs) and rework skill surfacing into a lightweight always-on catalog + explicit `load_skill`.

**Architecture:** Skills in `$ATOM_HOME/skills/` are auto-discovered and injected into every lead + sub-agent prompt as a name+description catalog; `search_skills` becomes discovery-only; a new `load_skill(name)` tool is the sole loader (reusing `promoted_skills` + `SkillLibraryMiddleware`). Notes are workflow-only: a `notes:` block on the workflow triggers `ensure_vault` once per run (a Logseq graph at `$ATOM_HOME/notes/<slug>/`), and a binding is threaded through `run_agent → build_lead_agent → lead_system.md` as a gated snippet.

**Tech Stack:** Python 3.11, LangChain v1 (`create_agent`, `AgentMiddleware`), Pydantic, Jinja2 (StrictUndefined), Logseq CLI, pytest.

## Global Constraints

- Run the test suite with **`.venv/bin/python -m pytest`** (NOT bare `.venv/bin/pytest` — collection fails on `from tests.conftest import ...`).
- Tool names follow **verb_noun** (`load_skill` is correct).
- Persistent notes are **workflow-only** (never `atom run`). The skill catalog/load change IS general (reaches `atom run` and sub-agents).
- Prompts render under **Jinja2 `StrictUndefined`**: any variable a template references MUST always be present in the render ctx (default to `None`/`[]`), or rendering raises.
- The `logseq` CLI must be installed and on `PATH` for persistent-notes workflows (guaranteed prerequisite).
- `RunManifest` has no run-level `error` field; surface run-setup failures on a task's `error`.
- DRY, YAGNI, TDD, frequent commits. One commit per task.

## File Structure

- `src/atom/library.py` — add `load_skill_catalog(home, extra_names)`.
- `src/atom/tools/search.py` — `search_skills` → discovery-only; add `load_skill`.
- `src/atom/agent.py` — catalog wiring, `load_skill` tool, middleware gating, `notes` param.
- `src/atom/prompts/lead_system.md` — catalog block (replaces body block) + notes block.
- `src/atom/subagent.py` — `skill_catalog`/`has_skill_library` fields, ctx, child tools + `SkillLibraryMiddleware`.
- `src/atom/prompts/subagent_general.md`, `subagent_bash.md` — catalog block.
- `src/atom/workflow/schema.py` — `NotesConfig` + `WorkflowDef.notes`.
- `src/atom/notes.py` — **new**: `NotesBinding`, `_slug`, `notes_root`, `ensure_vault`.
- `src/atom/runtime.py` — `run_agent(..., notes=None)`.
- `src/atom/workflow/engine.py` — `ensure_vault` once per run + forward `notes`.
- `workflows/notes-smoke.yaml` — **new** test workflow.
- `README.md` — prerequisites + notes docs.
- Tests: `tests/test_library.py`, `tests/test_search.py` (new), `tests/test_prompts.py`, `tests/test_subagent.py`, `tests/test_workflow_schema.py`, `tests/test_notes.py` (new), `tests/test_workflow_engine.py`.

---

### Task 1: `load_skill_catalog` helper

**Files:**
- Modify: `src/atom/library.py` (add function after `load_named_skills`)
- Test: `tests/test_library.py`

**Interfaces:**
- Produces: `load_skill_catalog(home: Path | str, extra_names: list[str]) -> list[SkillEntry]` — all skills in `<home>/skills/` plus any `extra_names` (from `skills.frequent`) not already present, deduped by name.

- [ ] **Step 1: Write the failing test** — append to `tests/test_library.py`:

```python
def test_load_skill_catalog_autodiscovers_skills_folder(atom_home):
    from atom.library import load_skill_catalog

    d = atom_home / "skills" / "logseq-cli"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: logseq-cli\ndescription: Operate the Logseq CLI\n---\nFULL BODY HERE"
    )
    catalog = load_skill_catalog(atom_home, [])
    assert [e.name for e in catalog] == ["logseq-cli"]
    assert catalog[0].description == "Operate the Logseq CLI"


def test_load_skill_catalog_adds_library_extras_and_dedupes(atom_home):
    from atom.library import load_skill_catalog

    (atom_home / "skills" / "a").mkdir(parents=True)
    (atom_home / "skills" / "a" / "SKILL.md").write_text(
        "---\nname: a\ndescription: skill a\n---\nBODY"
    )
    (atom_home / "skill_library" / "b").mkdir(parents=True)
    (atom_home / "skill_library" / "b" / "SKILL.md").write_text(
        "---\nname: b\ndescription: skill b\n---\nBODY"
    )
    names = [e.name for e in load_skill_catalog(atom_home, ["b", "a"])]
    assert names == ["a", "b"]  # a from folder; b pulled from library; a not duplicated
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_library.py::test_load_skill_catalog_autodiscovers_skills_folder -v`
Expected: FAIL with `ImportError: cannot import name 'load_skill_catalog'`.

- [ ] **Step 3: Write minimal implementation** — in `src/atom/library.py`, after `load_named_skills` (around line 264):

```python
def load_skill_catalog(home: Path | str, extra_names: list[str]) -> list[SkillEntry]:
    """Always-on catalog: every skill in ``skills/`` plus any ``extra_names`` (from a profile's
    ``skills.frequent``) not already present. Deduped by name; only name/description are surfaced."""
    home = Path(home)
    entries = load_skill_entries(home / "skills")
    have = {e.name for e in entries}
    for entry in load_named_skills(home, extra_names):
        if entry.name not in have:
            entries.append(entry)
            have.add(entry.name)
    return entries
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_library.py -v`
Expected: PASS (both new tests + existing).

- [ ] **Step 5: Commit**

```bash
git add src/atom/library.py tests/test_library.py
git commit -m "feat(skills): load_skill_catalog auto-discovers the skills/ folder"
```

---

### Task 2: `search_skills` discovery-only + new `load_skill` tool

**Files:**
- Modify: `src/atom/tools/search.py`
- Test: `tests/test_search.py` (new)

**Interfaces:**
- Consumes: `atom.library.get_index`, `atom.tools.search._home`.
- Produces:
  - `search_skills(runtime, query, max_results=3) -> Command` — returns a name+description listing of `skill_library/` matches; does NOT mutate `promoted_skills`.
  - `load_skill(runtime, name) -> Command` — validates `name` on disk (`skills/` then `skill_library/`), merges it into `promoted_skills`; rejects unknown/traversal names.

- [ ] **Step 1: Write the failing test** — create `tests/test_search.py`:

```python
"""search_skills is discovery-only; load_skill is the sole loader."""
from __future__ import annotations

from types import SimpleNamespace

from atom.library import load_library, register_index
from atom.tools.search import load_skill, search_skills
from tests.conftest import seed_library


def _runtime(home, state=None):
    return SimpleNamespace(context={"home": str(home)}, state=state or {}, tool_call_id="tc1")


def test_search_skills_lists_and_does_not_promote(atom_home):
    seed_library(atom_home)  # adds skill_library/pdf-extract
    register_index(str(atom_home), load_library(str(atom_home)))
    cmd = search_skills.func(_runtime(atom_home), query="extract text from a pdf")
    assert "promoted_skills" not in cmd.update              # discovery only
    msg = cmd.update["messages"][0].content
    assert "pdf-extract" in msg and "load_skill" in msg


def test_load_skill_promotes_known_skill(atom_home):
    d = atom_home / "skills" / "logseq-cli"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: logseq-cli\ndescription: Operate Logseq\n---\nBODY")
    cmd = load_skill.func(_runtime(atom_home), name="logseq-cli")
    assert cmd.update["promoted_skills"] == ["logseq-cli"]
    assert "Loaded skill 'logseq-cli'" in cmd.update["messages"][0].content


def test_load_skill_rejects_unknown_and_traversal(atom_home):
    assert "promoted_skills" not in load_skill.func(_runtime(atom_home), name="nope").update
    assert "promoted_skills" not in load_skill.func(_runtime(atom_home), name="../etc/passwd").update
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_search.py -v`
Expected: FAIL with `ImportError: cannot import name 'load_skill'`.

- [ ] **Step 3: Write minimal implementation** — in `src/atom/tools/search.py`, replace the whole `search_skills` function and add `load_skill`. Add `from pathlib import Path` at the top import block:

```python
@tool(parse_docstring=True)
def search_skills(runtime: ToolRuntime, query: str, max_results: int = 3) -> Command:
    """Search the skill library to discover skills relevant to a task.

    Returns each match's name and description. To use one, load its full instructions with
    load_skill("<name>").

    Args:
        query: Describe the task or workflow you need guidance for.
        max_results: Maximum number of skills to list.
    """
    index = get_index(_home(runtime))
    tcid = runtime.tool_call_id
    if index is None or not index.has_skills:
        return Command(update={"messages": [ToolMessage("The skill library is empty.", tool_call_id=tcid)]})
    matches = index.search_skills(query, k=max_results, min_score=index.min_score)
    if not matches:
        return Command(update={"messages": [ToolMessage("No matching skills found.", tool_call_id=tcid)]})
    listing = "\n".join(f"- {m.name}: {m.description}" for m in matches)
    content = (
        'Found these skills. Load one with load_skill("<name>") to get its full instructions:\n'
        + listing
    )
    return Command(update={"messages": [ToolMessage(content, tool_call_id=tcid)]})


@tool(parse_docstring=True)
def load_skill(runtime: ToolRuntime, name: str) -> Command:
    """Load a skill's full instructions into context by its exact name.

    Use a name shown in the skills catalog or returned by search_skills.

    Args:
        name: The exact skill name to load (e.g. "logseq-cli").
    """
    tcid = runtime.tool_call_id
    clean = (name or "").strip()
    if not clean or "/" in clean or "\\" in clean or ".." in clean:
        return Command(update={"messages": [ToolMessage(f"Invalid skill name '{name}'.", tool_call_id=tcid)]})
    home = _home(runtime)
    found = bool(home) and any(
        (Path(home) / base / clean / "SKILL.md").exists() for base in ("skills", "skill_library")
    )
    if not found:
        return Command(update={"messages": [ToolMessage(
            f"No skill named '{clean}' found. Check the skills catalog or use search_skills.",
            tool_call_id=tcid)]})
    # promoted_skills is a union-reducer channel (merge_name_list); returning just this name is enough.
    return Command(update={
        "promoted_skills": [clean],
        "messages": [ToolMessage(
            f"Loaded skill '{clean}'. Follow its instructions for this task.", tool_call_id=tcid)],
    })
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_search.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/atom/tools/search.py tests/test_search.py
git commit -m "feat(skills): search_skills discovery-only; add load_skill loader"
```

---

### Task 3: Lead prompt catalog + `load_skill` wiring

**Files:**
- Modify: `src/atom/agent.py` (`render_lead_system_prompt`, `build_lead_agent`, `_build_middlewares`)
- Modify: `src/atom/prompts/lead_system.md`
- Test: `tests/test_prompts.py`, `tests/test_subagent.py` (via run_agent integration)

**Interfaces:**
- Consumes: `load_skill_catalog` (Task 1), `load_skill`/`search_skills` (Task 2).
- Produces: `render_lead_system_prompt(cfg, profile, profile_name, caps, *, frequent_tool_names=None, skill_catalog=None, has_tool_library=False, has_skill_library=False, notes=None, system_prompt_ref=None)` — `frequent_skills` param REMOVED, `skill_catalog` (list of `{"name","description"}` dicts) and `notes` ADDED. `_build_middlewares(..., trace=None, *, skill_catalog=None)`.

- [ ] **Step 1: Write the failing test** — append to `tests/test_prompts.py`:

```python
def test_lead_prompt_renders_skill_catalog_not_body(base_config):
    from atom.agent import render_lead_system_prompt

    prof = base_config.profile("default")
    out = render_lead_system_prompt(
        base_config, prof, "default", {"supports_vision": True},
        frequent_tool_names=["read_file", "load_skill"],
        skill_catalog=[{"name": "logseq-cli", "description": "Operate the Logseq CLI"}],
        has_tool_library=False, has_skill_library=False,
    )
    assert "logseq-cli" in out and "Operate the Logseq CLI" in out
    assert "load_skill" in out
    assert "FULL BODY" not in out               # only frontmatter, never the body
    assert "Skills (load before use)" in out


@pytest.mark.asyncio
async def test_load_skill_tool_bound_when_skill_present(base_config, atom_home):
    from langchain_core.messages import AIMessage, ToolMessage
    from atom.runtime import run_agent
    from tests.conftest import make_prepared

    d = atom_home / "skills" / "logseq-cli"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: logseq-cli\ndescription: Operate Logseq\n---\nUSE THE CLI")
    prepared = make_prepared([
        AIMessage(content="", tool_calls=[
            {"name": "load_skill", "args": {"name": "logseq-cli"}, "id": "l1", "type": "tool_call"}]),
        AIMessage(content="done"),
    ])
    result = await run_agent("do it", config=base_config, prepared=prepared)
    tool_msgs = [m for m in result.messages if isinstance(m, ToolMessage)]
    assert any("Loaded skill 'logseq-cli'" in m.content for m in tool_msgs)
```

(`pytest` is already imported in `test_prompts.py`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_prompts.py::test_lead_prompt_renders_skill_catalog_not_body -v`
Expected: FAIL (`render_lead_system_prompt() got an unexpected keyword argument 'skill_catalog'`).

- [ ] **Step 3a: Update `render_lead_system_prompt`** in `src/atom/agent.py`. Replace the signature's `frequent_skills` and the two ctx lines. New signature + ctx:

```python
def render_lead_system_prompt(
    cfg: AtomConfig,
    profile: AgentProfile,
    profile_name: str,
    caps: dict[str, Any],
    *,
    frequent_tool_names: list[str] | None = None,
    skill_catalog: list[dict] | None = None,
    has_tool_library: bool = False,
    has_skill_library: bool = False,
    notes: dict | None = None,
    system_prompt_ref: str | None = None,
) -> str:
    ctx = {
        "agent_name": profile_name,
        "date": datetime.date.today().isoformat(),
        "workspace": VIRTUAL_WORKSPACE,
        "uploads": VIRTUAL_UPLOADS,
        "outputs": VIRTUAL_OUTPUTS,
        "skills": VIRTUAL_SKILLS,
        "frequent_tool_names": frequent_tool_names if frequent_tool_names is not None else profile.tools.frequent,
        "skill_catalog": list(skill_catalog or []),
        "notes": notes,
        "bash_enabled": cfg.sandbox.bash_enabled,
        "supports_vision": caps.get("supports_vision", False),
        "has_tool_library": has_tool_library,
        "has_skill_library": has_skill_library,
    }
    return render_prompt(system_prompt_ref or profile.system_prompt, ctx, cfg.config_dir)
```

- [ ] **Step 3b: Update imports + `build_lead_agent`** in `src/atom/agent.py`.

Change the library import line:
```python
from atom.library import LibraryIndex, load_library, load_skill_catalog, register_index
```
Change the search-tools import line to include `load_skill`:
```python
from atom.tools.search import load_skill, search_skills, search_tools
```
In `build_lead_agent`, replace `frequent_skills = load_named_skills(home, profile.skills.frequent)` with:
```python
    catalog_entries = load_skill_catalog(home, profile.skills.frequent)
    skill_catalog = [{"name": s.name, "description": s.description} for s in catalog_entries]
    has_any_skills = bool(catalog_entries) or library.has_skills
```
After the `if library.has_skills: tools.append(search_skills)` line, add:
```python
    if has_any_skills:
        tools.append(load_skill)
```
In the `extras` block, after the `search_skills` append, add:
```python
    if has_any_skills:
        extras.append("load_skill")
```
Update the `render_lead_system_prompt(...)` call: replace `frequent_skills=frequent_skills,` with `skill_catalog=skill_catalog,`.
Update the `_build_middlewares(...)` call to pass the catalog:
```python
    middleware = _build_middlewares(
        cfg, profile, prepared, provider, home, summarizer, library, mw_trace,
        skill_catalog=skill_catalog,
    )
```

- [ ] **Step 3c: Update `_build_middlewares`** in `src/atom/agent.py`. Change the signature's trailing params to:
```python
    trace: dict | None = None,
    *,
    skill_catalog: list[dict] | None = None,
) -> list[AgentMiddleware]:
```
Change the SkillLibraryMiddleware gate from `if library.has_skills:` to:
```python
    if library.has_skills or skill_catalog:
        chain.append(SkillLibraryMiddleware(home=home))  # inject loaded-skill bodies (transient)
```

- [ ] **Step 3d: Update `src/atom/prompts/lead_system.md`.** Replace the `{% if frequent_skills %}` block:

```jinja
{% if skill_catalog %}
# Skills (load before use)
These skills are available. Before using one, load its full instructions with `load_skill("<name>")`.
{% for s in skill_catalog %}
- **{{ s.name }}** — {{ s.description }}
{% endfor %}{% endif %}
```

And update the discovery bullet (the `{% if has_skill_library %}` line under "Discovering more capabilities"):
```jinja
{% if has_skill_library %}- Call `search_skills("<topic>")` to discover more skills, then `load_skill("<name>")` to load one.
{% endif %}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_prompts.py -v`
Expected: PASS (new tests + existing `test_default_lead_prompt_renders_and_reflects_toggles`, `test_lead_prompt_keeps_contract_and_adds_discipline` still pass — they don't pass `skill_catalog`, and `search_skills` still absent when `has_skill_library=False`).

Also confirm no leftover reference to the old variable:
Run: `grep -rn "frequent_skills" src/ tests/`
Expected: no matches.

- [ ] **Step 5: Commit**

```bash
git add src/atom/agent.py src/atom/prompts/lead_system.md tests/test_prompts.py
git commit -m "feat(skills): lead prompt shows skill catalog + binds load_skill"
```

---

### Task 4: Sub-agent skill parity (catalog + search_skills + load_skill + injection)

**Files:**
- Modify: `src/atom/subagent.py` (`SubagentRunner` fields, `_child_tools`, `_child_middleware`, `_child_system`)
- Modify: `src/atom/agent.py` (`_build_middlewares` passes catalog + `has_skill_library` to the runner)
- Modify: `src/atom/prompts/subagent_general.md`, `src/atom/prompts/subagent_bash.md`
- Test: `tests/test_subagent.py`, `tests/test_prompts.py`

**Interfaces:**
- Consumes: `skill_catalog` (Task 3), `load_skill`/`search_skills` (Task 2), `SkillLibraryMiddleware`.
- Produces: `SubagentRunner(..., skill_catalog: list = [], has_skill_library: bool = False)`.

- [ ] **Step 1: Write the failing test** — append to `tests/test_subagent.py`:

```python
def test_child_agent_has_skill_tools_and_catalog(atom_home):
    from atom.subagent import SubagentRunner

    runner = SubagentRunner(
        model=None, home=str(atom_home), context_window=100_000, bash_enabled=True,
        skill_catalog=[{"name": "logseq-cli", "description": "Operate Logseq"}],
        has_skill_library=True,
    )
    for st in ("general-purpose", "bash"):
        names = [t.name for t in runner._child_tools(st)]
        assert "load_skill" in names and "search_skills" in names
    sys = runner._child_system("general-purpose")
    assert "logseq-cli" in sys and "Operate Logseq" in sys


def test_child_middleware_includes_skill_library_when_catalog(atom_home):
    from atom.middleware.skill_library import SkillLibraryMiddleware
    from atom.subagent import SubagentRunner

    runner = SubagentRunner(model=None, home=str(atom_home), context_window=100_000,
                            bash_enabled=False, skill_catalog=[{"name": "x", "description": "y"}])
    assert any(isinstance(m, SkillLibraryMiddleware) for m in runner._child_middleware())


def test_child_agent_no_skill_tools_when_none(atom_home):
    from atom.subagent import SubagentRunner

    runner = SubagentRunner(model=None, home=str(atom_home), context_window=100_000, bash_enabled=False)
    names = [t.name for t in runner._child_tools("general-purpose")]
    assert "load_skill" not in names and "search_skills" not in names
```

Also update the existing `test_subagent_prompts_render_and_report_contract` in `tests/test_prompts.py` — add `"skill_catalog": []` to its `ctx` dict (the templates now reference it under StrictUndefined):

```python
    ctx = {
        "date": "2026-07-05",
        "workspace": "/w",
        "uploads": "/u",
        "outputs": "/o",
        "frequent_tool_names": ["read_file", "write_file"],
        "skill_catalog": [],
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_subagent.py::test_child_agent_has_skill_tools_and_catalog -v`
Expected: FAIL (`SubagentRunner.__init__() got an unexpected keyword argument 'skill_catalog'`).

- [ ] **Step 3a: Update `SubagentRunner`** in `src/atom/subagent.py`.

Add `field` to the dataclass import: `from dataclasses import dataclass, field`.
Add two fields to the dataclass (after `observability: Any = None`):
```python
    skill_catalog: list = field(default_factory=list)  # [{"name","description"}] injected as always-on catalog
    has_skill_library: bool = False                    # a skill_library/ exists -> bind search_skills
```

Replace `_child_tools`:
```python
    def _child_tools(self, subagent_type: SubagentType) -> list:
        # Children get file tools (+bash) but NOT delegate_task — no nested delegation.
        from atom.tools.bash import bash
        from atom.tools.filesystem import FILESYSTEM_TOOLS
        from atom.tools.search import load_skill, search_skills

        tools = list(FILESYSTEM_TOOLS)
        if subagent_type == "bash" and self.bash_enabled:
            tools.append(bash)
        if self.skill_catalog or self.has_skill_library:
            tools.append(load_skill)
        if self.has_skill_library:
            tools.append(search_skills)
        return tools
```

In `_child_middleware`, before the final `mw += [ToolErrorHandlingMiddleware(), LoopDetectionMiddleware()]` line, insert:
```python
        if self.skill_catalog or self.has_skill_library:
            from atom.middleware.skill_library import SkillLibraryMiddleware

            mw.append(SkillLibraryMiddleware(self.home))
```

In `_child_system`, add `skill_catalog` to the render ctx:
```python
    def _child_system(self, subagent_type: SubagentType) -> str:
        frequent = [t.name for t in self._child_tools(subagent_type)]
        return render_prompt(
            _SUBAGENT_PROMPTS[subagent_type],
            {
                "date": datetime.date.today().isoformat(),
                "workspace": VIRTUAL_WORKSPACE,
                "uploads": VIRTUAL_UPLOADS,
                "outputs": VIRTUAL_OUTPUTS,
                "frequent_tool_names": frequent,
                "skill_catalog": list(self.skill_catalog),
            },
            self.config_dir,
        )
```

- [ ] **Step 3b: Pass catalog into the runner** in `src/atom/agent.py` `_build_middlewares`, in the `SubagentRunner(...)` constructor call add:
```python
        skill_catalog=skill_catalog or [],
        has_skill_library=library.has_skills,
```

- [ ] **Step 3c: Add the catalog block to both sub-agent prompts.**

In `src/atom/prompts/subagent_general.md`, after the workspace bullets (the `{{ outputs }}` line) and before the "Do exactly the task" paragraph, insert:
```jinja
{% if skill_catalog %}
Skills available (load full instructions with `load_skill("<name>")` before use):
{% for s in skill_catalog %}
- {{ s.name }} — {{ s.description }}
{% endfor %}
{% endif %}
```

In `src/atom/prompts/subagent_bash.md`, after the `{{ outputs }}` bullet and before the "Run the commands…" paragraph, insert the same block.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_subagent.py tests/test_prompts.py -v`
Expected: PASS (new sub-agent tests + updated contract test + existing `test_child_agent_cannot_delegate`).

- [ ] **Step 5: Commit**

```bash
git add src/atom/subagent.py src/atom/agent.py src/atom/prompts/subagent_general.md src/atom/prompts/subagent_bash.md tests/test_subagent.py tests/test_prompts.py
git commit -m "feat(skills): sub-agents get skill catalog + search_skills/load_skill parity"
```

---

### Task 5: Notes config on the workflow schema

**Files:**
- Modify: `src/atom/workflow/schema.py`
- Test: `tests/test_workflow_schema.py`

**Interfaces:**
- Produces: `NotesConfig(enabled: bool = False, provider: Literal["logseq"] = "logseq", graph: Optional[str] = None)`; `WorkflowDef.notes: NotesConfig`.

- [ ] **Step 1: Write the failing test** — append to `tests/test_workflow_schema.py`:

```python
def test_notes_defaults_disabled():
    wf = WorkflowDef.model_validate(
        {"name": "w", "steps": [{"title": "s", "tasks": [{"prompt": "p"}]}]})
    assert wf.notes.enabled is False
    assert wf.notes.provider == "logseq"
    assert wf.notes.graph is None


def test_notes_block_parses():
    wf = WorkflowDef.model_validate({
        "name": "w",
        "notes": {"enabled": True, "graph": "my-graph"},
        "steps": [{"title": "s", "tasks": [{"prompt": "p"}]}],
    })
    assert wf.notes.enabled is True and wf.notes.graph == "my-graph"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_workflow_schema.py::test_notes_defaults_disabled -v`
Expected: FAIL (`AttributeError: 'WorkflowDef' object has no attribute 'notes'`).

- [ ] **Step 3: Write minimal implementation** — in `src/atom/workflow/schema.py`.

Add `Literal` to the typing import: `from typing import Literal, Optional, Union`.
Add the `NotesConfig` class (after `InputDef`):
```python
class NotesConfig(_Base):
    enabled: bool = False
    provider: Literal["logseq"] = "logseq"
    graph: Optional[str] = None   # default (resolved in atom.notes): slug of the workflow name
```
Add the field to `WorkflowDef` (after `inputs`):
```python
    notes: NotesConfig = Field(default_factory=NotesConfig)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_workflow_schema.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/atom/workflow/schema.py tests/test_workflow_schema.py
git commit -m "feat(notes): NotesConfig block on the workflow schema"
```

---

### Task 6: Notes module (`ensure_vault`)

**Files:**
- Create: `src/atom/notes.py`
- Test: `tests/test_notes.py` (new)

**Interfaces:**
- Produces:
  - `NotesBinding(provider, root_dir, graph)` with `.as_prompt_ctx() -> {"provider","root_dir","graph"}`.
  - `_slug(name) -> str`, `notes_root(home, workflow_name) -> Path`.
  - `ensure_vault(home, workflow_name, notes_cfg, *, runner=None) -> NotesBinding` (runner: `Callable[[list[str]], tuple[int, str, str]]`).

- [ ] **Step 1: Write the failing test** — create `tests/test_notes.py`:

```python
"""Persistent-notes vault lifecycle (Logseq), with an injected fake CLI runner."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from atom.notes import NotesBinding, _slug, ensure_vault, notes_root


def test_slug():
    assert _slug("Notes Smoke!") == "notes-smoke"
    assert _slug("  ") == "workflow"


def test_notes_root(atom_home):
    assert notes_root(str(atom_home), "Notes Smoke") == atom_home / "notes" / "notes-smoke"


def test_ensure_vault_creates_when_absent(atom_home):
    calls = []

    def fake_runner(args):
        calls.append(args)
        if args[1:3] == ["graph", "list"]:
            return 0, '{"status":"ok","data":{"graphs":[],"graph-items":[]}}', ""
        return 0, 'Created graph "notes-smoke"', ""

    cfg = SimpleNamespace(provider="logseq", graph=None)
    binding = ensure_vault(str(atom_home), "notes-smoke", cfg, runner=fake_runner)
    assert isinstance(binding, NotesBinding)
    assert binding.graph == "notes-smoke"
    assert binding.root_dir == str(atom_home / "notes" / "notes-smoke")
    assert binding.as_prompt_ctx() == {
        "provider": "logseq", "root_dir": binding.root_dir, "graph": "notes-smoke"}
    assert any(a[1:3] == ["graph", "create"] for a in calls)


def test_ensure_vault_reuses_when_present(atom_home):
    calls = []

    def fake_runner(args):
        calls.append(args)
        if args[1:3] == ["graph", "list"]:
            return 0, '{"status":"ok","data":{"graphs":["notes-smoke"]}}', ""
        return 0, "", ""

    cfg = SimpleNamespace(provider="logseq", graph=None)
    ensure_vault(str(atom_home), "notes-smoke", cfg, runner=fake_runner)
    assert not any(a[1:3] == ["graph", "create"] for a in calls)  # reused, no create


def test_ensure_vault_custom_graph_name(atom_home):
    def fake_runner(args):
        if args[1:3] == ["graph", "list"]:
            return 0, '{"data":{"graphs":[]}}', ""
        return 0, "", ""

    cfg = SimpleNamespace(provider="logseq", graph="custom")
    assert ensure_vault(str(atom_home), "wf", cfg, runner=fake_runner).graph == "custom"


def test_ensure_vault_rejects_unknown_provider(atom_home):
    with pytest.raises(NotImplementedError):
        ensure_vault(str(atom_home), "wf", SimpleNamespace(provider="notion", graph=None))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_notes.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'atom.notes'`).

- [ ] **Step 3: Write minimal implementation** — create `src/atom/notes.py`:

```python
"""Persistent workflow notes: provision + reuse a per-workflow Logseq vault (graph).

The vault lives OUTSIDE any per-run workspace, keyed by workflow name, so it is shared across
every run of that workflow. ``ensure_vault`` is idempotent (list-then-create-if-absent).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from atom.sandbox.paths import atom_home

CLIRunner = Callable[[list[str]], "tuple[int, str, str]"]


@dataclass
class NotesBinding:
    provider: str
    root_dir: str
    graph: str

    def as_prompt_ctx(self) -> dict:
        return {"provider": self.provider, "root_dir": self.root_dir, "graph": self.graph}


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s or "workflow"


def notes_root(home, workflow_name: str) -> Path:
    return atom_home(home) / "notes" / _slug(workflow_name)


def _default_runner(args: list[str]) -> "tuple[int, str, str]":
    if shutil.which(args[0]) is None:
        raise FileNotFoundError(
            f"'{args[0]}' CLI not found on PATH. Install the Logseq CLI to use persistent notes."
        )
    proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
    return proc.returncode, proc.stdout, proc.stderr


def ensure_vault(home, workflow_name: str, notes_cfg, *, runner: Optional[CLIRunner] = None) -> NotesBinding:
    """Ensure the workflow's Logseq graph exists (create once, reuse thereafter). Idempotent."""
    provider = getattr(notes_cfg, "provider", "logseq")
    if provider != "logseq":
        raise NotImplementedError(f"notes provider '{provider}' is not supported")
    run = runner or _default_runner
    root = notes_root(home, workflow_name)
    root.mkdir(parents=True, exist_ok=True)
    graph = getattr(notes_cfg, "graph", None) or _slug(workflow_name)

    _rc, out, _err = run(["logseq", "graph", "list", "--root-dir", str(root), "--output", "json"])
    try:
        existing = (json.loads(out).get("data") or {}).get("graphs") or []
    except (ValueError, AttributeError):
        existing = []
    if graph not in existing:
        run(["logseq", "graph", "create", "--graph", graph, "--root-dir", str(root)])
    return NotesBinding(provider="logseq", root_dir=str(root), graph=graph)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_notes.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/atom/notes.py tests/test_notes.py
git commit -m "feat(notes): ensure_vault provisions/reuses a per-workflow Logseq vault"
```

---

### Task 7: Thread `notes` into the lead prompt (runtime + agent + template)

**Files:**
- Modify: `src/atom/runtime.py` (`run_agent`)
- Modify: `src/atom/agent.py` (`build_lead_agent`)
- Modify: `src/atom/prompts/lead_system.md`
- Test: `tests/test_prompts.py`

**Interfaces:**
- Consumes: `render_lead_system_prompt(..., notes=...)` (Task 3 added the param).
- Produces: `run_agent(..., notes: dict | None = None)`, `build_lead_agent(..., notes: dict | None = None)`.

- [ ] **Step 1: Write the failing test** — append to `tests/test_prompts.py`:

```python
def test_lead_prompt_notes_block_renders(base_config):
    from atom.agent import render_lead_system_prompt

    prof = base_config.profile("default")
    out = render_lead_system_prompt(
        base_config, prof, "default", {"supports_vision": True},
        frequent_tool_names=["read_file"],
        notes={"provider": "logseq", "root_dir": "/n/notes-smoke", "graph": "notes-smoke"},
    )
    assert "Persistent notes" in out
    assert "notes-smoke" in out and "/n/notes-smoke" in out


def test_lead_prompt_no_notes_block_when_absent(base_config):
    from atom.agent import render_lead_system_prompt

    prof = base_config.profile("default")
    out = render_lead_system_prompt(
        base_config, prof, "default", {"supports_vision": True},
        frequent_tool_names=["read_file"])
    assert "Persistent notes" not in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_prompts.py::test_lead_prompt_notes_block_renders -v`
Expected: FAIL (asserts "Persistent notes" not present — the template block doesn't exist yet).

- [ ] **Step 3a: Add the notes block to `src/atom/prompts/lead_system.md`.** Insert immediately after the `{% if skill_catalog %}…{% endif %}` block and before `# How to work`:

```jinja
{% if notes %}
# Persistent notes (Logseq)
A Logseq vault persists across every run of this workflow — treat it as long-term memory. Graph `{{ notes.graph }}` lives at root-dir `{{ notes.root_dir }}`. Reach it with the logseq CLI: `logseq --root-dir {{ notes.root_dir }} --graph {{ notes.graph }} <command>`. Load the `logseq-cli` skill (`load_skill("logseq-cli")`) for command details. Before you start, read what earlier runs left; as you work, record durable notes and tasks there so future runs can build on them.
{% endif %}
```

- [ ] **Step 3b: Add `notes` to `build_lead_agent`** in `src/atom/agent.py`. Add the parameter (after `trace: dict | None = None,`):
```python
    notes: dict | None = None,
):
```
Pass it into the render call — add `notes=notes,` to the `render_lead_system_prompt(...)` arguments.

- [ ] **Step 3c: Add `notes` to `run_agent`** in `src/atom/runtime.py`. Add the parameter (after `prepared: PreparedModel | None = None,`):
```python
    notes: dict | None = None,
) -> RunResult:
```
In the `build_lead_agent(...)` call, add `notes=notes,`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_prompts.py -v`
Expected: PASS (notes block present when passed, absent otherwise).

- [ ] **Step 5: Commit**

```bash
git add src/atom/runtime.py src/atom/agent.py src/atom/prompts/lead_system.md tests/test_prompts.py
git commit -m "feat(notes): thread a notes binding into the lead system prompt"
```

---

### Task 8: Engine wiring (ensure vault once, forward binding)

**Files:**
- Modify: `src/atom/workflow/engine.py`
- Test: `tests/test_workflow_engine.py`

**Interfaces:**
- Consumes: `ensure_vault` (Task 6), `run_agent(..., notes=...)` (Task 7), `NotesBinding.as_prompt_ctx()`.

- [ ] **Step 1: Write the failing test** — append to `tests/test_workflow_engine.py`:

```python
@pytest.mark.asyncio
async def test_notes_binding_forwarded_to_run_agent(base_config, atom_home, monkeypatch):
    from atom.notes import NotesBinding
    from atom.runtime import RunResult

    captured = {}

    def fake_ensure(home, name, cfg, **k):
        return NotesBinding(provider="logseq", root_dir="/x", graph="demo")

    async def spy(prompt, **kwargs):
        captured["notes"] = kwargs.get("notes")
        return RunResult(thread_id=kwargs.get("thread_id", "t"), messages=[], final_text="ok", state={})

    monkeypatch.setattr(engine_mod, "ensure_vault", fake_ensure)
    monkeypatch.setattr(engine_mod, "run_agent", spy)

    wf = WorkflowDef.model_validate({
        "name": "demo", "notes": {"enabled": True},
        "steps": [{"title": "Draft", "tasks": [{"id": "t1", "prompt": "x"}]}]})
    engine = WorkflowEngine(base_config)
    engine.create_run(wf, {}, "runnotesfwd", "2026-07-03T00:00:00")
    await engine.execute("runnotesfwd")
    assert captured["notes"] == {"provider": "logseq", "root_dir": "/x", "graph": "demo"}


@pytest.mark.asyncio
async def test_no_notes_forwards_none(base_config, atom_home, monkeypatch):
    from atom.runtime import RunResult

    captured = {}

    async def spy(prompt, **kwargs):
        captured["notes"] = kwargs.get("notes")
        return RunResult(thread_id=kwargs.get("thread_id", "t"), messages=[], final_text="ok", state={})

    monkeypatch.setattr(engine_mod, "run_agent", spy)
    engine = WorkflowEngine(base_config)
    engine.create_run(_one_task_workflow(), {}, "runnonotes", "2026-07-03T00:00:00")
    await engine.execute("runnonotes")
    assert captured["notes"] is None


@pytest.mark.asyncio
async def test_notes_setup_failure_halts_run(base_config, atom_home, monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("logseq missing")

    monkeypatch.setattr(engine_mod, "ensure_vault", boom)
    wf = WorkflowDef.model_validate({
        "name": "demo", "notes": {"enabled": True},
        "steps": [{"title": "Draft", "tasks": [{"id": "t1", "prompt": "x"}]}]})
    engine = WorkflowEngine(base_config)
    engine.create_run(wf, {}, "runnotesfail", "2026-07-03T00:00:00")
    manifest = await engine.execute("runnotesfail")
    assert manifest.status == "halted"
    assert "logseq missing" in (manifest.steps[0].tasks[0].error or "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_workflow_engine.py::test_notes_binding_forwarded_to_run_agent -v`
Expected: FAIL (`notes` kwarg not captured / `ensure_vault` not imported in engine).

- [ ] **Step 3a: Import `ensure_vault`** in `src/atom/workflow/engine.py` (with the other `atom` imports near the top):
```python
from atom.notes import ensure_vault
```

- [ ] **Step 3b: Ensure the vault + thread it through** in `execute()`. After `workflow = self._defs.get(...) or load_workflow(...)` and `manifest.status = "running"; self.store.save(manifest)`, add:
```python
            notes_binding = None
            if workflow.notes.enabled:
                try:
                    notes_binding = ensure_vault(self.cfg.home, workflow.name, workflow.notes)
                except Exception as exc:  # noqa: BLE001 — notes setup failure halts the run cleanly
                    if manifest.steps and manifest.steps[0].tasks:
                        manifest.steps[0].tasks[0].status = "failed"
                        manifest.steps[0].tasks[0].error = (
                            f"persistent notes setup failed: {type(exc).__name__}: {exc}")
                        manifest.steps[0].status = "failed"
                    manifest.status = "halted"
                    manifest.ended_at = _now()
                    self.store.save(manifest)
                    return manifest
```
In the `run_one` inner coroutine, pass the binding through:
```python
                async def run_one(ts: TaskState, td: TaskDef, sd: StepDef, ss: StepState):
                    async with sem:
                        await self._run_task(manifest, workflow, ss, sd, ts, td, notes=notes_binding)
```

- [ ] **Step 3c: Accept + forward `notes` in `_run_task`.** Change its signature:
```python
    async def _run_task(
        self, manifest: RunManifest, workflow: WorkflowDef,
        step_state: StepState, step_def: StepDef, ts: TaskState, td: TaskDef,
        notes: "object | None" = None,
    ) -> None:
```
In the `run_agent(...)` call, add:
```python
                notes=notes.as_prompt_ctx() if notes else None,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_workflow_engine.py -v`
Expected: PASS (3 new tests + all existing engine tests).

- [ ] **Step 5: Commit**

```bash
git add src/atom/workflow/engine.py tests/test_workflow_engine.py
git commit -m "feat(notes): engine ensures the vault once per run and forwards the binding"
```

---

### Task 9: Test workflow + README

**Files:**
- Create: `workflows/notes-smoke.yaml`
- Modify: `README.md`
- Test: `tests/test_workflow_schema.py`

- [ ] **Step 1: Write the failing test** — append to `tests/test_workflow_schema.py`:

```python
def test_notes_smoke_workflow_valid():
    import yaml
    from pathlib import Path

    data = yaml.safe_load(Path("workflows/notes-smoke.yaml").read_text())
    wf = WorkflowDef.model_validate(data)
    assert wf.name == "notes-smoke"
    assert wf.notes.enabled is True
    assert [s.title for s in wf.steps] == ["Recall", "Record"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_workflow_schema.py::test_notes_smoke_workflow_valid -v`
Expected: FAIL (`FileNotFoundError: workflows/notes-smoke.yaml`).

- [ ] **Step 3a: Create `workflows/notes-smoke.yaml`:**

```yaml
# workflows/notes-smoke.yaml — copy to $ATOM_HOME/workflows/ to run it.
# Persistent-notes smoke test: recall prior entries, then record a new one.
# Run it TWICE — the second run's "Recall" step should see the first run's entry.
name: notes-smoke
description: Smoke-test persistent notes — recall prior entries in the shared Logseq vault, then record a new one.
notes:
  enabled: true
inputs:
  - name: entry
    required: false
    default: "hello from a notes-smoke run"
steps:
  - title: Recall
    description: Read what earlier runs left in the persistent vault.
    tasks:
      - id: recall
        prompt: >
          Load the logseq-cli skill, then list every task already recorded in this workflow's
          persistent Logseq vault and report how many exist and what they say. If none exist yet,
          say so plainly.
        model: haiku
        thinking: low
  - title: Record
    description: Append a new dated entry, then confirm it persisted.
    tasks:
      - id: record
        prompt: >
          Load the logseq-cli skill, then append a new dated task to this workflow's persistent
          Logseq vault with the content "{{ entry }} ({{ date }})". Confirm by listing tasks again,
          then write a one-line confirmation to {{ outputs }}/notes-confirmation.md and call
          present_files on it.
        model: haiku
        thinking: low
```

- [ ] **Step 3b: Update `README.md`.**

Add a **Prerequisites** line to the `## Install` section (after the providers paragraph, around line 21):
```markdown
**Prerequisites for persistent-notes workflows:** the `logseq` CLI must be installed and on your
`PATH` (guaranteed on target devices). Verify with `logseq --version`.
```

In the `## Workflows` section, after the paragraph ending "…see Observability below." (around line 66), add:
```markdown
**Persistent notes.** Add a `notes:` block to a workflow to give it long-term memory that
persists across runs:

```yaml
notes:
  enabled: true          # provisions a per-workflow Logseq vault, shared by every run
  # graph: my-graph      # optional; defaults to the slugified workflow name
```

When enabled, atom ensures a Logseq graph at `$ATOM_HOME/notes/<workflow-slug>/` (once, reused
across runs) and injects a snippet into each task's system prompt telling the agent where the vault
is and to `load_skill("logseq-cli")` for the CLI commands. Try it with `workflows/notes-smoke.yaml`
(run it twice — the second run recalls the first run's entry).
```

Also update the **Extend → A skill** bullet (around line 113-116) to reflect the catalog/load model:
```markdown
- **A skill**: create `$ATOM_HOME/skill_library/<name>/SKILL.md` with YAML front-matter
  (`name`, `description`, `keywords`) + a markdown body. Discover it with `search_skills` and load
  it with `load_skill("<name>")`. Skills in `$ATOM_HOME/skills/<name>/SKILL.md` are auto-discovered
  into an always-on catalog (name + description) in every agent's prompt and loaded on demand with
  `load_skill`.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_workflow_schema.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add workflows/notes-smoke.yaml README.md tests/test_workflow_schema.py
git commit -m "feat(notes): notes-smoke test workflow + README prerequisites/docs"
```

---

### Task 10: Full suite + live end-to-end debug

**Files:** none (verification only).

- [ ] **Step 1: Run the full test suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass. If any fail, fix before proceeding.

- [ ] **Step 2: Install the test workflow into ATOM_HOME**

```bash
cp workflows/notes-smoke.yaml ~/.atom/workflows/
.venv/bin/atom workflow list        # notes-smoke should appear
```

- [ ] **Step 3: First live run (records the first entry)**

```bash
.venv/bin/atom workflow run notes-smoke --input entry="first run entry"
```
Expected: status `complete`. The Recall step reports the vault is empty (first run); the Record step writes an entry. Verify the vault + graph exist:
```bash
ls -la ~/.atom/notes/notes-smoke
logseq list task --root-dir ~/.atom/notes/notes-smoke --graph notes-smoke
```
Expected: one task containing "first run entry".

- [ ] **Step 4: Second live run (proves cross-run persistence)**

```bash
.venv/bin/atom workflow run notes-smoke --input entry="second run entry"
```
Expected: status `complete`. The Recall step now reports the **first run's** entry ("first run entry") — proving the vault is shared across runs. Confirm two entries:
```bash
logseq list task --root-dir ~/.atom/notes/notes-smoke --graph notes-smoke
```
Expected: two tasks (both entries).

- [ ] **Step 5: Debug if needed**

If a run halts or an agent misuses the CLI, inspect the per-run transcripts:
```bash
ls ~/.atom/workflows/runs                                  # find the latest run id
cat ~/.atom/workflows/runs/<run_id>/chats/s0__recall.json  # Recall step transcript
cat ~/.atom/workflows/runs/<run_id>/chats/s1__record.json  # Record step transcript
```
Common issues + fixes:
- Agent didn't load the skill → strengthen the task prompt / catalog wording.
- `logseq` command errors → the injected snippet's `--root-dir`/`--graph` must match `ensure_vault`'s output; verify `NotesBinding.as_prompt_ctx()` values appear in the prompt.
- Fix, re-run the suite, and re-run the live workflow. Do not mark complete until step 4 shows cross-run recall.

- [ ] **Step 6: Final commit (only if debugging changed files)**

```bash
git add -A
git commit -m "fix(notes): live-run debugging adjustments"
```

---

## Self-Review

- **Spec coverage:** Skill catalog (T1,T3,T4) · `search_skills` discovery-only + `load_skill` (T2) · sub-agent parity (T4) · notes schema (T5) · vault lifecycle (T6) · prompt threading (T7) · engine wiring (T8) · test workflow + README prereqs (T9) · live proof (T10). All spec components mapped.
- **Type consistency:** `render_lead_system_prompt(skill_catalog=…, notes=…)` used identically in T3/T7/tests; `skill_catalog` is always a list of `{"name","description"}` dicts (lead + child); `NotesBinding.as_prompt_ctx()` shape `{"provider","root_dir","graph"}` matches the engine forward (T8) and prompt assertions (T7); `promoted_skills` union-reducer relied on in T2; `_build_middlewares(..., *, skill_catalog=…)` defined in T3 and consumed in T4.
- **No placeholders:** every step carries real code/commands/expected output.
