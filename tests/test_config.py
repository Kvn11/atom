"""Tests for AtomConfig defaults."""

from __future__ import annotations

from atom.config.schema import AtomConfig, TodosConfig


def test_todos_config_defaults():
    cfg = AtomConfig()
    assert isinstance(cfg.todos, TodosConfig)
    assert cfg.todos.continuation_nudge is True
    assert cfg.todos.max_nudges == 2


def test_notes_runtime_config_defaults():
    from atom.config.schema import AtomConfig
    cfg = AtomConfig()
    assert cfg.notes.obsidian_cli == "obsidian"      # device CLI that bridges to the running app


def test_notes_runtime_config_from_yaml():
    from atom.config.schema import AtomConfig
    cfg = AtomConfig.model_validate({"notes": {"obsidian_cli": "/opt/obsidian"}})
    assert cfg.notes.obsidian_cli == "/opt/obsidian"


def test_overflow_and_tool_cap_defaults():
    cfg = AtomConfig()
    assert cfg.compaction.overflow_recovery is True
    assert cfg.compaction.overflow_max_attempts == 3
    assert cfg.compaction.overflow_target_ratio == 0.5
    assert cfg.profile("default").tools.max_output_chars == 100_000
