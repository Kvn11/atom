# /mnt/skill_library Mount Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose `$ATOM_HOME/skill_library/` to the agent sandbox at a new virtual mount `/mnt/skill_library`, and tell the agent where a loaded skill's bundled files live, so deferred-skill resources (non-`SKILL.md` files) are reachable.

**Architecture:** Add one more entry to the sandbox's virtual→physical mount map (the existing generic `resolve()`/bash-rewrite logic needs no change), advertise the mount in the lead prompt, and add a per-skill location hint to the `load_skill` tool message and the two transient skill-injection middlewares. Option B (separate mount) — each mount maps to exactly one physical dir; no overlay/union resolution.

**Tech Stack:** Python 3.11, pytest, Jinja2 (StrictUndefined) prompts, LangChain middleware.

## Global Constraints

- New mount constant: `VIRTUAL_SKILL_LIBRARY = "/mnt/skill_library"` (physical: `ThreadPaths.skill_library` = `$ATOM_HOME/skill_library`).
- The existing `/mnt/skills` mount (`VIRTUAL_SKILLS`) and its behavior are UNCHANGED.
- No changes to `LocalSandbox.resolve()` or `_rewrite_virtual()` — the mount set is generic; adding a mapping is sufficient.
- Skill-file mount precedence (which mount a skill's files are reported under): `skills/` before `skill_library/` — matches the existing `("skills", "skill_library")` lookup order in `load_skill` and `load_named_skills`.
- Prompts render under Jinja `StrictUndefined`: every new template variable (`{{ skill_library }}`) MUST always be provided by `render_lead_system_prompt`.
- Reuse the mount constants (`VIRTUAL_SKILLS`, `VIRTUAL_SKILL_LIBRARY`) — do NOT hardcode the `/mnt/...` strings in prompts/tools/middleware except in the prompt Markdown template.
- TDD: write the failing test first, watch it fail, implement minimally, watch it pass, commit.

---

### Task 1: Add the `/mnt/skill_library` mount

**Files:**
- Modify: `src/atom/sandbox/paths.py` (add constant near line 32; add to `virtual_map()` near line 74)
- Modify: `src/atom/sandbox/__init__.py` (re-export the constant)
- Test: `tests/test_sandbox.py`

**Interfaces:**
- Produces: `atom.sandbox.paths.VIRTUAL_SKILL_LIBRARY: str` (also importable as `atom.sandbox.VIRTUAL_SKILL_LIBRARY`); `ThreadPaths.virtual_map()` now includes the key `VIRTUAL_SKILL_LIBRARY → self.skill_library`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_sandbox.py`:

```python
def test_skill_library_mount_maps_to_physical_dir(atom_home):
    from atom.sandbox.paths import VIRTUAL_SKILL_LIBRARY
    tp = thread_paths("u", "skl-map")
    assert tp.virtual_map()[VIRTUAL_SKILL_LIBRARY] == tp.skill_library


def test_sandbox_reads_bundled_file_from_skill_library_mount(atom_home):
    tp = thread_paths("u", "skl-read").ensure()
    skill = tp.skill_library / "pdf-extract"
    skill.mkdir(parents=True)
    (skill / "reference.md").write_text("bundled reference body\n")
    sb = LocalSandboxProvider().acquire(thread_paths("u", "skl-read"))
    assert sb.read_text("/mnt/skill_library/pdf-extract/reference.md") == "bundled reference body\n"


def test_skill_library_mount_confines_escapes(atom_home):
    sb = _sandbox("skl-esc")
    with pytest.raises((PathEscapeError, FileNotFoundError)):
        sb.resolve("/mnt/skill_library/../../../../etc/passwd")


def test_skills_and_skill_library_are_distinct_roots(atom_home):
    tp = thread_paths("u", "skl-distinct")
    vm = tp.virtual_map()
    from atom.sandbox.paths import VIRTUAL_SKILLS, VIRTUAL_SKILL_LIBRARY
    assert vm[VIRTUAL_SKILLS] == tp.skills
    assert vm[VIRTUAL_SKILL_LIBRARY] == tp.skill_library
    assert tp.skills != tp.skill_library
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_sandbox.py -k "skill_library or distinct_roots" -v`
Expected: FAIL with `ImportError` / `KeyError` on `VIRTUAL_SKILL_LIBRARY`.

- [ ] **Step 3: Add the constant and the mapping**

In `src/atom/sandbox/paths.py`, after the `VIRTUAL_SKILLS` line:

```python
VIRTUAL_SKILLS = "/mnt/skills"
VIRTUAL_SKILL_LIBRARY = "/mnt/skill_library"
```

In `virtual_map()`, add the new entry:

```python
    def virtual_map(self) -> dict[str, Path]:
        """Map each virtual mount prefix to its physical directory."""
        return {
            VIRTUAL_WORKSPACE: self.workspace,
            VIRTUAL_UPLOADS: self.uploads,
            VIRTUAL_OUTPUTS: self.outputs,
            VIRTUAL_SKILLS: self.skills,
            VIRTUAL_SKILL_LIBRARY: self.skill_library,
        }
```

- [ ] **Step 4: Re-export the constant**

In `src/atom/sandbox/__init__.py`, add `VIRTUAL_SKILL_LIBRARY` to both the import from `atom.sandbox.paths` and `__all__`:

```python
from atom.sandbox.paths import (
    ThreadPaths,
    VIRTUAL_OUTPUTS,
    VIRTUAL_SKILL_LIBRARY,
    VIRTUAL_SKILLS,
    VIRTUAL_UPLOADS,
    VIRTUAL_WORKSPACE,
    atom_home,
    thread_paths,
)
```

and in `__all__` add `"VIRTUAL_SKILL_LIBRARY",` next to `"VIRTUAL_SKILLS",`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_sandbox.py -v`
Expected: PASS (all, including pre-existing).

- [ ] **Step 6: Commit**

```bash
git add src/atom/sandbox/paths.py src/atom/sandbox/__init__.py tests/test_sandbox.py
git commit -m "feat(sandbox): mount skill_library at /mnt/skill_library"
```

---

### Task 2: Advertise the mount in the lead system prompt

**Files:**
- Modify: `src/atom/agent.py` (`render_lead_system_prompt` — imports near line 21-26; ctx dict near line 72-75)
- Modify: `src/atom/prompts/lead_system.md` (Workspace section, lines 4-9)
- Test: `tests/test_prompts.py`

**Interfaces:**
- Consumes: `VIRTUAL_SKILL_LIBRARY` from Task 1.
- Produces: the rendered lead prompt now names the `/mnt/skill_library` mount and the bundled-files convention.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_prompts.py`:

```python
def test_lead_prompt_advertises_skill_library_mount(base_config):
    from atom.agent import render_lead_system_prompt

    prof = base_config.profile("default")
    out = render_lead_system_prompt(
        base_config, prof, "default", {"supports_vision": True},
        frequent_tool_names=["read_file"],
    )
    assert "/mnt/skill_library" in out
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_prompts.py::test_lead_prompt_advertises_skill_library_mount -v`
Expected: FAIL (`/mnt/skill_library` not in output).

- [ ] **Step 3: Pass the mount into the prompt ctx**

In `src/atom/agent.py`, add `VIRTUAL_SKILL_LIBRARY` to the sandbox-paths import block (keep alphabetical with the others):

```python
from atom.sandbox.paths import (
    VIRTUAL_OUTPUTS,
    VIRTUAL_SKILL_LIBRARY,
    VIRTUAL_SKILLS,
    VIRTUAL_UPLOADS,
    VIRTUAL_WORKSPACE,
)
```

In the `ctx` dict inside `render_lead_system_prompt`, add the key right after `"skills"`:

```python
        "skills": VIRTUAL_SKILLS,
        "skill_library": VIRTUAL_SKILL_LIBRARY,
```

- [ ] **Step 4: Update the prompt template**

In `src/atom/prompts/lead_system.md`, in the `# Workspace` bullet list, add a bullet after the `{{ skills }}` line and a convention note. Replace lines 8-9:

```markdown
- `{{ skills }}` — reference skill documents.
File tools accept these virtual paths or a path relative to the workspace. Paths outside these mounts are rejected.
```

with:

```markdown
- `{{ skills }}` — reference skill documents (always-on skills).
- `{{ skill_library }}` — files bundled with skills you load via `load_skill`.
File tools accept these virtual paths or a path relative to the workspace. Paths outside these mounts are rejected. A skill's bundled files live under `{{ skills }}/<skill-name>/` (always-on skills) or `{{ skill_library }}/<skill-name>/` (skills you load on demand).
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_prompts.py -v`
Expected: PASS (new test + all pre-existing lead-prompt tests still render under StrictUndefined).

- [ ] **Step 6: Commit**

```bash
git add src/atom/agent.py src/atom/prompts/lead_system.md tests/test_prompts.py
git commit -m "feat(prompt): advertise /mnt/skill_library mount + bundled-files convention"
```

---

### Task 3: `load_skill` names the skill's bundled-files mount

**Files:**
- Modify: `src/atom/tools/search.py` (`load_skill`, lines 77-103)
- Test: `tests/test_search.py`

**Interfaces:**
- Consumes: `VIRTUAL_SKILLS`, `VIRTUAL_SKILL_LIBRARY` from Task 1.
- Produces: `load_skill`'s success `ToolMessage` ends with `Its bundled files are at <mount>/<name>/.` where `<mount>` is `/mnt/skills` for an always-on skill and `/mnt/skill_library` for a deferred one.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_search.py` (it already imports `load_skill`, `seed_library`, and defines `_runtime`):

```python
def test_load_skill_message_names_skill_library_mount(atom_home):
    seed_library(atom_home)  # adds skill_library/pdf-extract
    cmd = load_skill.func(_runtime(atom_home), name="pdf-extract")
    msg = str(cmd.update["messages"][0].content)
    assert "/mnt/skill_library/pdf-extract/" in msg


def test_load_skill_message_names_skills_mount_for_always_on(atom_home):
    d = atom_home / "skills" / "logseq-cli"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: logseq-cli\ndescription: x\n---\nBODY")
    cmd = load_skill.func(_runtime(atom_home), name="logseq-cli")
    msg = str(cmd.update["messages"][0].content)
    assert "/mnt/skills/logseq-cli/" in msg
    assert "/mnt/skill_library/" not in msg
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_search.py -k "names_skill_library_mount or names_skills_mount" -v`
Expected: FAIL (mount path not present in message).

- [ ] **Step 3: Implement the mount-aware message**

In `src/atom/tools/search.py`, add the import near the top (after the existing imports):

```python
from atom.sandbox.paths import VIRTUAL_SKILLS, VIRTUAL_SKILL_LIBRARY
```

Replace the body of `load_skill` from the existing `home = _home(runtime)` line through the final `return Command(...)` with (this replaces the existing `home = ...`/`found = ...`/guard/return block — do not leave the old `home = _home(runtime)` line behind):

```python
    home = _home(runtime)
    mount: str | None = None
    if home:
        if (Path(home) / "skills" / clean / "SKILL.md").exists():
            mount = VIRTUAL_SKILLS
        elif (Path(home) / "skill_library" / clean / "SKILL.md").exists():
            mount = VIRTUAL_SKILL_LIBRARY
    if mount is None:
        return Command(update={"messages": [ToolMessage(
            f"No skill named '{clean}' found. Check the skills catalog or use search_skills.",
            tool_call_id=tcid)]})
    # promoted_skills is a union-reducer channel (merge_name_list); returning just this name suffices.
    return Command(update={
        "promoted_skills": [clean],
        "messages": [ToolMessage(
            f"Loaded skill '{clean}'. Follow its instructions for this task. "
            f"Its bundled files are at {mount}/{clean}/.", tool_call_id=tcid)],
    })
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_search.py -v`
Expected: PASS (new tests + pre-existing `test_load_skill_*`).

- [ ] **Step 5: Commit**

```bash
git add src/atom/tools/search.py tests/test_search.py
git commit -m "feat(tools): load_skill reports the skill's bundled-files mount path"
```

---

### Task 4: Injection middlewares add a bundled-files location hint

**Files:**
- Modify: `src/atom/middleware/skill_library.py` (`_bodies` + `_inject`)
- Modify: `src/atom/middleware/skill_activation.py` (`_skill_body` + `_inject`)
- Test: `tests/test_library.py`

**Interfaces:**
- Consumes: `VIRTUAL_SKILLS`, `VIRTUAL_SKILL_LIBRARY` from Task 1.
- Produces: each injected skill guide is prefixed with the skill's bundled-files location, e.g. `# Skill: pdf-extract (bundled files: /mnt/skill_library/pdf-extract/)` for a `skill_library` skill; the slash-activation note likewise names the mount.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_library.py` (it already imports `AIMessage`, `HumanMessage`, `seed_library`, and defines `_FakeRequest`):

```python
def test_skill_library_injection_includes_bundled_files_location(atom_home):
    from atom.middleware.skill_library import SkillLibraryMiddleware

    seed_library(atom_home)  # skill_library/pdf-extract
    mw = SkillLibraryMiddleware(home=str(atom_home))
    req = _FakeRequest(tools=[], state={"promoted_skills": ["pdf-extract"]},
                       messages=[AIMessage(content="hi")])
    text = "\n".join(str(m.content) for m in mw._inject(req).messages)
    assert "/mnt/skill_library/pdf-extract/" in text
    assert "extract each page" in text  # body still injected


def test_skill_activation_injection_includes_bundled_files_location(atom_home):
    from atom.middleware.skill_activation import SkillActivationMiddleware

    d = atom_home / "skills" / "demo-skill"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: demo-skill\ndescription: x\n---\nDEMO BODY")
    mw = SkillActivationMiddleware(home=str(atom_home))
    req = _FakeRequest(tools=[], state={}, messages=[HumanMessage(content="/demo-skill go")])
    text = "\n".join(str(m.content) for m in mw._inject(req).messages)
    assert "/mnt/skills/demo-skill/" in text
    assert "DEMO BODY" in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_library.py -k "bundled_files_location" -v`
Expected: FAIL (mount path not in injected text).

- [ ] **Step 3: Add the location hint in `skill_library.py`**

In `src/atom/middleware/skill_library.py`, add the import:

```python
from atom.sandbox.paths import VIRTUAL_SKILLS, VIRTUAL_SKILL_LIBRARY
```

Replace `_bodies` and the `_inject` join line:

```python
    def _bodies(self, names: list[str]) -> list[tuple[str, str, str]]:
        out: list[tuple[str, str, str]] = []
        for name in names:
            for base, mount in (
                (self.home / "skill_library", VIRTUAL_SKILL_LIBRARY),
                (self.home / "skills", VIRTUAL_SKILLS),
            ):
                md = base / name / "SKILL.md"
                if md.exists():
                    body = parse_skill_md(md.read_text(encoding="utf-8"), name).body
                    out.append((name, mount, body))
                    break
        return out

    def _inject(self, request: Any) -> Any:
        names = request.state.get("promoted_skills") or []
        bodies = self._bodies(names)
        if not bodies:
            return request
        text = "\n\n---\n\n".join(
            f"# Skill: {n} (bundled files: {mount}/{n}/)\n\n{b}" for n, mount, b in bodies
        )
        note = HumanMessage(content=f"[Active skill guide(s) — follow these]\n\n{text}")
        return request.override(messages=[*request.messages, note])
```

- [ ] **Step 4: Add the location hint in `skill_activation.py`**

In `src/atom/middleware/skill_activation.py`, add the import:

```python
from atom.sandbox.paths import VIRTUAL_SKILLS, VIRTUAL_SKILL_LIBRARY
```

Replace `_skill_body` and the note construction in `_inject`:

```python
    def _skill_body(self, name: str) -> tuple[str, str] | None:
        for base, mount in (
            (self.home / "skills", VIRTUAL_SKILLS),
            (self.home / "skill_library", VIRTUAL_SKILL_LIBRARY),
        ):
            md = base / name / "SKILL.md"
            if md.exists():
                return mount, parse_skill_md(md.read_text(encoding="utf-8"), name).body
        return None
```

and in `_inject`, replace the `body = ...` / guard / `note = ...` block:

```python
        found = self._skill_body(m.group(1))
        if not found:
            return request
        mount, body = found
        note = HumanMessage(content=(
            f"[Activated skill '{m.group(1)}' — follow this guide. "
            f"Bundled files: {mount}/{m.group(1)}/]\n\n{body}"))
        return request.override(messages=[*messages, note])
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_library.py -v`
Expected: PASS (new tests + pre-existing `test_skill_library_injects_promoted_bodies_transiently` and the slash-activation test).

- [ ] **Step 6: Commit**

```bash
git add src/atom/middleware/skill_library.py src/atom/middleware/skill_activation.py tests/test_library.py
git commit -m "feat(middleware): inject bundled-files location with each activated skill"
```

---

## Final verification

- [ ] Run the full suite: `.venv/bin/python -m pytest -q`
- [ ] Expected: all green (no regressions).
