# System Design Analysis: The DeerFlow 2.0 Backend (ByteDance)

## TL;DR
- DeerFlow 2.0 has a genuine, first-class **middleware** system: it is a thin, config-driven layer over **LangChain v1's `AgentMiddleware`** abstraction, and the lead agent is built with **`langchain.agents.create_agent`** (NOT the older `create_react_agent`), which compiles a LangGraph `StateGraph` under the hood. Roughly 14–19 ordered middlewares implement every cross-cutting concern (sandbox, uploads, memory, summarization, subagent limits, clarification interrupts, etc.).
- Middlewares fire at LangChain's standard lifecycle hooks — `before_agent`/`after_agent`, `before_model`/`after_model`, and the wrap-style `wrap_model_call`/`wrap_tool_call` — so they trigger before/after the whole run, around each LLM call, and around each tool call. Order is load-bearing (`before_*` run in sequence, `after_*` unwind in reverse), and `ClarificationMiddleware` must run last because it interrupts the graph via `Command(goto=END)`.
- Tools are plain **LangChain `@tool`-decorated functions** (from `langchain_core.tools`), assembled per-invocation by `get_available_tools()` across five categories (sandbox, built-in, community, MCP, subagent). MCP tools enter via `MultiServerMCPClient` from `langchain-mcp-adapters`. The whole thing runs on LangGraph ≥1.x with a checkpointer for durable per-thread state.

## Key Findings

**1. It is a real middleware system, not just an analogy.** Unlike DeerFlow 1.x (a hand-wired LangGraph `StateGraph` of researcher/coder/reporter nodes), DeerFlow 2.0 — open-sourced February 27, 2026 as a ground-up rewrite that shares no code with v1 — leans on LangChain v1's middleware pattern. This aligns with the broader ecosystem shift: when LangChain and LangGraph hit v1.0 in late October 2025, the old `initialize_agent`/`AgentExecutor` patterns were removed in favor of a single idiomatic `create_agent` factory with LangGraph as the runtime underneath. In DeerFlow the lead agent is a single `create_agent(...)` call; all behavior is layered as `AgentMiddleware` subclasses. This is confirmed at the source level: `backend/packages/harness/deerflow/agents/lead_agent/agent.py` and `agents/factory.py` both `from langchain.agents import create_agent` and `from langchain.agents.middleware import AgentMiddleware`.

**2. The middleware chain is a fixed, carefully ordered list** built by `_build_middlewares()` (in `lead_agent/agent.py`) starting from `build_lead_runtime_middlewares()`. Order is load-bearing and documented in code comments.

**3. Tools follow the idiomatic LangChain `@tool` pattern** and are resolved dynamically from config via a reflection module (`use: package.module:object` strings). There is no bespoke tool-registry protocol; the registry is just config + `get_available_tools()`.

**4. Heavy reliance on LangGraph/LangChain primitives**: `create_agent`, `AgentMiddleware`, `AgentState` (extended into `ThreadState`), custom reducers, `Command(goto=END)` interrupts, a `BaseCheckpointSaver` checkpointer, `MultiServerMCPClient`, and `BaseChatModel` for models.

## Details

### Architecture context (what "the backend" is)

The backend is split into two layers with a strict, CI-enforced dependency direction (`tests/test_harness_boundary.py`):
- **Harness** — `backend/packages/harness/deerflow/` (import prefix `deerflow.*`), the publishable `deerflow-harness` package. Contains agent orchestration, tools, sandbox, models, MCP, skills, memory, and the embedded `DeerFlowClient`.
- **App** — `backend/app/` (import prefix `app.*`), the FastAPI **Gateway API** (port 8001) and IM-channel integrations (Feishu, Slack, Telegram, DingTalk, WeCom, etc.). App imports harness; harness never imports app.

Everything sits behind an Nginx reverse proxy on port 2026. The agent runtime runs *inside* the Gateway via `RunManager` + `run_agent()` + `StreamBridge` (`packages/harness/deerflow/runtime/`); Nginx exposes it at `/api/langgraph/*` and rewrites to the Gateway's native routers. `langgraph.json` exists for LangGraph Studio/Server compatibility but is not the default entrypoint.

### 1. Middleware system architecture

**Design pattern.** DeerFlow adopts LangChain v1's middleware pattern wholesale: a chain-of-responsibility / interceptor pipeline wrapped around a single ReAct-style agent graph. The lead agent is constructed with `langchain.agents.create_agent(model, tools, middleware=[...], system_prompt=..., state_schema=ThreadState, checkpointer=...)`. Internally `create_agent` compiles a LangGraph `StateGraph` with a model node and a `ToolNode`, and weaves each middleware's hooks into the graph edges.

**Tiered factory.** DeerFlow uses two factory layers:
- `create_deerflow_agent(...)` in `agents/factory.py` — the SDK-level entry point. Its docstring describes it as sitting "between the raw `langchain.agents.create_agent` primitive and the config-driven `make_lead_agent` application factory." It accepts a `RuntimeFeatures` object and translates declarative feature flags into a middleware chain via an internal `_assemble_from_features()`, then calls `create_agent(model=..., tools=effective_tools or None, middleware=effective_middleware, system_prompt=..., state_schema=effective_state or ThreadState, checkpointer=..., name=...)`.
- `make_lead_agent(config: RunnableConfig)` in `lead_agent/agent.py` — the application-level entry point registered as the graph. It reads `config.configurable` (`thinking_enabled`, `model_name`, `is_plan_mode`, `subagent_enabled`, `agent_name`), builds the model via `create_chat_model()`, loads tools via `get_available_tools()`, assembles middlewares via `_build_middlewares()`, generates the system prompt via `apply_prompt_template()`, and calls `create_agent(...)` with `state_schema=ThreadState`.

**Registration/composition.** Middlewares are *not* auto-discovered; they are appended in a deliberate, fixed order in Python code (partly in `agents/middlewares/tool_error_handling_middleware.py::build_lead_runtime_middlewares` and partly in `lead_agent/agent.py::_build_middlewares`). Some are conditional (summarization if enabled, todo if plan mode, subagent-limit if subagents enabled, view-image if the model supports vision, deferred-tool-filter if `tool_search.enabled`). Custom user middlewares are injected immediately before `ClarificationMiddleware`, which is always last. Code comments explicitly explain the ordering constraints (e.g., "ThreadDataMiddleware must be before SandboxMiddleware to ensure thread_id is available"; "ClarificationMiddleware should be last to intercept clarification requests after model calls").

**Invocation/execution.** Because it is LangChain v1 middleware, composition is nested: all `before_*` hooks run in sequence before the model, then each `wrap_model_call` nests around the actual model call, and the `after_*` hooks run in reverse order on the way back. `wrap_model_call`/`wrap_tool_call` compose like layered function wrappers (caching, retries, dynamic model requests, and per-call tool interception live here). Node-style hooks return a state dict merged via reducers; wrap-style hooks can short-circuit, retry, or inject a `Command`.

**State.** `ThreadState` (`agents/thread_state.py`) extends LangGraph's `AgentState` with `sandbox`, `thread_data`, `title`, `artifacts`, `todos`, `uploaded_files`, `viewed_images`, plus `promoted` (for deferred tools). It uses custom reducers `merge_artifacts` (dedupe) and `merge_viewed_images` (merge/clear). Middlewares communicate through this shared state.

### 2. Implemented middlewares (current main, ~19 in the assembled lead chain)

Per `backend/CLAUDE.md`, the lead-agent chain, in order:

1. **ThreadDataMiddleware** — creates per-thread isolated directories under `backend/.deer-flow/users/{user_id}/threads/{thread_id}/user-data/{workspace,uploads,outputs}`; resolves `user_id` via `get_effective_user_id()` (falls back to `"default"` in no-auth mode).
2. **UploadsMiddleware** — tracks and injects newly uploaded files into the conversation; offloads the uploads-directory scan off the event loop (`abefore_agent` via `asyncio.to_thread`).
3. **SandboxMiddleware** — acquires a sandbox and stores `sandbox_id` in state; calls `acquire()` at run start and `release()` at end.
4. **DanglingToolCallMiddleware** — injects placeholder `ToolMessage`s for `AIMessage` tool_calls that lack responses (e.g., after user interruption), preserving raw provider tool-call payloads in `additional_kwargs["tool_calls"]`.
5. **LLMErrorHandlingMiddleware** — normalizes provider/model invocation failures into recoverable, assistant-facing errors.
6. **GuardrailMiddleware** (optional, if `guardrails.enabled`) — pre-tool-call authorization via a pluggable `GuardrailProvider` protocol (built-in `AllowlistProvider`, OAP policy providers, or custom); returns an error `ToolMessage` on deny.
7. **SandboxAuditMiddleware** — audits sandboxed shell/file operations for security logging before tool execution continues.
8. **ToolErrorHandlingMiddleware** — converts tool exceptions into error `ToolMessage`s so the run continues instead of aborting.
9. **SkillActivationMiddleware** — detects `/skill-name task` syntax on the latest user message, reads `SKILL.md` from trusted skill storage, injects the skill body as hidden current-turn model context, and records an audit event.
10. **SummarizationMiddleware** (optional) — reduces context when approaching token limits; like LangChain's built-in `SummarizationMiddleware`, it implements the `before_model` hook and, when message history exceeds a token threshold, summarizes contents before passing to the model.
11. **TodoListMiddleware** (optional, plan mode) — task tracking via a `write_todos` tool.
12. **TokenUsageMiddleware** (optional) — records token-usage metrics; subagent usage is attributed back to the dispatching step.
13. **TitleMiddleware** — auto-generates a thread title after the first complete exchange.
14. **MemoryMiddleware** — queues conversations for async memory update (filters to user + final AI responses).
15. **ViewImageMiddleware** (conditional on vision support) — injects base64 image data before the LLM call.
16. **DeferredToolFilterMiddleware** (optional, if `tool_search.enabled`) — hides deferred (MCP) tool schemas from the bound model until promoted via `tool_search`.
17. **SubagentLimitMiddleware** (optional, if subagents enabled) — truncates excess `task` tool calls to enforce `MAX_CONCURRENT_SUBAGENTS` (default 3, clamped to [2,4]).
18. **LoopDetectionMiddleware** — detects repeated tool-call loops (hashes tool calls in a sliding window) and forces a final text answer when a hard limit is hit.
19. **ClarificationMiddleware** — intercepts `ask_clarification` tool calls and interrupts the graph via `Command(goto=END)`; must be last.

(The README's simplified "9 middleware" list and the "10 middleware" comment in the tree are older/abridged; `CLAUDE.md` lists the full current set.)

### 3. Middleware trigger points / lifecycle hooks

DeerFlow's middlewares implement LangChain v1's hooks (per LangChain's own definitions: `before_agent` runs once on invocation; `before_model` fires before each model call; `wrap_model_call` wraps the model call end-to-end for caching/retries/dynamic tool selection; `wrap_tool_call` wraps tool execution similarly). Mapping the main ones to their trigger points:

- **On agent start / end (`before_agent` / `after_agent`, once per invocation):**
  - ThreadDataMiddleware, UploadsMiddleware, SandboxMiddleware (`acquire` at start, `release` at end) — set up per-thread dirs, inject uploads, and stand up the sandbox before anything else runs.
  - MemoryMiddleware — `after_agent()` queues the finished conversation for debounced background extraction.
- **Before an LLM call is sent to the provider (`before_model`):**
  - DanglingToolCallMiddleware (patches missing ToolMessages before the model sees history), SummarizationMiddleware (compress context), SkillActivationMiddleware (inject skill body), ViewImageMiddleware (inject base64 image data), DeferredToolFilterMiddleware (strip hidden MCP tool schemas), and memory injection into the system prompt.
- **After a response is received from the provider (`after_model`):**
  - SubagentLimitMiddleware (truncate excess `task` calls the model just emitted), LoopDetectionMiddleware (detect repeated tool calls and hard-stop), ClarificationMiddleware (if the model emitted `ask_clarification`, interrupt via `Command(goto=END)`), TitleMiddleware (generate a title after the first exchange).
- **Around the model call (`wrap_model_call`):** LLMErrorHandlingMiddleware normalizes invocation failures; this is where retry/normalize-style logic lives.
- **Before/after each tool call (`wrap_tool_call` / pre-execution):** GuardrailMiddleware (authorize each tool call, deny → error ToolMessage), SandboxAuditMiddleware (audit shell/file ops), ToolErrorHandlingMiddleware (convert tool exceptions into error ToolMessages).

Because `after_model` hooks unwind in reverse order, ClarificationMiddleware being last means it observes the fully-processed model output before deciding to interrupt.

### 4. Included tools

`get_available_tools()` (in `tools/tools.py`) assembles the tool list per invocation across five categories:

- **Sandbox tools** (`sandbox/tools.py`): `bash` (execute shell commands; disabled by default under `LocalSandboxProvider`, enabled under `AioSandboxProvider`), `ls`, `read_file`, `write_file` (overwrites by default, `append` option), `str_replace` (serialized read-modify-write per `(sandbox.id, path)`). Plus `glob`/`grep` search. These operate over a virtual filesystem (`/mnt/user-data/{workspace,uploads,outputs}` and `/mnt/skills`) translated to per-thread physical directories.
- **Built-in tools** (`tools/builtins/`): `present_files` (surface deliverables to the user), `ask_clarification` (triggers the clarification interrupt), `view_image` (feed images to vision models), and `task` (the subagent-delegation tool). In plan mode, `write_todos`. When `tool_search.enabled`, a `tool_search` meta-tool.
- **Community tools** (`community/`): **web search** and **web fetch** with multiple providers — Tavily (web search), Jina AI (web fetch/read), Firecrawl (search + scraping), DuckDuckGo (image search), and ByteDance/BytePlus **InfoQuest** (intelligent search + crawling). Exa and Brave are also referenced as providers. Also `aio_sandbox` (the Docker sandbox provider).
- **MCP tools**: any Model Context Protocol server (stdio, SSE, HTTP transports), discovered at runtime and converted to LangChain tools.
- **Skills**: not tools per se — Markdown `SKILL.md` workflow modules injected into the prompt progressively.

Agents call these via normal LLM function-calling; the lead agent uses `task()` to fan out to subagents (`general-purpose` and `bash` built-in agents), max 3 concurrent per turn with a ~15-minute timeout, executed in background thread pools with SSE status events.

Note on scope vs. DeerFlow 1.x: the older 1.x branch shipped TTS/podcast generation and RAG/retrieval provider integrations (RAGFlow, Qdrant, Milvus, VikingDB, Dify) plus Arxiv/Brave/SearXNG search. 2.0 is a from-scratch rewrite; media generation (image/video/slides/webpages/"podcast"-style outputs) is now delivered through the **skills + sandbox** model rather than dedicated backend tool modules, and knowledge-base retrieval is handled via MCP servers rather than built-in RAG connectors.

### 5. Tool system implementation

The pattern is **idiomatic LangChain**, not custom:
- Built-in and sandbox tools are **`@tool`-decorated module-level functions** from `langchain_core.tools`. Sandbox tools reach shared state through a `runtime` parameter (`runtime.state["sandbox"]`) with lazy sandbox init (`ensure_sandbox_initialized(runtime)`); `task_tool` pulls `sandbox_state`, `thread_data`, `thread_id`, and `tool_call_id` from runtime context (using the tool-call-id as the subagent's `task_id`). (The exact injection annotations — `InjectedState`/`InjectedToolCallId`/`Command` vs. the newer `ToolRuntime` parameter — could not be captured verbatim from the raw files, which were not fetchable.)
- **Config-driven dynamic loading**: tools are declared in `config.yaml` under `tools:` with a `use:` field (e.g. `use: deerflow.community.tavily.tools:web_search_tool`) and a `group:`. A **reflection module** (`reflection/__init__.py`, `resolve_variable`/`resolve_class`) imports the object at runtime; extra config fields (e.g. `api_key`, `max_results`) are passed through. Custom tools just need to be a `BaseTool` or an `@tool` function reachable by import path — "Your tool must be a LangChain BaseTool or a function decorated with @tool."
- **Tool groups** (`tool_groups`) let `get_available_tools(groups=...)` filter which tools an agent sees.
- **MCP**: `MultiServerMCPClient` (from `langchain-mcp-adapters`) manages multiple servers and converts MCP tools to LangChain `StructuredTool`/`BaseTool` instances (`convert_mcp_tool_to_langchain_tool`). Deferred loading (`tool_search`) hides schemas until promotion to save context.
- **Execution** is via LangChain's `ToolNode` inside the `create_agent` graph; `ToolErrorHandlingMiddleware` wraps exceptions.

### 6. LangChain / LangGraph integration

- **Agent construction**: `langchain.agents.create_agent` (LangChain v1 middleware-based factory) — definitively confirmed in current source, superseding the `create_react_agent` shown in some older docs. It compiles a LangGraph `StateGraph` with a model node + `ToolNode`, weaving middleware hooks into the edges.
- **State**: `ThreadState(AgentState)` with `add_messages` and custom reducers; LangGraph channel values are serialized to JSON-safe dicts (`serialize_channel_values`) for the LangGraph Platform wire format.
- **Models**: `create_chat_model()` resolves any LangChain `BaseChatModel` via the `use:` import path (e.g., `langchain_openai:ChatOpenAI`, or custom providers like `deerflow.models.vllm_provider:VllmChatModel`, `openai_codex_provider:CodexChatModel`, `claude_provider:ClaudeChatModel`). Capability flags (`supports_thinking`, `supports_vision`, `use_responses_api`) drive middleware/model behavior.
- **Persistence**: an async checkpointer (`BaseCheckpointSaver`, e.g. SQLite/Postgres) configured in `langgraph.json`/config persists full graph state per thread; `thread_id` scopes conversation history.
- **Human-in-the-loop / interrupts**: `ClarificationMiddleware` uses `Command(goto=END)`; interrupts require the checkpointer.
- **Streaming**: LangGraph SSE protocol (`values`, `messages-tuple`, `end`) via `StreamBridge`; `RunManager` + `RunJournal` track run lifecycle and token usage.
- **Observability**: built-in LangSmith and Langfuse tracing (callbacks attached at model creation; trace metadata injected into `RunnableConfig.metadata`).
- **MCP**: `langchain-mcp-adapters`' `MultiServerMCPClient`.
- **Versions** (from `backend/README.md`): LangGraph 1.0.6+, LangChain 1.2.3+, FastAPI 0.115.0+, plus `langchain-mcp-adapters`, `agent-sandbox`, `markitdown`, `tavily-python`/`firecrawl-py`.

## Recommendations

- **To study the core design**, read these files in order: `agents/factory.py` (`create_deerflow_agent`) → `agents/lead_agent/agent.py` (`make_lead_agent`, `_build_middlewares`) → `agents/thread_state.py` (`ThreadState`) → `tools/tools.py` (`get_available_tools`) → `sandbox/middleware.py` and each middleware in `agents/middlewares/`. This traces the whole request path from factory to compiled graph. `backend/CLAUDE.md` is the single most authoritative map of the current architecture.
- **To extend behavior**, prefer the middleware seam: subclass `AgentMiddleware`, implement only the hooks you need, and pass it via `custom_middlewares` (it lands just before `ClarificationMiddleware`). To add a tool, register a `@tool` function in `config.yaml` under `tools:` with a `use:` path — no core changes required. To add external capabilities, prefer an MCP server over a bespoke tool module.
- **Thresholds that would change this analysis**: if a future release replaces `create_agent` with a hand-built `StateGraph`, or moves middleware ordering into config/plugins, the "fixed ordered list" conclusion changes — watch `_build_middlewares` and `factory.py`. Also watch the `tool_search`/deferred-MCP gate (open issues #2507 and #3341): subagents currently may bind full MCP schemas before promotion.
- **Security posture**: DeerFlow executes arbitrary code; for anything beyond a trusted localhost deployment, run under `AioSandboxProvider` (Docker) or the K8s provisioner, keep host bash disabled, and use `GuardrailMiddleware` allowlists.

## Caveats

- DeepWiki and some Mintlify/Medium docs show a `make_lead_agent` that calls `create_react_agent` with `backend/src/...` paths. That reflects an **older snapshot**; the current main branch uses `langchain.agents.create_agent` under `backend/packages/harness/deerflow/...`, verified directly in source at recent commits. Treat any `create_react_agent`/`src/`-path description as stale.
- The exact number of middlewares varies by config (conditional middlewares) and by doc vintage (README says 9; the tree comment says 10; `CLAUDE.md` enumerates ~19). The `CLAUDE.md` list is the most current.
- The precise tool-injection annotations (`InjectedState`/`InjectedToolCallId`/`Command`) and the exact `pyproject.toml` version pins for the harness package could not be captured verbatim (raw files were not fetchable); versions cited come from `backend/README.md`'s "Technology Stack" section and may lag the lockfile.
- DeerFlow is under very active development (the repo has grown from roughly 1,500 commits in March 2026 toward 2,100+, with well over 100 contributors — figures are date-sensitive and vary by source). Specific line numbers and middleware counts will drift.
