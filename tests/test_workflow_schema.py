"""Workflow YAML schema, loading, input validation, and prompt templating."""
from __future__ import annotations

import pytest

from atom.workflow.schema import (
    MissingInputError, StepDef, TaskDef, WorkflowDef,
    list_workflows, load_workflow, render_task_prompt, resolve_inputs, resolve_workflow_path,
)


def _write(home, name, text):
    d = home / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.yaml").write_text(text)


DEMO = """
name: demo
description: A demo workflow.
inputs:
  - name: topic
    required: true
  - name: style
    required: false
    default: free verse
steps:
  - title: Draft
    tasks:
      - id: poet_a
        prompt: "Write about {{ topic }} in {{ style }}."
        model: haiku
        thinking: low
      - prompt: "Second poem about {{ inputs.topic }}."
"""


def test_load_workflow_parses_steps_and_defaults(atom_home):
    _write(atom_home, "demo", DEMO)
    wf = load_workflow("demo", str(atom_home))
    assert wf.name == "demo"
    assert [s.title for s in wf.steps] == ["Draft"]
    # first task keeps its id; second gets an auto id.
    assert [t.id for t in wf.steps[0].tasks] == ["poet_a", "task_2"]
    assert wf.steps[0].tasks[0].model == "haiku"


def test_list_workflows_returns_all(atom_home):
    _write(atom_home, "demo", DEMO)
    names = {w.name for w in list_workflows(str(atom_home))}
    assert "demo" in names


def test_resolve_inputs_requires_required_and_fills_defaults(atom_home):
    _write(atom_home, "demo", DEMO)
    wf = load_workflow("demo", str(atom_home))
    with pytest.raises(MissingInputError):
        resolve_inputs(wf, {})                       # topic missing
    resolved = resolve_inputs(wf, {"topic": "the sea"})
    assert resolved == {"topic": "the sea", "style": "free verse"}


def test_render_task_prompt_templates_inputs(atom_home):
    _write(atom_home, "demo", DEMO)
    wf = load_workflow("demo", str(atom_home))
    inputs = resolve_inputs(wf, {"topic": "rain", "style": "haiku"})
    assert render_task_prompt(wf.steps[0].tasks[0], inputs) == "Write about rain in haiku."


def test_notes_smoke_workflow_valid():
    import yaml
    from pathlib import Path

    data = yaml.safe_load(Path("workflows/notes-smoke.yaml").read_text())
    wf = WorkflowDef.model_validate(data)
    assert wf.name == "notes-smoke"
    assert wf.notes.enabled is True
    assert [s.title for s in wf.steps] == ["Recall", "Record"]


def test_duplicate_task_ids_rejected():
    with pytest.raises(Exception):
        StepDef(title="s", tasks=[TaskDef(id="x", prompt="a"), TaskDef(id="x", prompt="b")])


def test_empty_steps_rejected():
    with pytest.raises(Exception):
        WorkflowDef(name="w", steps=[])


def test_notes_defaults_disabled():
    wf = WorkflowDef.model_validate(
        {"name": "w", "steps": [{"title": "s", "tasks": [{"prompt": "p"}]}]})
    assert wf.notes.enabled is False
    assert wf.notes.provider == "obsidian"
    assert wf.notes.vault is None


def test_notes_block_parses():
    wf = WorkflowDef.model_validate({
        "name": "w",
        "notes": {"enabled": True, "vault": "my-vault"},
        "steps": [{"title": "s", "tasks": [{"prompt": "p"}]}],
    })
    assert wf.notes.enabled is True and wf.notes.vault == "my-vault"


def test_resolve_inputs_treats_null_as_missing(atom_home):
    _write(atom_home, "demo", DEMO)
    wf = load_workflow("demo", str(atom_home))
    with pytest.raises(MissingInputError):
        resolve_inputs(wf, {"topic": None})          # required None -> missing
    assert resolve_inputs(wf, {"topic": "x", "style": None}) == {"topic": "x", "style": "free verse"}  # optional None -> default


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


# --- built-in workflow resolution (self-improve ships bundled; user dir overrides) ---

def test_builtin_workflow_resolves_without_user_dir(atom_home):
    # empty $ATOM_HOME/workflows/ — the bundled self-improve must still load.
    wf = load_workflow("self-improve", str(atom_home))
    assert wf.name == "self-improve"


def test_list_workflows_includes_builtins(atom_home):
    # no user workflows at all -> the built-ins still show up.
    names = {w.name for w in list_workflows(str(atom_home))}
    assert "self-improve" in names


def test_user_workflow_overrides_builtin_of_same_name(atom_home):
    # a user file named self-improve.yaml takes precedence over the bundled built-in.
    _write(atom_home, "self-improve", DEMO.replace("name: demo", "name: self-improve"))
    path = resolve_workflow_path("self-improve", str(atom_home))
    assert path == atom_home / "workflows" / "self-improve.yaml"      # user copy wins
    wf = load_workflow("self-improve", str(atom_home))
    assert [s.title for s in wf.steps] == ["Draft"]                   # the user's content, not the built-in's
    names = [w.name for w in list_workflows(str(atom_home))]
    assert names.count("self-improve") == 1                           # de-duped, not listed twice


def test_unknown_workflow_still_raises(atom_home):
    assert resolve_workflow_path("ghost", str(atom_home)) is None
    with pytest.raises(FileNotFoundError):
        load_workflow("ghost", str(atom_home))


def test_taskdef_recursion_limit_defaults_none_and_accepts_int():
    assert TaskDef(prompt="x").recursion_limit is None
    assert TaskDef(prompt="x", recursion_limit=600).recursion_limit == 600
