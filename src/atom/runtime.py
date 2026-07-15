"""Run entrypoint: provision context, open the checkpointer, and drive the lead agent.

``run_agent`` is the programmatic API; the CLI (``atom.cli``) wraps it. Workspace mode is a
per-run argument here (not config), per deviation #7.
"""

from __future__ import annotations

import uuid
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Benign pydantic<->langgraph serializer warning about the typed context object.
warnings.filterwarnings("ignore", message="Pydantic serializer warnings")

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from atom.agent import PreparedModel, build_lead_agent, prepare_model
from atom.checkpoint import open_checkpointer
from atom.config import load_config
from atom.config.schema import AtomConfig
from atom.messages import message_text
from atom.observability import _apply_trace
from atom.prompts import render_prompt
from atom.sandbox.paths import atom_home
from atom.state import WorkspaceContext


@dataclass
class RunResult:
    thread_id: str
    messages: list[BaseMessage]
    final_text: str
    state: dict[str, Any] = field(default_factory=dict)
    awaiting_clarification: bool = False

    @property
    def title(self) -> str | None:
        return self.state.get("title")


def _build_context(cfg: AtomConfig, *, user_id, thread_id, profile_name, home, workspace, caps, window, uploads=None) -> WorkspaceContext:
    if workspace in (None, "new"):
        mode, wpath = "new", None
    else:
        mode, wpath = "existing", str(Path(workspace).expanduser().resolve())
    return {
        "user_id": user_id,
        "thread_id": thread_id,
        "profile_name": profile_name,
        "home": home,
        "workspace_mode": mode,
        "workspace_path": wpath,
        "uploads_path": str(Path(uploads).expanduser().resolve()) if uploads else None,
        "allow_bash": cfg.sandbox.bash_enabled,
        "supports_vision": bool(caps.get("supports_vision")),
        "context_window": window,
    }


def build_run_config(thread_id: str, recursion_limit: int, trace: dict | None = None) -> dict:
    """Assemble the LangGraph invoke config: thread id + recursion_limit (+ optional trace).

    ``recursion_limit`` counts super-steps, not agent turns. atom's middleware chain spends
    ~11 super-steps per turn, so this must be well above the intended turn count (see
    ``AgentProfile.recursion_limit``).
    """
    return _apply_trace(
        {"configurable": {"thread_id": thread_id}, "recursion_limit": recursion_limit}, trace
    )


async def run_agent(
    task: str,
    *,
    config: AtomConfig | None = None,
    config_path: str | None = None,
    profile: str | None = None,
    thread_id: str | None = None,
    workspace: str = "new",
    uploads: str | None = None,
    user_id: str | None = None,
    override_model: str | None = None,
    override_thinking: str | int | None = None,
    override_system_prompt: str | None = None,
    trace: dict | None = None,
    prepared: PreparedModel | None = None,
    notes: dict | None = None,
) -> RunResult:
    """Run the lead agent on ``task`` and return the final result.

    ``prepared`` lets callers inject a pre-built model (used by tests to avoid a real provider).
    """
    cfg = config or load_config(config_path)
    profile_name = profile or cfg.defaults.agent
    prof = cfg.profile(profile_name)
    user_id = user_id or cfg.defaults.user_id
    thread_id = thread_id or uuid.uuid4().hex[:12]
    home = str(atom_home(cfg.home))

    prepared = prepared or prepare_model(prof, override_model, override_thinking)
    context = _build_context(
        cfg,
        user_id=user_id,
        thread_id=thread_id,
        profile_name=profile_name,
        home=home,
        workspace=workspace,
        uploads=uploads,
        caps=prepared.caps,
        window=prepared.context_window,
    )

    content = render_prompt(prof.user_prompt, {"task": task}, cfg.config_dir) if prof.user_prompt else task
    db_path = Path(home) / "atom.sqlite"

    async with open_checkpointer(cfg.checkpointer.backend, db_path) as cp:
        agent = build_lead_agent(
            cfg, profile_name, prepared=prepared, checkpointer=cp,
            override_model=override_model, override_thinking=override_thinking,
            override_system_prompt=override_system_prompt, trace=trace, notes=notes,
        )
        run_config = build_run_config(thread_id, prof.recursion_limit, trace)
        result = await agent.ainvoke(
            {"messages": [HumanMessage(content=content)]},
            config=run_config,
            context=context,
        )

    from atom.middleware.clarification import pending_clarification

    messages = result.get("messages", [])
    pending = pending_clarification(messages)
    if pending is not None:
        args = pending.get("args", {})
        question = args.get("question", "(clarification requested)")
        extras = []
        if args.get("context"):
            extras.append(f"({args['context']})")
        if args.get("options"):
            extras.append("Options: " + "; ".join(args["options"]))
        final_text = "\n".join([question, *extras])
        return RunResult(
            thread_id=thread_id, messages=messages, final_text=final_text,
            state=result, awaiting_clarification=True,
        )

    final_text = ""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            text = message_text(msg)
            if text.strip():
                final_text = text
                break
    return RunResult(thread_id=thread_id, messages=messages, final_text=final_text, state=result)
