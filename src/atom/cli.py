"""atom CLI (``atom run|chat|threads``). A thin wrapper over :func:`atom.runtime.run_agent`."""

from __future__ import annotations

import asyncio
import os
import uuid
import warnings
from pathlib import Path

import typer
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import ValidationError
from rich.console import Console

from atom.config import load_config
from atom.middleware.llm_error import ProviderUnavailableError
from atom.runtime import RunResult, run_agent
from atom.sandbox.paths import atom_home

# Cosmetic: silence a benign pydantic<->langgraph serializer warning about the typed context.
warnings.filterwarnings("ignore", message="Pydantic serializer warnings")

app = typer.Typer(add_completion=False, help="atom — an agentic harness.")
console = Console()


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:  # noqa: BLE001
        pass


def _print_activity(result: RunResult) -> None:
    for msg in result.messages:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for call in msg.tool_calls:
                desc = call.get("args", {}).get("description") or call.get("args", {}).get("query", "")
                console.print(f"  [dim]→ {call['name']}[/dim] [dim italic]{str(desc)[:70]}[/dim italic]")
        elif isinstance(msg, ToolMessage) and getattr(msg, "status", None) == "error":
            console.print(f"  [red]! {str(msg.content)[:100]}[/red]")
    if result.state.get("artifacts"):
        console.print("\n[bold]Deliverables:[/bold]")
        for art in result.state["artifacts"]:
            console.print(f"  • {art.get('path')}")


@app.command()
def run(
    task: str = typer.Argument(..., help="The task for the agent."),
    profile: str = typer.Option(None, "--profile", "-p", help="Agent profile name."),
    model: str = typer.Option(None, "--model", "-m", help="Override the model (registry key or provider:model)."),
    thinking: str = typer.Option(None, "--thinking", help="Override reasoning: off|low|medium|high|adaptive|<int budget>."),
    system_prompt: str = typer.Option(None, "--system-prompt", help="Override the system prompt (inline string or @file)."),
    workspace: str = typer.Option("new", "--workspace", "-w", help="'new' or an absolute path to an existing dir."),
    thread: str = typer.Option(None, "--thread", "-t", help="Thread id (resume an existing thread)."),
    config: str = typer.Option(None, "--config", "-c", help="Path to config.yaml."),
    user: str = typer.Option(None, "--user", help="User id."),
) -> None:
    """Run the agent once on TASK."""
    _load_env()
    from atom.observability import build_provider

    obs_provider = None
    try:
        cfg = load_config(config)
        obs_provider = build_provider(cfg)   # never raises; NullProvider unless tracing configured
        with console.status("[bold]thinking…[/bold]"):
            result = asyncio.run(run_agent(
                task, config=cfg, profile=profile, override_model=model,
                override_thinking=thinking, override_system_prompt=system_prompt,
                workspace=workspace, thread_id=thread, user_id=user,
                obs_provider=obs_provider,
            ))
    except (ProviderUnavailableError, ValidationError, KeyError, FileNotFoundError) as e:
        console.print(f"[red]Error: {type(e).__name__}: {e}[/red]")
        raise typer.Exit(1)
    finally:
        if obs_provider is not None:
            try:
                obs_provider.flush()          # drain the trace queue before the process exits
            except Exception:  # noqa: BLE001 — telemetry flush must never break the CLI
                pass
    _print_activity(result)
    console.print()
    if result.awaiting_clarification:
        console.print(f"[yellow bold]Needs clarification:[/yellow bold] {result.final_text}")
    else:
        console.print(result.final_text or "[dim](no text answer)[/dim]")
    console.print(f"\n[dim]thread: {result.thread_id}"
                  + (f"  ·  title: {result.title}" if result.title else "") + "[/dim]")


@app.command()
def chat(
    profile: str = typer.Option(None, "--profile", "-p"),
    model: str = typer.Option(None, "--model", "-m"),
    thinking: str = typer.Option(None, "--thinking"),
    system_prompt: str = typer.Option(None, "--system-prompt"),
    workspace: str = typer.Option("new", "--workspace", "-w"),
    thread: str = typer.Option(None, "--thread", "-t"),
    config: str = typer.Option(None, "--config", "-c"),
    user: str = typer.Option(None, "--user"),
) -> None:
    """Interactive REPL on a single thread (type 'exit' to quit)."""
    _load_env()
    from atom.observability import build_provider

    try:
        cfg = load_config(config)
    except (ValidationError, KeyError, FileNotFoundError) as e:
        console.print(f"[red]Error: {type(e).__name__}: {e}[/red]")
        raise typer.Exit(1)
    obs_provider = build_provider(cfg)   # built once for the session; NullProvider unless configured
    tid = thread
    console.print("[bold]atom chat[/bold] — type 'exit' to quit.\n")
    while True:
        try:
            task = console.input("[bold cyan]you ›[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if task.lower() in {"exit", "quit"}:
            break
        if not task:
            continue
        try:
            with console.status("[bold]thinking…[/bold]"):
                result = asyncio.run(run_agent(
                    task, config=cfg, profile=profile, override_model=model,
                    override_thinking=thinking, override_system_prompt=system_prompt,
                    # Keep the ORIGINAL workspace across turns: pinning the thread already reuses a
                    # 'new' per-thread dir, and an 'existing' bind MUST persist (don't reset to 'new').
                    workspace=workspace, thread_id=tid, user_id=user,
                    obs_provider=obs_provider,
                ))
        except (ProviderUnavailableError, ValidationError, KeyError, FileNotFoundError) as e:
            console.print(f"[red]Error: {type(e).__name__}: {e}[/red]")
            continue
        finally:
            try:
                obs_provider.flush()     # drain traces per turn (no-op unless tracing is active)
            except Exception:  # noqa: BLE001 — telemetry flush must never break the REPL
                pass
        tid = result.thread_id           # pin the thread for the rest of the session
        console.print(f"[bold green]atom ›[/bold green] {result.final_text}\n")


@app.command()
def threads(config: str = typer.Option(None, "--config", "-c")) -> None:
    """List threads that have on-disk workspaces."""
    cfg = load_config(config)
    home = atom_home(cfg.home)
    users = home / "users"
    if not users.is_dir():
        console.print("[dim]No threads yet.[/dim]")
        return
    for user_dir in sorted(users.iterdir()):
        tdir = user_dir / "threads"
        if not tdir.is_dir():
            continue
        for thread_dir in sorted(tdir.iterdir()):
            console.print(f"{user_dir.name}/{thread_dir.name}  [dim]{thread_dir}[/dim]")


# --------------------------------------------------------------------------- workflows
workflow_app = typer.Typer(help="Run multi-agent workflows (Steps x Tasks).")
app.add_typer(workflow_app, name="workflow")

notes_app = typer.Typer(help="Manage a workflow's persistent Logseq vault.")
workflow_app.add_typer(notes_app, name="notes")


@notes_app.command("clear")
def workflow_notes_clear(
    name: str = typer.Argument(..., help="Workflow name whose vault to clear."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    config: str = typer.Option(None, "--config", "-c"),
) -> None:
    """Delete a workflow's persistent Logseq vault (a fresh one is provisioned on the next run)."""
    from atom.notes import clear_vault
    from atom.workflow.run_store import RunStore

    cfg = load_config(config)
    if RunStore(cfg.home).has_active_runs(name):
        console.print(
            f"[red]Refusing to clear notes for '{name}': a run is active. "
            f"Wait for it to finish or cancel it first.[/red]"
        )
        raise typer.Exit(1)
    if not yes:
        typer.confirm(f"Delete the persistent Logseq vault for workflow '{name}'?", abort=True)
    if clear_vault(cfg.home, name):
        console.print(f"[green]Cleared notes vault for '{name}'.[/green]")
    else:
        console.print(f"[dim]No notes vault existed for '{name}'.[/dim]")


@workflow_app.command("list")
def workflow_list(config: str = typer.Option(None, "--config", "-c")) -> None:
    """List available workflow definitions in $ATOM_HOME/workflows."""
    from atom.workflow.schema import list_workflows

    cfg = load_config(config)
    wfs = list_workflows(cfg.home)
    if not wfs:
        console.print("[dim]No workflows found. Add YAML files under $ATOM_HOME/workflows.[/dim]")
        return
    for w in wfs:
        console.print(f"[bold]{w.name}[/bold]  [dim]{w.description or ''}[/dim]")


@workflow_app.command("run")
def workflow_run(
    name: str = typer.Argument(..., help="Workflow name."),
    input: list[str] = typer.Option(None, "--input", "-i", help="key=value (repeatable)."),
    file: list[str] = typer.Option(None, "--file", "-f", help="name=path for a file input (repeatable)."),
    profile: str = typer.Option(None, "--profile", "-p"),
    config: str = typer.Option(None, "--config", "-c"),
) -> None:
    """Submit a workflow and poll it to completion."""
    import datetime

    from atom.workflow.engine import WorkflowEngine
    from atom.workflow.schema import load_workflow, MissingInputError
    from atom.workflow.uploads import (
        UploadTooLarge, UploadTypeNotAllowed, check_extension, check_size, virtual_upload_path,
    )
    from pathlib import Path

    _load_env()

    # Check for malformed --input tokens (missing =)
    if input:
        for token in input:
            if "=" not in token:
                console.print(f"[red]Error: --input must be KEY=VALUE, got: {token}[/red]")
                raise typer.Exit(1)

    cfg = load_config(config)

    try:
        wf = load_workflow(name, cfg.home)
    except FileNotFoundError:
        console.print(f"[red]Error: workflow '{name}' not found[/red]")
        raise typer.Exit(1)

    inputs = dict(kv.split("=", 1) for kv in (input or []) if "=" in kv)

    # Parse + stage --file NAME=PATH tokens (bytes read now; written after the run dir exists).
    file_input_names = {i.name for i in wf.inputs if i.type == "file"}
    staged: dict[str, tuple[str, bytes]] = {}
    for token in (file or []):
        if "=" not in token:
            console.print(f"[red]Error: --file must be NAME=PATH, got: {token}[/red]")
            raise typer.Exit(1)
        fname, fpath = token.split("=", 1)
        p = Path(fpath).expanduser()
        if fname not in file_input_names:
            console.print(f"[red]Error: '{fname}' is not a file input of workflow '{name}'[/red]")
            raise typer.Exit(1)
        if not p.is_file():
            console.print(f"[red]Error: file not found: {p}[/red]")
            raise typer.Exit(1)
        data = p.read_bytes()
        try:
            check_size(len(data), cfg.uploads.max_file_bytes)
            check_extension(p.name, cfg.uploads.allowed_extensions)
        except (UploadTooLarge, UploadTypeNotAllowed) as e:
            console.print(f"[red]Error: {e}[/red]")
            raise typer.Exit(1)
        staged[fname] = (p.name, data)
        inputs[fname] = virtual_upload_path(fname, p.name)

    engine = WorkflowEngine(cfg, profile=profile)
    run_id = uuid.uuid4().hex[:12]

    try:
        engine.create_run(wf, inputs, run_id, datetime.datetime.now().isoformat(timespec="seconds"))
    except MissingInputError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)

    for fname, (orig, data) in staged.items():
        engine.store.save_upload(run_id, fname, orig, data)

    engine.enqueue(run_id)

    with console.status(f"[bold]running workflow {name}…[/bold]"):
        manifest = asyncio.run(engine.await_run(run_id))
    for step in manifest.steps:
        marks = ", ".join(f"{t.id}:{t.status}" for t in step.tasks)
        console.print(f"  [bold]{step.title}[/bold] [dim]{step.status}[/dim] — {marks}")
    color = "green" if manifest.status == "complete" else "yellow" if manifest.status == "cancelled" else "red"
    console.print(f"\n[{color} bold]{manifest.status}[/{color} bold]  [dim]run: {run_id}[/dim]")
    ws = engine.store.workspace_dir(run_id)
    files = [p for p in ws.rglob("*") if p.is_file()] if ws.is_dir() else []
    if files:
        console.print("[bold]Artifacts:[/bold]")
        for p in files:
            console.print(f"  • {p.relative_to(ws)}")


@workflow_app.command("runs")
def workflow_runs(config: str = typer.Option(None, "--config", "-c")) -> None:
    """List workflow runs."""
    from atom.workflow.run_store import RunStore

    cfg = load_config(config)
    runs = RunStore(cfg.home).list()
    if not runs:
        console.print("[dim]No runs yet.[/dim]")
        return
    for m in runs:
        console.print(f"{m.run_id}  [bold]{m.workflow}[/bold]  [dim]{m.status}  {m.created_at}[/dim]")


def _parse_task_selector(sel: str) -> tuple[int, str]:
    """Parse a --task selector ``<step_index>:<task_id>`` (e.g. ``0:writer``)."""
    step_str, sep, task_id = sel.partition(":")
    if not sep or not task_id or not step_str.isdigit():
        raise ValueError(f"--task must be <step_index>:<task_id> (e.g. 0:writer), got {sel!r}")
    return int(step_str), task_id


def _export_module(cfg):
    """Select the exporter matching the configured provider (both expose export_run/export_task/resolve_run_ids).

    Returns the RESOLVED provider string ("langfuse" / "langsmith" / "none") so callers can tailor
    messaging. "none" (observability off) still routes to the LangSmith exporter — a run traced
    before observability was disabled may still have exportable LangSmith traces.
    """
    provider = cfg.observability.provider
    if provider is None:
        provider = "langsmith" if cfg.observability.enabled else "none"
    if provider == "langfuse":
        from atom.observability import langfuse_export as mod
        return "langfuse", mod
    from atom.observability import export as mod
    return provider, mod                                  # "langsmith" or collapsed "none"


def _export_one_task(export_mod, cfg, proj: str, run_id, latest, all_workflow, task: str) -> None:
    """Export a single task's trace (runs/<id>/exports/s<step>__<task>.json)."""
    if all_workflow:
        console.print("[red]--task cannot be combined with --all (pick one run via <run_id> or --latest)[/red]")
        raise typer.Exit(1)
    try:
        step_index, task_id = _parse_task_selector(task)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    try:
        run_ids = export_mod.resolve_run_ids(cfg.home, run_id=run_id, latest=latest)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    rid = run_ids[0]
    try:
        result = export_mod.export_task(cfg.home, rid, step_index, task_id, project=proj, cfg=cfg)
    except FileNotFoundError:
        console.print(f"[red]run '{rid}' not found[/red]")
        raise typer.Exit(1)
    except KeyError as e:                                  # unknown step/task — print the plain message
        console.print(f"[red]{e.args[0] if e.args else e}[/red]")
        raise typer.Exit(1)
    except (ValueError, RuntimeError) as e:               # not-terminal / no project / missing API key
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    except Exception as e:  # noqa: BLE001 — surface LangSmith API/network errors cleanly
        console.print(f"[red]export failed for {rid} task {step_index}:{task_id}: {type(e).__name__}: {e}[/red]")
        raise typer.Exit(1)
    if result.fetched_roots == 0:
        console.print(
            f"[red]no traces found for {rid} task {step_index}:{task_id} "
            f"— was observability enabled when it ran?[/red]"
        )
        raise typer.Exit(1)
    console.print(f"exported {rid} task {step_index}:{task_id} → {result.path}")


@workflow_app.command("export")
def workflow_export(
    run_id: str = typer.Argument(None, help="Run id to export."),
    latest: str = typer.Option(None, "--latest", help="Export the newest run of this workflow."),
    all_workflow: str = typer.Option(None, "--all", help="Export every run of this workflow."),
    task: str = typer.Option(None, "--task", help="Export one completed task: <step_index>:<task_id> (e.g. 0:writer)."),
    project: str = typer.Option(None, "--project", help="LangSmith project (LangSmith only; default: observability.project)."),
    config: str = typer.Option(None, "--config", "-c"),
) -> None:
    """Download this run's observability traces (LangSmith or LangFuse) for offline evaluation.

    The backend is chosen by observability.provider. Whole run -> runs/<run_id>/export.json.
    With --task, one completed task -> runs/<run_id>/exports/s<step>__<task>.json.
    """
    _load_env()
    cfg = load_config(config)
    provider, export_mod = _export_module(cfg)

    if provider == "langfuse":
        proj = None                                       # LangFuse selects by session, not a project
        if project is not None:
            console.print("[yellow]--project is ignored for LangFuse (it scopes by run session).[/yellow]")
        from atom.observability.provider import resolve_langfuse_keys
        public, secret, _ = resolve_langfuse_keys(cfg.observability)   # config.yaml keys OR env
        if not (public and secret):
            console.print("[red]set LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY (or observability.langfuse "
                          "keys in config) to export from LangFuse[/red]")
            raise typer.Exit(1)
    else:
        proj = project or cfg.observability.project
        if not proj:
            if provider == "none":
                console.print("[red]observability is disabled (provider=none / not enabled) — no traces to "
                              "export. Enable observability, or pass --project for a prior LangSmith run.[/red]")
            else:
                console.print("[red]no LangSmith project — set observability.project or pass --project[/red]")
            raise typer.Exit(1)

    if task is not None:
        _export_one_task(export_mod, cfg, proj, run_id, latest, all_workflow, task)
        return

    try:
        run_ids = export_mod.resolve_run_ids(
            cfg.home, run_id=run_id, latest=latest, all_workflow=all_workflow
        )
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    errors = False
    for rid in run_ids:
        try:
            result = export_mod.export_run(cfg.home, rid, project=proj, cfg=cfg)
        except FileNotFoundError:
            console.print(f"[red]run '{rid}' not found[/red]")
            errors = True
            continue
        except RuntimeError as e:                       # missing API key — abort the whole command
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
        except Exception as e:  # noqa: BLE001 — surface LangSmith API/network errors cleanly, don't traceback
            console.print(f"[red]export failed for {rid}: {type(e).__name__}: {e}[/red]")
            errors = True
            continue
        if result.fetched_roots == 0:
            console.print(
                f"[red]no traces found for {rid} — was observability enabled when it ran?[/red]"
            )
            errors = True
            continue
        if not result.complete:
            console.print(
                f"[yellow]partial: {rid} {result.fetched_roots}/{result.expected_roots} "
                f"task traces (async ingestion may still be catching up)[/yellow]"
            )
        console.print(f"exported {rid} → {result.path}")
    if errors:
        raise typer.Exit(1)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
    config: str = typer.Option(None, "--config", "-c"),
) -> None:
    """Launch the workflow API + UI server."""
    import uvicorn

    from atom.api.app import create_app

    _load_env()
    uvicorn.run(create_app(load_config(config)), host=host, port=port)


if __name__ == "__main__":
    app()
