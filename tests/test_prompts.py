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
        notes={"provider": "obsidian", "vault": "notes-smoke", "root_dir": "/n/notes-smoke"},
    )
    assert "Persistent notes" in out
    assert "obsidian vault=notes-smoke" in out and "/n/notes-smoke" in out
    assert "logseq" not in out.lower()


def test_lead_prompt_no_notes_block_when_absent(base_config):
    from atom.agent import render_lead_system_prompt

    prof = base_config.profile("default")
    out = render_lead_system_prompt(
        base_config, prof, "default", {"supports_vision": True},
        frequent_tool_names=["read_file"])
    assert "Persistent notes" not in out


def test_build_lead_agent_renders_notes_into_system_prompt(base_config, monkeypatch):
    # Closes the seam between the render unit test (which bypasses build_lead_agent) and the
    # engine-forwarding test (which spies on run_agent): prove build_lead_agent actually hands a
    # vault-aware system prompt to create_agent, so deleting `notes=notes` at the render call fails.
    import atom.agent as agent_mod
    from atom.agent import build_lead_agent
    from langchain_core.messages import AIMessage
    from tests.conftest import make_prepared

    captured: dict = {}
    real_create = agent_mod.create_agent

    def spy_create(*args, **kwargs):
        captured["system_prompt"] = kwargs.get("system_prompt")
        return real_create(*args, **kwargs)

    monkeypatch.setattr(agent_mod, "create_agent", spy_create)
    build_lead_agent(
        base_config, "default", prepared=make_prepared([AIMessage(content="x")]),
        notes={"provider": "obsidian", "vault": "vaultxyz", "root_dir": "/n/dir-xyz"},
    )
    sp = captured["system_prompt"]
    assert "Persistent notes" in sp
    assert "/n/dir-xyz" in sp and "obsidian vault=vaultxyz" in sp


@pytest.mark.asyncio
async def test_run_agent_forwards_notes_to_build_lead_agent(base_config, monkeypatch):
    # The other half of the wire: run_agent must forward `notes` into build_lead_agent (a deletion
    # at runtime.py's build_lead_agent call would otherwise pass every existing test).
    import atom.runtime as rt
    from atom.runtime import run_agent
    from langchain_core.messages import AIMessage
    from tests.conftest import make_prepared

    captured: dict = {}
    real_build = rt.build_lead_agent

    def spy_build(*args, **kwargs):
        captured["notes"] = kwargs.get("notes")
        return real_build(*args, **kwargs)

    monkeypatch.setattr(rt, "build_lead_agent", spy_build)
    await run_agent(
        "hi", config=base_config, prepared=make_prepared([AIMessage(content="done")]),
        notes={"provider": "obsidian", "vault": "demo", "root_dir": "/x"},
    )
    assert captured["notes"] == {"provider": "obsidian", "vault": "demo", "root_dir": "/x"}


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
        skill_catalog=[{"name": "demo-skill", "description": "A demo skill"}],
        has_tool_library=False, has_skill_library=False,
    )
    assert "demo-skill" in out and "A demo skill" in out
    assert "load_skill" in out
    assert "FULL BODY" not in out               # only frontmatter, never the body
    assert "Skills (load before use)" in out


@pytest.mark.asyncio
async def test_load_skill_tool_bound_when_skill_present(base_config, atom_home):
    from langchain_core.messages import AIMessage, ToolMessage
    from atom.runtime import run_agent
    from tests.conftest import make_prepared

    d = atom_home / "skills" / "demo-skill"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: demo-skill\ndescription: A demo skill\n---\nBODY")
    prepared = make_prepared([
        AIMessage(content="", tool_calls=[
            {"name": "load_skill", "args": {"name": "demo-skill"}, "id": "l1", "type": "tool_call"}]),
        AIMessage(content="done"),
    ])
    result = await run_agent("do it", config=base_config, prepared=prepared)
    tool_msgs = [m for m in result.messages if isinstance(m, ToolMessage)]
    assert any("Loaded skill 'demo-skill'" in m.content for m in tool_msgs)


def test_lead_prompt_advertises_skill_library_mount(base_config):
    from atom.agent import render_lead_system_prompt

    prof = base_config.profile("default")
    out = render_lead_system_prompt(
        base_config, prof, "default", {"supports_vision": True},
        frequent_tool_names=["read_file"],
    )
    assert "/mnt/skill_library" in out


def test_subagent_prompts_render_and_report_contract():
    from atom.prompts.render import render_prompt

    ctx = {
        "date": "2026-07-05",
        "workspace": "/w",
        "uploads": "/u",
        "outputs": "/o",
        "frequent_tool_names": ["read_file", "write_file"],
        "skill_catalog": [],
        "notes": None,   # _child_system always supplies notes (None -> no vault block)
    }
    for ref in ("@prompts/subagent_general.md", "@prompts/subagent_bash.md"):
        out = render_prompt(ref, ctx)
        assert "read_file" in out                          # tool list rendered
        assert "self-contained report" in out              # return-value contract
    bash_out = render_prompt("@prompts/subagent_bash.md", ctx)
    assert "bash" in bash_out
