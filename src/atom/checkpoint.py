"""Checkpointer factory: durable AsyncSqlite for real runs, in-memory for tests.

The checkpointer persists per-thread graph state (keyed by ``thread_id``) and is what makes
``ask_clarification`` resumable across turns.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Literal

from langgraph.checkpoint.base import BaseCheckpointSaver


@asynccontextmanager
async def open_checkpointer(
    backend: Literal["sqlite", "memory"] = "sqlite",
    db_path: str | Path | None = None,
) -> AsyncIterator[BaseCheckpointSaver]:
    """Yield a checkpointer. Use as ``async with open_checkpointer(...) as cp:``."""
    if backend == "memory":
        from langgraph.checkpoint.memory import InMemorySaver

        yield InMemorySaver()
        return

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    if db_path is None:
        raise ValueError("sqlite checkpointer requires db_path")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        yield saver
