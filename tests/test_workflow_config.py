"""Workflow engine config defaults."""
from __future__ import annotations

from atom.config.schema import AtomConfig, WorkflowConfig


def test_workflow_config_defaults():
    cfg = AtomConfig()
    assert cfg.workflow.max_parallel == 4
    assert cfg.workflow.task_timeout_seconds == 1800


def test_workflow_config_override():
    wc = WorkflowConfig(max_parallel=2, task_timeout_seconds=60)
    assert wc.max_parallel == 2 and wc.task_timeout_seconds == 60
