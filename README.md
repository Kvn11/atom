# atom

An **agentic middleware harness** built on LangChain v1. A single lead agent is
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

**Prerequisites for persistent-notes workflows:** the `obsidian` CLI must be on your `PATH`
(guaranteed on target devices; verify with `obsidian version`), the Obsidian app must be running
during runs, and the vault named by the workflow must already be **registered** in Obsidian
(`obsidian vaults` lists the known ones).

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
- **Two-tier tool/skill libraries** — frequent tools are bound up front and skills in
  `$ATOM_HOME/skills/` are auto-advertised as a name+description catalog; the rest live in
  `$ATOM_HOME/tool_library` and `skill_library` and are found via `search_tools` / `search_skills`
  (BM25). Tools are promoted on demand; skill bodies are pulled in with `load_skill`.
- **Compaction** at 50% of the selected model's context window.
- **Workspace** provisioned new or bound to an existing directory (a per-run choice).
- **Clarification** interrupts the turn and resumes on the next message.
- Local **sandbox** with path confinement; `bash` on by default (⚠️ no container isolation yet —
  docker is Phase 2). A dormant `GuardrailMiddleware` can gate commands.

## Workflows

Run many agents as ordered **steps** of parallel **tasks** sharing one workspace.

```bash
cp workflows/parallel-poems.yaml ~/.atom/workflows/          # make it discoverable
atom workflow list
atom workflow run parallel-poems --input topic="the tide" --input style=haiku
atom serve                                                   # REST API + web UI at http://127.0.0.1:8000
```

A workflow is defined in YAML (`$ATOM_HOME/workflows/<name>.yaml`): workflow-level `inputs`
(required/optional, used in task prompts via `{{ topic }}`), ordered `steps`, and each step's
`tasks` (a `prompt` plus optional `model`/`thinking`). Tasks in a step run in parallel; a step
advances only if **all** its tasks succeed, otherwise the run halts. Later steps read what earlier
steps wrote to the shared workspace. Each task can be traced to LangSmith — see Observability below.
The API (`atom serve`) is automation-first: `POST /api/runs` to submit a job, poll
`GET /api/runs/{id}`, then `GET /api/runs/{id}/artifacts`.

**Persistent notes.** Add a `notes:` block to a workflow to give it long-term memory that
persists across runs:

```yaml
notes:
  enabled: true          # bind a registered Obsidian vault to this workflow, shared by every run
  vault: my-vault        # the Obsidian vault name; defaults to the workflow name
```

The vault is an ordinary Obsidian vault — a folder of markdown notes you have **registered** in
Obsidian (once, via "Open folder as vault"). atom does **not** create, register, or delete vaults:
when a notes-enabled run starts, atom validates that `notes.vault` (defaulting to the workflow name)
is a registered vault via `obsidian vaults`, resolves its path, and injects a snippet into each
task's system prompt telling the agent to reach it with the `obsidian` CLI — always passing
`vault=<name>` (e.g. `obsidian vault=<name> read file="<Note>"`, `... append file="<Note>"
content="..."`). If the named vault isn't registered, the run **halts cleanly** and tells you to
open it in Obsidian first. The Obsidian app must be running during a run (the CLI bridges to it).

Try it with `workflows/notes-smoke.yaml`: register a vault named `notes-smoke` in Obsidian, then run
the workflow twice — the second run's Recall step reads the first run's note.

### File inputs

Declare a file input by giving it `type: file` alongside the usual `required`/`description`:

```yaml
inputs:
  - name: document
    type: file
    required: true
    description: The document to summarize (a text file).
```

Uploaded bytes are copied into the run's own uploads directory and shared with every task on a
read-only mount; `{{ document }}` in a task `prompt` resolves to the mount path
(`/mnt/user-data/uploads/document.<ext>`, extension taken from the uploaded file), so agents can
`read_file` it like any other path. From the CLI, supply the file with `--file name=path`:

```bash
atom workflow run summarize-doc --file document=~/report.txt
```

The web UI renders a native file picker for any `type: file` input instead of a text box. Upload
limits are configured under `uploads:` in `config.yaml`: `max_file_bytes` (default 25 MiB) and
`allowed_extensions` (empty allows any extension) are enforced on both the CLI and API upload
paths, while `max_files_per_run` guards the API's multipart endpoint (the CLI `--file` path is
naturally bounded by the workflow's declared file inputs). See `workflows/summarize-doc.yaml` for
a complete example.

### Workflow queue

Workflow invocations run through a durable, config-driven queue so they execute one at a time
(by default) instead of all at once — which keeps sub-agent fan-out from hitting provider rate
limits. Configure it in `config.yaml`:

```yaml
queue:
  max_concurrent_runs: 1   # how many workflow RUNS execute at once; raise as compute grows
  poll_interval_seconds: 3 # worker re-scan interval for cross-process enqueues + crash recovery
```

- **Durable:** an enqueued run is written to `$ATOM_HOME/workflows/runs/<id>/run.json` (status
  `queued`) before it starts, so it is never lost. If the server dies mid-run, the next
  `atom serve` startup re-queues interrupted runs and resumes them at step granularity (finished
  steps are skipped).
- **One drainer:** the `atom serve` process drains the queue. When no server is running,
  `atom workflow run` drains its own run in-process under a `flock` lease
  (`$ATOM_HOME/workflows/queue/worker.lock`), so a CLI run and a server can never overlap.
- **`queue.max_concurrent_runs`** caps concurrent *runs*; **`workflow.max_parallel`** (separate)
  caps concurrent *tasks within a step*.

#### Exporting a run for offline evaluation

If the run was executed with observability enabled (`observability.enabled: true` and a
`LANGSMITH_API_KEY` in the environment), download its full LangSmith trace tree to disk:

    atom workflow export <run_id>              # one run by id
    atom workflow export --latest <workflow>   # newest run of a workflow
    atom workflow export --all <workflow>      # every run of a workflow
    atom workflow export <run_id> --task 0:writer   # just one completed task (step 0, task "writer")

The whole-run form writes `$ATOM_HOME/workflows/runs/<run_id>/export.json`; `--task` writes
`.../runs/<run_id>/exports/s<step>__<task>.json` — either is a self-contained record holding the
raw LangSmith run tree (lead task(s) plus nested sub-agent and per-LLM-call runs, with
reasoning/thinking blocks intact), the run's `run.json` manifest (inputs + per-task verdict), and a
`complete` flag. A whole run can be exported once all steps complete; a single task once it reaches
a terminal state (`succeeded`/`failed`), so partial runs are still salvageable task-by-task. Both are
also available from the web UI (**Export run** in the run header, **Export task** in the transcript
toolbar). It is the input to the separate offline evaluation pipeline. Runs executed without
observability have nothing to download (the command reports "no traces found").

### Self-improving workflows

Every finished run (`complete` or `halted`) shows an **Improve** button in its run header (hidden
on `self-improve` runs themselves, to avoid recursion). Clicking it reduces the run to a compact
run-log (manifest + `chats/` transcript, plus per-call token/timing/tool-failure metrics when
available) and launches the `self-improve` workflow against it: three analysts review failures and
tool errors, performance bottlenecks and context hotspots, and workflow structure/prompts, then a
synthesis step drafts two artifacts — `improved-<name>.yaml`, an improved version of the source
workflow, and `suggestions.md`, a report of everything a YAML edit can't fix plus a changelog.
Nothing is auto-applied: review the deliverables on the new run and copy the YAML into
`$ATOM_HOME/workflows/` by hand if you like it.

The feature is itself a workflow, but a **built-in** one: `self-improve.yaml` ships bundled with
the package (`src/atom/builtin_workflows/`), so the Improve button works out of the box on any
install — no copy into `$ATOM_HOME/workflows/` required. Placing your own `self-improve.yaml`
there overrides the built-in if you want to customize it.
Per-call token/context/timing detail in the analysis requires observability (LangSmith or LangFuse)
to have been enabled for the *source* run; if it wasn't, the analysis still runs from the manifest
and transcript alone, just without those metrics.

## Observability (LangSmith)

Workflow runs can be traced to [LangSmith](https://smith.langchain.com). Enable it via the
`observability:` block in `config.yaml` or the standard `LANGSMITH_*` env vars (env wins):

```yaml
observability:
  enabled: true
  project: atom-workflows
```

Set `LANGSMITH_API_KEY` in `.env`. Tracing turns on only when a key is present.

Each workflow task is its own LangSmith **thread** (keyed by `session_id` = the task thread id).
Sub-agents are tagged `role:subagent` / `is_subagent` and grouped into their parent lead agent's
thread. Every run carries eval-ready metadata: `workflow` / `run_id` / `step_*` / `task_id`, the
`model` / `thinking` / `context_window` / `recursion_limit`, compaction settings, and a **prompt
fingerprint** (`system_prompt_ref` + `system_prompt_sha`, plus `summary_prompt_*` for the lead) so a
prompt version can be correlated with run outcomes. Filter in the UI by tags such as
`workflow:<name>`, `profile:<name>`, `model:<name>`, `role:lead` / `role:subagent`.

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
  (`name`, `description`, `keywords`) + a markdown body. Discover it with `search_skills` and load
  it with `load_skill("<name>")`. Skills in `$ATOM_HOME/skills/<name>/SKILL.md` are auto-discovered
  into an always-on catalog (name + description) in every agent's prompt (lead + sub-agents) and
  loaded on demand with `load_skill`.
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
