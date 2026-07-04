"""Workflow YAML schema, loading, input validation, and prompt templating."""
from __future__ import annotations

import pytest

from atom.workflow.schema import (
    MissingInputError, StepDef, TaskDef, WorkflowDef,
    list_workflows, load_workflow, render_task_prompt, resolve_inputs,
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


def test_duplicate_task_ids_rejected():
    with pytest.raises(Exception):
        StepDef(title="s", tasks=[TaskDef(id="x", prompt="a"), TaskDef(id="x", prompt="b")])


def test_empty_steps_rejected():
    with pytest.raises(Exception):
        WorkflowDef(name="w", steps=[])
