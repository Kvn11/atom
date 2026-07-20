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
    """Opt-in persistent notes for a workflow, backed by a registered Obsidian vault."""

    enabled: bool = False
    provider: Literal["obsidian"] = "obsidian"
    vault: Optional[str] = None   # registered vault name; defaults (in atom.notes) to the workflow name


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


# Core-platform workflows bundled inside the package (shipped as package-data). These must be
# available on every install without the user copying anything into $ATOM_HOME/workflows/.
# schema.py is at src/atom/workflow/schema.py, so parents[1] == src/atom (mirrors prompts/render.py).
BUILTIN_WORKFLOWS_DIR = Path(__file__).resolve().parents[1] / "builtin_workflows"


def workflows_dir(home: str | None = None) -> Path:
    return atom_home(home) / "workflows"


def resolve_workflow_path(name: str, home: str | None = None) -> Optional[Path]:
    """Locate a workflow's YAML: the user's $ATOM_HOME/workflows/ takes precedence, then the
    bundled built-ins. A user file of the same name overrides the built-in. Returns None if
    neither has it."""
    user = workflows_dir(home) / f"{name}.yaml"
    if user.exists():
        return user
    builtin = BUILTIN_WORKFLOWS_DIR / f"{name}.yaml"
    if builtin.exists():
        return builtin
    return None


def load_workflow(name: str, home: str | None = None) -> WorkflowDef:
    path = resolve_workflow_path(name, home)
    if path is None:
        raise FileNotFoundError(
            f"workflow '{name}' not found in {workflows_dir(home)} or bundled built-ins")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return WorkflowDef.model_validate(data)


def list_workflows(home: str | None = None) -> list[WorkflowDef]:
    """All available workflows: the user's $ATOM_HOME/workflows/ plus bundled built-ins, with a
    user file overriding a built-in of the same name. Malformed files are skipped."""
    out: dict[str, WorkflowDef] = {}
    # Built-ins first so a user file of the same name overwrites (takes precedence).
    for base in (BUILTIN_WORKFLOWS_DIR, workflows_dir(home)):
        if not base.is_dir():
            continue
        for p in sorted(base.glob("*.yaml")):
            try:
                wf = WorkflowDef.model_validate(yaml.safe_load(p.read_text()) or {})
            except Exception:  # noqa: BLE001 — skip malformed files in listings
                continue
            out[wf.name] = wf
    return list(out.values())


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
