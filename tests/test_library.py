"""Library subsystem: BM25 search, deferred-tool hiding, and end-to-end promotion."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from atom.library import load_library
from atom.middleware.deferred_tools import DeferredToolFilterMiddleware
from atom.runtime import run_agent
from tests.conftest import make_prepared, seed_library


def _tc(name, args, cid):
    return {"name": name, "args": args, "id": cid, "type": "tool_call"}


def test_bm25_search_finds_relevant(atom_home):
    seed_library(atom_home)
    index = load_library(atom_home)
    assert index.has_tools and index.has_skills
    tools = index.search_tools("how many words in this string", k=3)
    assert tools and tools[0].tool.name == "wordcount"
    skills = index.search_skills("read a pdf file", k=3)
    assert skills and skills[0].name == "pdf-extract"
    assert index.deferred_tool_names() == {"wordcount"}


@dataclass
class _FakeRequest:
    """Minimal stand-in for ModelRequest used to unit-test the deferred filter."""

    tools: list
    state: dict = field(default_factory=dict)
    tool_call: dict = field(default_factory=dict)
    messages: list = field(default_factory=list)

    def override(self, *, tools=None, messages=None, **_: Any) -> "_FakeRequest":
        return _FakeRequest(
            tools=tools if tools is not None else self.tools,
            state=self.state,
            tool_call=self.tool_call,
            messages=messages if messages is not None else self.messages,
        )


class _T:
    def __init__(self, name):
        self.name = name


def _seed_second_count_tool(home):
    from pathlib import Path

    home = Path(home)
    d = home / "tool_library" / "linecount"
    d.mkdir(parents=True, exist_ok=True)
    (d / "tool.py").write_text(
        "from langchain.tools import tool\n\n"
        "@tool(parse_docstring=True)\n"
        "def linecount(text: str) -> str:\n"
        '    """Count the lines in text.\n\n    Args:\n        text: text.\n    """\n'
        "    return str(len(text.splitlines()))\n"
    )
    (d / "manifest.yaml").write_text(
        "name: linecount\ndescription: Count the number of lines in text\n"
        "keywords: [lines, count, text]\ntier: deferred\nentrypoint: linecount\n"
    )


def test_deferred_filter_hides_until_promoted(atom_home):
    seed_library(atom_home)
    index = load_library(atom_home)
    wc = index.tools[0].tool
    other = index  # any object with a .name; use a simple stub instead

    class _T:
        name = "read_file"

    mw = DeferredToolFilterMiddleware({"wordcount"})

    # Not promoted -> wordcount hidden.
    req = _FakeRequest(tools=[wc, _T()], state={})
    visible = {getattr(t, "name", None) for t in mw._filter(req).tools}
    assert visible == {"read_file"}

    # Promoted -> wordcount visible.
    req2 = _FakeRequest(tools=[wc, _T()], state={"promoted": {"names": ["wordcount"]}})
    visible2 = {getattr(t, "name", None) for t in mw._filter(req2).tools}
    assert visible2 == {"read_file", "wordcount"}


@pytest.mark.asyncio
async def test_search_then_use_deferred_tool(base_config, atom_home):
    seed_library(atom_home)
    prepared = make_prepared([
        AIMessage(content="", tool_calls=[_tc("search_tools", {"query": "count words"}, "s1")]),
        AIMessage(content="", tool_calls=[_tc(
            "wordcount", {"text": "one two three four"}, "w1")]),
        AIMessage(content="There are 4 words."),
    ])
    result = await run_agent("count the words in a phrase", config=base_config, prepared=prepared)

    # search_tools promoted wordcount, and wordcount actually executed (returned "4").
    assert result.state.get("promoted", {}).get("names") == ["wordcount"]
    tool_msgs = [m for m in result.messages if isinstance(m, ToolMessage)]
    assert any(m.content == "4" for m in tool_msgs), [m.content for m in tool_msgs]
    assert "4 words" in result.final_text


def test_merge_promoted_and_name_list_reducers():
    from atom.reducers import merge_name_list, merge_promoted

    assert merge_promoted({"names": ["a"]}, {"names": ["b"]})["names"] == ["a", "b"]
    assert merge_promoted(None, {"names": ["x"], "catalog_hash": "h"}) == {
        "names": ["x"],
        "catalog_hash": "h",
    }
    assert merge_name_list(["b"], ["a", "b"]) == ["a", "b"]
    assert merge_name_list(None, ["z"]) == ["z"]


@pytest.mark.asyncio
async def test_parallel_search_tools_does_not_crash(base_config, atom_home):
    seed_library(atom_home)
    # Two search_tools calls in a SINGLE AIMessage -> two writes to `promoted` in one super-step.
    prepared = make_prepared([
        AIMessage(content="", tool_calls=[
            _tc("search_tools", {"query": "count words"}, "s1"),
            _tc("search_tools", {"query": "length of text"}, "s2"),
        ]),
        AIMessage(content="done"),
    ])
    result = await run_agent("count words two ways", config=base_config, prepared=prepared)
    assert result.state.get("promoted", {}).get("names") == ["wordcount"]


@pytest.mark.asyncio
async def test_search_skills_records_promotion_and_confirms(base_config, atom_home):
    seed_library(atom_home)
    prepared = make_prepared([
        AIMessage(content="", tool_calls=[_tc("search_skills", {"query": "extract pdf text"}, "k1")]),
        AIMessage(content="Following the pdf-extract skill now."),
    ])
    result = await run_agent("get text from a pdf", config=base_config, prepared=prepared)
    assert "pdf-extract" in result.state.get("promoted_skills", [])
    tool_msgs = [m for m in result.messages if isinstance(m, ToolMessage)]
    # The ToolMessage is a short confirmation; the BODY is injected transiently by
    # SkillLibraryMiddleware (not persisted into history / summarized away).
    assert any("pdf-extract" in m.content for m in tool_msgs)
    assert not any("extract each page" in m.content for m in tool_msgs)  # body not persisted


def test_ranker_min_score_and_catalog_hash(atom_home):
    seed_library(atom_home)
    index = load_library(atom_home)
    assert len(index.search_tools("count words", k=3)) == 1        # default gate passes matches
    assert index.search_tools("count words", k=3, min_score=1.1) == []  # impossible gate drops all
    assert isinstance(index.catalog_hash, str) and index.catalog_hash


def test_deferred_filter_respects_catalog_hash():
    mw = DeferredToolFilterMiddleware({"wordcount"}, catalog_hash="H1")
    tools = [_T("read_file"), _T("wordcount")]
    stale = _FakeRequest(tools=tools, state={"promoted": {"names": ["wordcount"], "catalog_hash": "OLD"}})
    assert {t.name for t in mw._filter(stale).tools} == {"read_file"}  # stale promotion ignored
    fresh = _FakeRequest(tools=tools, state={"promoted": {"names": ["wordcount"], "catalog_hash": "H1"}})
    assert {t.name for t in mw._filter(fresh).tools} == {"read_file", "wordcount"}


def test_deferred_exec_guard_blocks_unpromoted_tool():
    mw = DeferredToolFilterMiddleware({"wordcount"}, catalog_hash="H1")
    req = _FakeRequest(
        tools=[], state={"promoted": {"names": [], "catalog_hash": "H1"}},
        tool_call={"name": "wordcount", "id": "w1", "args": {}},
    )
    blocked = mw.wrap_tool_call(req, lambda r: "SHOULD_NOT_RUN")
    assert isinstance(blocked, ToolMessage) and blocked.status == "error"
    # A promoted tool passes through to the handler.
    req2 = _FakeRequest(
        tools=[], state={"promoted": {"names": ["wordcount"], "catalog_hash": "H1"}},
        tool_call={"name": "wordcount", "id": "w1", "args": {}},
    )
    assert mw.wrap_tool_call(req2, lambda r: "RAN") == "RAN"


def test_skill_activation_injects_slash_skill_body(atom_home):
    from atom.middleware.skill_activation import SkillActivationMiddleware

    seed_library(atom_home)
    mw = SkillActivationMiddleware(home=str(atom_home))
    req = _FakeRequest(tools=[], state={}, messages=[HumanMessage(content="/pdf-extract do it")])
    text = "\n".join(str(m.content) for m in mw._inject(req).messages)
    assert "extract each page" in text  # skill body injected for a leading /skill-name
    # No slash -> request unchanged.
    plain = [HumanMessage(content="just do it")]
    assert mw._inject(_FakeRequest(tools=[], state={}, messages=list(plain))).messages == plain


def test_skill_library_injects_promoted_bodies_transiently(atom_home):
    from atom.middleware.skill_library import SkillLibraryMiddleware

    seed_library(atom_home)
    mw = SkillLibraryMiddleware(home=str(atom_home))
    base = [AIMessage(content="hi")]
    req = _FakeRequest(tools=[], state={"promoted_skills": ["pdf-extract"]}, messages=list(base))
    injected = mw._inject(req)
    text = "\n".join(str(m.content) for m in injected.messages)
    assert "extract each page" in text  # the skill body is injected for this call
    # No promoted skills -> request unchanged.
    assert mw._inject(_FakeRequest(tools=[], state={}, messages=list(base))).messages == base


@pytest.mark.asyncio
async def test_search_tools_promotes_at_most_auto_promote_k(base_config, atom_home):
    seed_library(atom_home)
    _seed_second_count_tool(atom_home)  # now two tools match "count"
    base_config.library.auto_promote_k = 1
    prepared = make_prepared([
        AIMessage(content="", tool_calls=[_tc("search_tools", {"query": "count things"}, "s1")]),
        AIMessage(content="ok"),
    ])
    result = await run_agent("count things", config=base_config, prepared=prepared)
    assert len(result.state.get("promoted", {}).get("names", [])) == 1  # bounded by auto_promote_k
