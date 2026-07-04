"""Workspace new/existing provisioning + the multi-provider model registry."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from atom.config.schema import AgentProfile, AtomConfig
from atom.models import REGISTRY, model_caps, resolve_context_window, resolve_spec
from atom.runtime import run_agent
from atom.sandbox import thread_paths
from tests.conftest import ScriptedChatModel, make_prepared


def _tc(name, args, cid):
    return {"name": name, "args": args, "id": cid, "type": "tool_call"}


def test_thread_paths_new_vs_existing(atom_home, tmp_path):
    new = thread_paths("u", "t1")
    assert not new.workspace_is_external
    assert new.workspace.name == "workspace"

    ext = tmp_path / "proj"
    existing = thread_paths("u", "t1", workspace_override=str(ext))
    assert existing.workspace_is_external
    assert existing.workspace == ext.resolve()


@pytest.mark.asyncio
async def test_existing_workspace_binds_external_dir(atom_home, tmp_path):
    ext = tmp_path / "checkout"
    ext.mkdir()
    (ext / "data.txt").write_text("EXISTING CONTENT")
    cfg = AtomConfig(home=str(atom_home), checkpointer={"backend": "memory"},
                     agents={"default": AgentProfile(model="haiku")})
    prepared = make_prepared([
        AIMessage(content="", tool_calls=[_tc(
            "read_file", {"description": "read", "path": "/mnt/user-data/workspace/data.txt"}, "r1")]),
        AIMessage(content="", tool_calls=[_tc(
            "write_file", {"description": "w", "path": "/mnt/user-data/workspace/out.txt",
                           "content": "NEW"}, "w1")]),
        AIMessage(content="done"),
    ])
    result = await run_agent("work here", config=cfg, prepared=prepared, workspace=str(ext))

    tms = [m for m in result.messages if isinstance(m, ToolMessage)]
    assert any("EXISTING CONTENT" in m.content for m in tms)   # read the pre-existing file
    assert (ext / "out.txt").read_text() == "NEW"              # wrote into the external dir


def test_registry_covers_all_four_providers():
    assert {s.provider for s in REGISTRY.values()} == {"anthropic", "openai", "google_genai", "qwen"}


def test_resolve_spec_key_and_raw_and_unknown():
    assert resolve_spec("haiku").provider == "anthropic"
    assert resolve_spec("openai:gpt-4o").model_name == "gpt-4o"
    with pytest.raises(KeyError):
        resolve_spec("nonexistent-model")


def test_caps_profile_first_then_fallback():
    spec = resolve_spec("qwen-max")  # qwen fallbacks matter (often no profile)
    with_profile = ScriptedChatModel(responses=[], profile={"max_input_tokens": 4242, "image_inputs": True})
    assert resolve_context_window(with_profile, spec) == 4242
    assert model_caps(with_profile, spec)["supports_vision"] is True

    no_profile = ScriptedChatModel(responses=[], profile={})
    assert resolve_context_window(no_profile, spec) == spec.context_window
    assert model_caps(no_profile, spec)["supports_vision"] == spec.supports_vision
