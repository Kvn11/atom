# atom

A DeerFlow-style **agentic middleware harness** built on LangChain v1. A single lead agent is
constructed with `langchain.agents.create_agent`, and every cross-cutting concern (workspace,
compaction, planning, subagents, tool/skill libraries, clarification, …) is a small, ordered
`AgentMiddleware`. atom is designed to be the reusable foundation for AI projects: models,
prompts, tools, skills, and workspaces are all configured at run time, not hardcoded.

Phase 1 (this repo) is the **harness**: no frontend, no docker, no MCP, no web tools.

## Install

```bash
python3.11 -m venv .venv           # 3.11+ required
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env               # add the API key(s) you have
```

Set the key for whatever model you run — e.g. `ANTHROPIC_API_KEY` for the default Claude Haiku.
(Providers: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `DASHSCOPE_API_KEY`.)

## Use

```bash
atom run "create hello.py that prints hi, run it, and show the output"
atom run "summarize data.csv" --workspace /path/to/existing/project   # reuse an existing dir
atom run "..." --model opus --profile researcher                       # override model/profile
atom run "..." --thinking high --system-prompt "You are terse."        # override reasoning/prompt at run time
atom chat                                                              # interactive REPL on one thread
atom threads                                                           # list threads with workspaces
```

Everything persists per thread under `$ATOM_HOME` (default `~/.atom`); resume a thread by passing
`--thread <id>`.

## What's built in

- **Multi-provider models** — Anthropic, OpenAI, Gemini, Alibaba Qwen. Context window +
  capabilities come from the live model profile (with a static fallback registry).
- **Planning (TODOs)** and **subagent delegation (`delegate_task`)** are **always on**.
- **Two-tier tool/skill libraries** — frequent tools/skills are bound/injected up front; the rest
  live in `$ATOM_HOME/tool_library` and `skill_library` and are found via `search_tools` /
  `search_skills` (BM25) and promoted on demand.
- **Compaction** at 50% of the selected model's context window.
- **Workspace** provisioned new or bound to an existing directory (a per-run choice).
- **Clarification** interrupts the turn and resumes on the next message.
- Local **sandbox** with path confinement; `bash` on by default (⚠️ no container isolation yet —
  docker is Phase 2). A dormant `GuardrailMiddleware` can gate commands.

## Configure (`config.yaml`)

The whole harness is config-driven. An **agent profile** defines one project's lead agent:

```yaml
agents:
  researcher:
    model: opus                       # registry key or "provider:model"
    thinking: medium                  # off | low|medium|high | adaptive (Opus)
    system_prompt: "@prompts/my_system.md"   # inline string OR @file
    tools:  { frequent: [read_file, write_file, edit_file, bash, ls, grep, glob, present_files] }
    skills: { frequent: [] }
```

Run it with `atom run "..." --profile researcher`. Workspace mode is **not** a profile field — it's
the per-run `--workspace` argument.

## Extend

- **A library tool**: create `$ATOM_HOME/tool_library/<name>/` with a `manifest.yaml`
  (`name`, `description`, `keywords`, `tier: deferred`, `entrypoint: <fn>`) and a `tool.py`
  defining a `@tool`. It becomes discoverable via `search_tools`.
- **A skill**: create `$ATOM_HOME/skill_library/<name>/SKILL.md` with YAML front-matter
  (`name`, `description`, `keywords`) + a markdown body. Discoverable via `search_skills`.
  Put always-on skills in `$ATOM_HOME/skills/<name>/SKILL.md` and list them in a profile's
  `skills.frequent`.
- **A middleware**: subclass `AgentMiddleware`, implement the hooks you need, and add it to the
  ordered list in `src/atom/agent.py::_build_middlewares`.

## Layout

```
src/atom/
  agent.py           # build_lead_agent: profile -> model -> prompt -> ordered middleware -> create_agent
  runtime.py         # run_agent entrypoint; cli.py: the atom CLI
  config/            # pydantic schema + yaml loader (AgentProfile, ...)
  models/            # registry + factory + profile-first caps/context-window
  sandbox/           # ATOM_HOME layout + LocalSandboxProvider (confinement, bash, glob, grep)
  tools/             # @tool functions (filesystem, bash, present_files, view_image, clarification,
                     #   search_tools/search_skills, delegate_task)
  middleware/        # the ordered chain (thread_data, sandbox, compaction, clarification, ...)
  library.py         # tool/skill library index (BM25) + loaders
  subagent.py        # SubagentRunner (child agents sharing the workspace)
  prompts/           # default prompt templates (overridable per profile)
```

## Roadmap

Phase 2: docker sandbox (swap the provider inside `SandboxMiddleware`), MCP tools (via the deferred
promotion path), active guardrails, long-term memory, a UI, and embedding-based library search.
