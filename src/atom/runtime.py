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
    cancelled: bool = False

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


def build_run_config(
    thread_id: str, recursion_limit: int, trace: dict | None = None, obs_provider=None,
) -> dict:
    """Assemble the LangGraph invoke config: thread id + recursion_limit (+ optional trace).

    When an observability provider is supplied, it decorates the config (LangFuse attaches its
    CallbackHandler and stamps the run-level session id; LangSmith/none are no-ops).

    ``recursion_limit`` counts super-steps, not agent turns. atom's middleware chain spends
    ~11 super-steps per turn, so this must be well above the intended turn count (see
    ``AgentProfile.recursion_limit``).
    """
    config = _apply_trace(
        {"configurable": {"thread_id": thread_id}, "recursion_limit": recursion_limit}, trace
    )
    if obs_provider is not None:
        obs_provider.decorate_run_config(config)
    return config


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
    on_event: "Callable[[dict], Awaitable[None]] | None" = None,
    obs_provider=None,
    on_transcript: "Callable[[list], None] | None" = None,
    should_cancel: "Callable[[], bool] | None" = None,
) -> RunResult:
    """Run the lead agent on ``task`` and return the final result.

    ``prepared`` lets callers inject a pre-built model (used by tests to avoid a real provider).

    ``on_transcript`` is a failure hook: if the agent loop raises, whatever messages the
    checkpointer has persisted so far are recovered and handed to it (best-effort) BEFORE the
    error is re-raised, so a failed run's partial transcript can still be saved for later viewing.
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

    cancelled = False
    async with open_checkpointer(cfg.checkpointer.backend, db_path) as cp:
        agent = build_lead_agent(
            cfg, profile_name, prepared=prepared, checkpointer=cp,
            override_model=override_model, override_thinking=override_thinking,
            override_system_prompt=override_system_prompt, trace=trace, notes=notes,
            obs_provider=obs_provider,
        )
        run_config = build_run_config(thread_id, prof.recursion_limit, trace, obs_provider)
        inp = {"messages": [HumanMessage(content=content)]}
        try:
            if on_event is not None and cfg.streaming.enabled:
                from atom.streaming import translate_message_chunk, translate_update

                async for item in agent.astream(
                    inp, config=run_config, context=context, stream_mode=["messages", "updates"],
                ):
                    # Compiled-graph astream yields (mode, data) tuples; the create_agent sugar yields
                    # {"type","data"} dicts. Normalize both so the translator sees one shape.
                    mode, data = item if isinstance(item, tuple) else (item.get("type"), item.get("data"))
                    if mode == "messages":
                        chunk, metadata = data
                        # "model" is create_agent's dedicated node for the primary agent-loop LLM
                        # call (langchain.agents.factory: graph.add_node("model", ...)). Middleware
                        # hooks are their own nodes too (e.g. "TitleMiddleware.after_model"), and any
                        # out-of-band model.invoke() they make (title generation reusing the lead
                        # model when no summarizer_model is configured) is still captured by
                        # stream_mode="messages" — filter to the real node so that text doesn't
                        # duplicate into the live stream.
                        if (metadata or {}).get("langgraph_node") == "model":
                            for ev in translate_message_chunk(chunk, metadata):
                                await on_event(ev)
                    elif mode == "updates":
                        for _node, update in (data or {}).items():
                            msgs = update.get("messages") if isinstance(update, dict) else None
                            for ev in translate_update(msgs or []):
                                await on_event(ev)
                        if should_cancel is not None and should_cancel():
                            cancelled = True
                            break
                # aget_state gives the authoritative final channel values (messages + artifacts + title),
                # equivalent to what ainvoke returned — the checkpointer is still open in this context.
                result = (await agent.aget_state(run_config)).values
            else:
                result = await agent.ainvoke(inp, config=run_config, context=context)
        except Exception:
            # The agent loop failed. Recover whatever the checkpointer persisted so far (it is still
            # open in this context) and hand it to the failure hook so the caller can keep the
            # partial transcript, then re-raise unchanged — task-failure semantics are the caller's.
            if on_transcript is not None:
                partial: list = []
                try:
                    partial = (await agent.aget_state(run_config)).values.get("messages", [])
                except Exception:  # noqa: BLE001 — best-effort recovery must never mask the real error
                    partial = []
                if partial:
                    try:
                        on_transcript(partial)
                    except Exception:  # noqa: BLE001 — persistence must never mask the real error
                        pass
            raise

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
            state=result, awaiting_clarification=True, cancelled=cancelled,
        )

    final_text = ""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            text = message_text(msg)
            if text.strip():
                final_text = text
                break
    return RunResult(thread_id=thread_id, messages=messages, final_text=final_text,
                     state=result, cancelled=cancelled)
