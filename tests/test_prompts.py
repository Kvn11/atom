"""Prompt rendering: @file/inline resolution, precedence, StrictUndefined, and the default prompt."""

from __future__ import annotations

import pytest

from atom.prompts.render import render_prompt


def test_render_inline_and_file_precedence(tmp_path):
    assert render_prompt("A {{ x }}", {"x": "1"}) == "A 1"  # inline rendered
    (tmp_path / "p.md").write_text("FROM_CONFIG {{ x }}")
    assert render_prompt("@p.md", {"x": "1"}, config_dir=str(tmp_path)) == "FROM_CONFIG 1"  # @file
    with pytest.raises(FileNotFoundError):
        render_prompt("@nope.md", {}, config_dir=str(tmp_path))


def test_render_strict_undefined_raises_on_typo():
    from jinja2 import UndefinedError

    with pytest.raises(UndefinedError):
        render_prompt("Hello {{ workpace }}", {"workspace": "/mnt"})  # typo -> loud failure
    assert render_prompt('{{ name | default("atom") }}', {}) == "atom"  # default filter still works


def test_default_lead_prompt_renders_and_reflects_toggles(base_config):
    from atom.agent import render_lead_system_prompt

    prof = base_config.profile("default")
    out = render_lead_system_prompt(
        base_config, prof, "default", {"supports_vision": True},
        frequent_tool_names=["read_file", "bash", "write_todos"],
        has_tool_library=True, has_skill_library=False,
    )
    assert "read_file" in out and "bash" in out
    assert "search_tools" in out          # has_tool_library -> discovery section present
    assert "search_skills" not in out     # has_skill_library False -> that bullet absent


def test_ask_clarification_is_return_direct():
    from atom.tools.clarification import ask_clarification

    assert ask_clarification.return_direct is True  # matches the module docstring's claim


def test_summary_prompt_keeps_placeholder_and_notes_pin():
    from atom.prompts.render import resolve_prompt_ref

    text = resolve_prompt_ref("@prompts/summary.md")
    assert "{messages}" in text                 # SummarizationMiddleware .format() contract
    assert "pinned" in text.lower()             # tells the summarizer the instruction is pinned
    assert "verbatim" in text.lower()
    assert "## PLAN STATE" in text              # checklist structure present
    assert "## WORKSPACE & FILES" in text
