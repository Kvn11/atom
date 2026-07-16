"""Shared test fixtures: a scripted fake chat model, a tmp ATOM_HOME, and configs."""

from __future__ import annotations

import os
from typing import Any, Sequence

import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

from atom.agent import PreparedModel
from atom.config.schema import AtomConfig, AgentProfile


DEFAULT_PROFILE_DATA = {
    "max_input_tokens": 200_000,
    "max_output_tokens": 64_000,
    "image_inputs": True,
    "reasoning_output": True,
    "tool_calling": True,
}


class ScriptedChatModel(BaseChatModel):
    """Returns a fixed sequence of AIMessages; supports bind_tools. ``profile`` is the
    inherited BaseChatModel field (set at construction)."""

    responses: list[AIMessage] = []
    _i: int = PrivateAttr(default=0)

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "ScriptedChatModel":
        return self  # scripted responses ignore the bound tools

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        idx = min(self._i, len(self.responses) - 1)
        self._i += 1
        msg = self.responses[idx]
        return ChatResult(generations=[ChatGeneration(message=msg)])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return self._generate(messages, stop=stop, **kwargs)

    @property
    def _llm_type(self) -> str:
        return "scripted"


def make_prepared(responses: list[AIMessage], profile: dict | None = None) -> PreparedModel:
    model = ScriptedChatModel(responses=responses, profile=profile or DEFAULT_PROFILE_DATA)
    caps = {
        "context_window": model.profile["max_input_tokens"],
        "max_output_tokens": model.profile["max_output_tokens"],
        "supports_vision": model.profile["image_inputs"],
        "supports_reasoning": model.profile["reasoning_output"],
        "has_profile": True,
    }
    return PreparedModel(model=model, caps=caps, context_window=caps["context_window"])


@pytest.fixture
def atom_home(tmp_path, monkeypatch):
    home = tmp_path / "atomhome"
    home.mkdir()
    monkeypatch.setenv("ATOM_HOME", str(home))
    return home


@pytest.fixture
def base_config(atom_home) -> AtomConfig:
    return AtomConfig(
        home=str(atom_home),
        checkpointer={"backend": "memory"},
        agents={"default": AgentProfile(model="haiku")},
    )


_WORDCOUNT_TOOL = '''
from langchain.tools import tool


@tool(parse_docstring=True)
def wordcount(text: str) -> str:
    """Count the words in a piece of text.

    Args:
        text: The text to count words in.
    """
    return str(len(text.split()))
'''

_WORDCOUNT_MANIFEST = """
name: wordcount
description: Count the number of words in a piece of text
keywords: [words, count, text, length]
tier: deferred
entrypoint: wordcount
"""

_PDF_SKILL = """---
name: pdf-extract
description: How to extract text from a PDF document
keywords: [pdf, extract, text, document]
tier: deferred
---
Step 1: open the PDF. Step 2: extract each page's text. Step 3: concatenate and save.
"""


from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk


class StreamingTextChatModel(BaseChatModel):
    """Streams a fixed text word-by-word via _astream so astream(stream_mode='messages') yields
    multiple text deltas. Falls back to _agenerate for the ainvoke path."""
    text: str = "hello streamed world"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.text))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return self._generate(messages)

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        for word in self.text.split(" "):
            piece = word + " "
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=piece))
            if run_manager:
                await run_manager.on_llm_new_token(piece, chunk=chunk)
            yield chunk

    @property
    def _llm_type(self) -> str:
        return "streaming-text"


def make_streaming_prepared(text: str = "hello streamed world") -> PreparedModel:
    model = StreamingTextChatModel(text=text, profile=DEFAULT_PROFILE_DATA)
    caps = {
        "context_window": model.profile["max_input_tokens"],
        "max_output_tokens": model.profile["max_output_tokens"],
        "supports_vision": model.profile["image_inputs"],
        "supports_reasoning": model.profile["reasoning_output"],
        "has_profile": True,
    }
    return PreparedModel(model=model, caps=caps, context_window=caps["context_window"])


def seed_library(home) -> None:
    """Create one deferred tool (wordcount) and one deferred skill (pdf-extract)."""
    from pathlib import Path

    home = Path(home)
    tool_dir = home / "tool_library" / "wordcount"
    tool_dir.mkdir(parents=True, exist_ok=True)
    (tool_dir / "tool.py").write_text(_WORDCOUNT_TOOL)
    (tool_dir / "manifest.yaml").write_text(_WORDCOUNT_MANIFEST)

    skill_dir = home / "skill_library" / "pdf-extract"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(_PDF_SKILL)
