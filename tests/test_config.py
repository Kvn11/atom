"""Tests for AtomConfig defaults."""

from __future__ import annotations

from atom.config.schema import AtomConfig, TodosConfig


def test_todos_config_defaults():
    cfg = AtomConfig()
    assert isinstance(cfg.todos, TodosConfig)
    assert cfg.todos.continuation_nudge is True
    assert cfg.todos.max_nudges == 2
