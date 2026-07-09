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


def test_lead_prompt_notes_block_renders(base_config):
    from atom.agent import render_lead_system_prompt

    prof = base_config.profile("default")
    out = render_lead_system_prompt(
        base_config, prof, "default", {"supports_vision": True},
        frequent_tool_names=["read_file"],
        notes={"provider": "logseq", "root_dir": "/n/notes-smoke", "graph": "notes-smoke"},
    )
    assert "Persistent notes" in out
    assert "notes-smoke" in out and "/n/notes-smoke" in out


def test_lead_prompt_no_notes_block_when_absent(base_config):
    from atom.agent import render_lead_system_prompt

    prof = base_config.profile("default")
    out = render_lead_system_prompt(
        base_config, prof, "default", {"supports_vision": True},
        frequent_tool_names=["read_file"])
    assert "Persistent notes" not in out


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


def test_lead_prompt_keeps_contract_and_adds_discipline(base_config):
    from atom.agent import render_lead_system_prompt

    prof = base_config.profile("default")
    out = render_lead_system_prompt(
        base_config, prof, "default", {"supports_vision": True},
        frequent_tool_names=["read_file", "bash", "write_todos"],
        has_tool_library=True, has_skill_library=False,
    )
    assert "read_file" in out and "bash" in out          # tool-name contract preserved
    assert "search_tools" in out and "search_skills" not in out
    assert "present_files" in out                         # deliverable discipline
    assert "Plan before you act" in out                   # planning discipline anchor


def test_lead_prompt_renders_skill_catalog_not_body(base_config):
    from atom.agent import render_lead_system_prompt

    prof = base_config.profile("default")
    out = render_lead_system_prompt(
        base_config, prof, "default", {"supports_vision": True},
        frequent_tool_names=["read_file", "load_skill"],
        skill_catalog=[{"name": "logseq-cli", "description": "Operate the Logseq CLI"}],
        has_tool_library=False, has_skill_library=False,
    )
    assert "logseq-cli" in out and "Operate the Logseq CLI" in out
    assert "load_skill" in out
    assert "FULL BODY" not in out               # only frontmatter, never the body
    assert "Skills (load before use)" in out


@pytest.mark.asyncio
async def test_load_skill_tool_bound_when_skill_present(base_config, atom_home):
    from langchain_core.messages import AIMessage, ToolMessage
    from atom.runtime import run_agent
    from tests.conftest import make_prepared

    d = atom_home / "skills" / "logseq-cli"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: logseq-cli\ndescription: Operate Logseq\n---\nUSE THE CLI")
    prepared = make_prepared([
        AIMessage(content="", tool_calls=[
            {"name": "load_skill", "args": {"name": "logseq-cli"}, "id": "l1", "type": "tool_call"}]),
        AIMessage(content="done"),
    ])
    result = await run_agent("do it", config=base_config, prepared=prepared)
    tool_msgs = [m for m in result.messages if isinstance(m, ToolMessage)]
    assert any("Loaded skill 'logseq-cli'" in m.content for m in tool_msgs)


def test_subagent_prompts_render_and_report_contract():
    from atom.prompts.render import render_prompt

    ctx = {
        "date": "2026-07-05",
        "workspace": "/w",
        "uploads": "/u",
        "outputs": "/o",
        "frequent_tool_names": ["read_file", "write_file"],
        "skill_catalog": [],
    }
    for ref in ("@prompts/subagent_general.md", "@prompts/subagent_bash.md"):
        out = render_prompt(ref, ctx)
        assert "read_file" in out                          # tool list rendered
        assert "self-contained report" in out              # return-value contract
    bash_out = render_prompt("@prompts/subagent_bash.md", ctx)
    assert "bash" in bash_out
