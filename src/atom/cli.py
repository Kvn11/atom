"""atom CLI (``atom run|chat|threads``). A thin wrapper over :func:`atom.runtime.run_agent`."""

from __future__ import annotations

import asyncio
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


if __name__ == "__main__":
    app()
