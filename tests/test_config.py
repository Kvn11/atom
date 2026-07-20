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
    assert cfg.notes.expose_to_logseq is False       # field default is off; config.yaml turns it on
    assert cfg.notes.logseq_root_dir is None


def test_notes_runtime_config_from_yaml():
    from atom.config.schema import AtomConfig
    cfg = AtomConfig.model_validate(
        {"notes": {"expose_to_logseq": True, "logseq_root_dir": "~/logseq"}}
    )
    assert cfg.notes.expose_to_logseq is True
    assert cfg.notes.logseq_root_dir == "~/logseq"


def test_overflow_and_tool_cap_defaults():
    cfg = AtomConfig()
    assert cfg.compaction.overflow_recovery is True
    assert cfg.compaction.overflow_max_attempts == 3
    assert cfg.compaction.overflow_target_ratio == 0.5
    assert cfg.profile("default").tools.max_output_chars == 100_000
