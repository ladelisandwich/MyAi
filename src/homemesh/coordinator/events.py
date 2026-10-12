"""In-process publish/subscribe used to push live updates to browsers (SSE) and tests.

Events are small dicts ``{"type": "node.updated", "data": {...}, "ts": ...}``. Each
subscriber has a bounded queue; a slow subscriber loses old events and receives a
``resync`` event telling the UI to reload state, so one stuck browser tab can never block
the coordinator or grow memory without bound.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

from homemesh.common.timeutil import now_iso


class Subscription:
    def __init__(self, bus: EventBus, user_id: str | None, maxsize: int) -> None:
        self.bus = bus
        self.user_id = user_id
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=maxsize)
        self.overflowed = False

    def offer(self, event: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            # Drop everything queued and ask the client to resynchronise.
            while not self.queue.empty():
                self.queue.get_nowait()
            self.overflowed = True
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait({"type": "resync", "data": {}, "ts": now_iso()})

    async def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            yield await self.queue.get()

    def close(self) -> None:
        self.bus.unsubscribe(self)


class EventBus:
    def __init__(self, queue_size: int = 500) -> None:
        self._subs: set[Subscription] = set()
        self.queue_size = queue_size

    def subscribe(self, user_id: str | None = None) -> Subscription:
        sub = Subscription(self, user_id, self.queue_size)
        self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        self._subs.discard(sub)

    def publish(self, event_type: str, data: dict[str, Any] | None = None, *, user_id: str | None = None) -> None:
        """Publish to all subscribers. ``user_id`` restricts delivery to that user's sessions
        (personal data such as conversations/memories); ``None`` means system-wide events."""
        event = {"type": event_type, "data": data or {}, "ts": now_iso()}
        for sub in list(self._subs):
            if user_id is not None and sub.user_id is not None and sub.user_id != user_id:
                continue
            sub.offer(event)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)
