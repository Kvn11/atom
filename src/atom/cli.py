"""atom CLI (``atom run|chat|threads``). A thin wrapper over :func:`atom.runtime.run_agent`."""

from __future__ import annotations

import asyncio
import uuid
import warnings
from pathlib import Path

import typer
from langchain_core.messages import AIMessage, ToolMessage
from rich.console import Console

from atom.config import load_config
from atom.runtime import RunResult, run_agent
from atom.sandbox.paths import atom_home

# Cosmetic: silence a benign pydantic<->langgraph serializer warning about the typed context.
warnings.filterwarnings("ignore", message="Pydantic serializer warnings")

app = typer.Typer(add_completion=False, help="atom — a DeerFlow-style agentic harness.")
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
    with console.status("[bold]thinking…[/bold]"):
        result = asyncio.run(run_agent(
            task, config_path=config, profile=profile, override_model=model,
            override_thinking=thinking, override_system_prompt=system_prompt,
            workspace=workspace, thread_id=thread, user_id=user,
        ))
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
        with console.status("[bold]thinking…[/bold]"):
            result = asyncio.run(run_agent(
                task, config_path=config, profile=profile, override_model=model,
                override_thinking=thinking, override_system_prompt=system_prompt,
                # Keep the ORIGINAL workspace across turns: pinning the thread already reuses a
                # 'new' per-thread dir, and an 'existing' bind MUST persist (don't reset to 'new').
                workspace=workspace, thread_id=tid, user_id=user,
            ))
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
    profile: str = typer.Option(None, "--profile", "-p"),
    config: str = typer.Option(None, "--config", "-c"),
) -> None:
    """Submit a workflow and poll it to completion."""
    import datetime

    from atom.workflow.engine import WorkflowEngine
    from atom.workflow.schema import load_workflow

    _load_env()
    cfg = load_config(config)
    wf = load_workflow(name, cfg.home)
    inputs = dict(kv.split("=", 1) for kv in (input or []) if "=" in kv)
    engine = WorkflowEngine(cfg, profile=profile)
    run_id = uuid.uuid4().hex[:12]
    engine.create_run(wf, inputs, run_id, datetime.datetime.now().isoformat(timespec="seconds"))
    with console.status(f"[bold]running workflow {name}…[/bold]"):
        manifest = asyncio.run(engine.execute(run_id))
    for step in manifest.steps:
        marks = ", ".join(f"{t.id}:{t.status}" for t in step.tasks)
        console.print(f"  [bold]{step.title}[/bold] [dim]{step.status}[/dim] — {marks}")
    color = "green" if manifest.status == "complete" else "red"
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
