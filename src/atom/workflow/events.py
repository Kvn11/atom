"""In-memory async pub/sub for live run events (single-process; see the streaming design doc).

One channel per (run_id, step, task). A channel keeps subscriber queues plus a bounded, coalesced
accumulator so a late subscriber (a page refresh) gets a catch-up snapshot then live deltas. Closed
channels are retained (LRU-bounded by ``retain_closed``) so a subscriber joining just after a task
completes still catches up before falling back to the durable on-disk chat. Transport-agnostic: it
moves opaque event dicts between one producer (the task) and N consumers (SSE handlers).
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import AsyncIterator, Optional


def channel_key(run_id: str, step: int, task_id: str) -> str:
    return f"{run_id}:s{step}:{task_id}"


_TERMINAL = object()  # sentinel pushed to subscriber queues on close


class _Channel:
    def __init__(self, max_chars: int, queue_max: int):
        self.max_chars = max_chars
        self.queue_max = queue_max
        self.subscribers: set[asyncio.Queue] = set()
        self.blocks: list[dict] = []          # coalesced accumulator (ordered atom events)
        self.closed = False
        self.error: Optional[str] = None

    def accumulate(self, event: dict) -> None:
        t = event.get("type")
        if t in ("text_delta", "thinking_delta") and self.blocks and self.blocks[-1].get("type") == t:
            merged = self.blocks[-1]["text"] + event.get("text", "")
            if len(merged) > self.max_chars:  # elide the oldest chars of this block to stay bounded
                merged = "…" + merged[-self.max_chars:]
            self.blocks[-1]["text"] = merged
        else:
            self.blocks.append(dict(event))


class RunEventBus:
    def __init__(self, *, max_chars: int = 20000, queue_max: int = 512,
                 retain_closed: int = 64, heartbeat_seconds: float = 15.0):
        self._chans: "OrderedDict[str, _Channel]" = OrderedDict()
        self.max_chars = max_chars
        self.queue_max = queue_max
        self.retain_closed = retain_closed
        self.heartbeat = heartbeat_seconds

    def _ensure(self, key: str) -> _Channel:
        ch = self._chans.get(key)
        if ch is None:
            ch = _Channel(self.max_chars, self.queue_max)
            self._chans[key] = ch
        self._chans.move_to_end(key)
        return ch

    async def publish(self, key: str, event: dict) -> None:
        ch = self._ensure(key)
        if ch.closed:
            return
        ch.accumulate(event)
        for q in list(ch.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # drop; the client re-syncs from a fresh snapshot on reconnect

    async def close(self, key: str, error: Optional[str] = None) -> None:
        ch = self._ensure(key)
        ch.closed = True
        ch.error = error
        for q in list(ch.subscribers):
            try:
                q.put_nowait(_TERMINAL)
            except asyncio.QueueFull:
                pass
        self._evict_closed()

    def _evict_closed(self) -> None:
        # Bound retained completed channels (oldest first, only those with no live subscribers).
        while sum(1 for c in self._chans.values() if c.closed) > self.retain_closed:
            for k, c in list(self._chans.items()):
                if c.closed and not c.subscribers:
                    self._chans.pop(k, None)
                    break
            else:
                break

    async def stream(self, key: str) -> AsyncIterator[dict]:
        ch = self._ensure(key)
        q: asyncio.Queue = asyncio.Queue(maxsize=self.queue_max)
        ch.subscribers.add(q)
        try:
            yield {"type": "snapshot", "blocks": [dict(b) for b in ch.blocks]}
            if ch.closed:
                yield {"type": "done", "error": ch.error}
                return
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=self.heartbeat)
                except asyncio.TimeoutError:
                    yield {"type": "ping"}
                    continue
                if ev is _TERMINAL:
                    yield {"type": "done", "error": ch.error}
                    return
                yield ev
        finally:
            ch.subscribers.discard(q)
            self._evict_closed()
