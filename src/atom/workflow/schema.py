"""Workflow definitions (Steps x Tasks), YAML loading, input validation, prompt templating."""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import Literal, Optional, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from atom.prompts.render import apply_prompt_template
from atom.sandbox.paths import VIRTUAL_OUTPUTS, VIRTUAL_UPLOADS, VIRTUAL_WORKSPACE, atom_home


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")


class InputDef(_Base):
    name: str
    type: Literal["text", "file"] = "text"
    required: bool = False
    description: Optional[str] = None
    default: Optional[str] = None


class NotesConfig(_Base):
    """Opt-in persistent notes for a workflow (a per-workflow Logseq vault shared across runs)."""

    enabled: bool = False
    provider: Literal["logseq"] = "logseq"
    graph: Optional[str] = None   # default (resolved in atom.notes): slug of the workflow name


class TaskDef(_Base):
    id: Optional[str] = None
    prompt: str
    model: Optional[str] = None
    thinking: Optional[Union[str, int]] = None


class StepDef(_Base):
    title: str
    description: Optional[str] = None
    tasks: list[TaskDef] = Field(default_factory=list)

    @field_validator("tasks")
    @classmethod
    def _non_empty(cls, v: list) -> list:
        if not v:
            raise ValueError("a step must define at least one task")
        return v

    @model_validator(mode="after")
    def _assign_and_check_ids(self) -> "StepDef":
        seen: set[str] = set()
        for i, t in enumerate(self.tasks):
            if not t.id:
                t.id = f"task_{i + 1}"
            if t.id in seen:
                raise ValueError(f"duplicate task id '{t.id}' in step '{self.title}'")
            seen.add(t.id)
        return self


class WorkflowDef(_Base):
    name: str
    description: Optional[str] = None
    inputs: list[InputDef] = Field(default_factory=list)
    notes: NotesConfig = Field(default_factory=NotesConfig)
    steps: list[StepDef] = Field(default_factory=list)

    @field_validator("steps")
    @classmethod
    def _non_empty(cls, v: list) -> list:
        if not v:
            raise ValueError("a workflow must define at least one step")
        return v


class MissingInputError(ValueError):
    """Raised when required workflow inputs are absent."""


def workflows_dir(home: str | None = None) -> Path:
    return atom_home(home) / "workflows"


def load_workflow(name: str, home: str | None = None) -> WorkflowDef:
    path = workflows_dir(home) / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"workflow '{name}' not found at {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return WorkflowDef.model_validate(data)


def list_workflows(home: str | None = None) -> list[WorkflowDef]:
    d = workflows_dir(home)
    if not d.is_dir():
        return []
    out: list[WorkflowDef] = []
    for p in sorted(d.glob("*.yaml")):
        try:
            out.append(WorkflowDef.model_validate(yaml.safe_load(p.read_text()) or {}))
        except Exception:  # noqa: BLE001 — skip malformed files in listings
            continue
    return out


def resolve_inputs(workflow: WorkflowDef, provided: dict) -> dict:
    provided = provided or {}
    resolved: dict = {}
    missing: list[str] = []
    for inp in workflow.inputs:
        if inp.name in provided and provided[inp.name] is not None and str(provided[inp.name]).strip() != "":
            resolved[inp.name] = provided[inp.name]
        elif inp.type != "file" and inp.default is not None:   # a text default is meaningless for a file input
            resolved[inp.name] = inp.default
        elif inp.required:
            missing.append(inp.name)
        else:
            resolved[inp.name] = ""
    if missing:
        raise MissingInputError(f"missing required input(s): {', '.join(missing)}")
    return resolved


def render_task_prompt(task: TaskDef, inputs: dict) -> str:
    ctx = {
        **inputs,
        "inputs": inputs,
        "workspace": VIRTUAL_WORKSPACE,
        "uploads": VIRTUAL_UPLOADS,
        "outputs": VIRTUAL_OUTPUTS,
        "date": datetime.date.today().isoformat(),
    }
    return apply_prompt_template(task.prompt, ctx)
