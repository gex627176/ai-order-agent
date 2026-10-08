from __future__ import annotations

import asyncio
from dataclasses import dataclass
import threading
from typing import Any


@dataclass(frozen=True)
class RecognitionTaskEvent:
    sequence: int
    task: dict[str, Any]


class RecognitionTaskSubscription:
    def __init__(
        self,
        broker: "RecognitionTaskEventBroker",
        task_id: str,
        loop: asyncio.AbstractEventLoop,
        events: asyncio.Queue[RecognitionTaskEvent],
    ) -> None:
        self._broker = broker
        self.task_id = task_id
        self._loop = loop
        self._events = events
        self._closed = False

    async def get(self, timeout: float) -> RecognitionTaskEvent:
        return await asyncio.wait_for(self._events.get(), timeout)

    def offer(self, event: RecognitionTaskEvent) -> None:
        self._loop.call_soon_threadsafe(self._offer_on_loop, event)

    def _offer_on_loop(self, event: RecognitionTaskEvent) -> None:
        if self._closed:
            return
        if self._events.full():
            self._events.get_nowait()
        self._events.put_nowait(event)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._broker.unsubscribe(self)


class RecognitionTaskEventBroker:
    """Thread-safe, bounded task notifications for this single process."""

    def __init__(self, queue_size: int = 1) -> None:
        if queue_size < 1:
            raise ValueError("订阅队列容量必须大于 0")
        self._queue_size = queue_size
        self._lock = threading.Lock()
        self._sequence = 0
        self._subscribers: dict[
            str, set[RecognitionTaskSubscription]
        ] = {}

    def subscribe(self, task_id: str) -> RecognitionTaskSubscription:
        loop = asyncio.get_running_loop()
        events: asyncio.Queue[RecognitionTaskEvent] = asyncio.Queue(
            maxsize=self._queue_size
        )
        subscription = RecognitionTaskSubscription(
            self, task_id, loop, events
        )
        with self._lock:
            self._subscribers.setdefault(task_id, set()).add(subscription)
        return subscription

    def unsubscribe(self, subscription: RecognitionTaskSubscription) -> None:
        with self._lock:
            subscriptions = self._subscribers.get(subscription.task_id)
            if subscriptions is None:
                return
            subscriptions.discard(subscription)
            if not subscriptions:
                self._subscribers.pop(subscription.task_id, None)

    def publish(self, task: dict[str, Any]) -> None:
        task_id = str(task["id"])
        with self._lock:
            self._sequence += 1
            event = RecognitionTaskEvent(self._sequence, dict(task))
            subscriptions = tuple(self._subscribers.get(task_id, ()))
            for subscription in subscriptions:
                subscription.offer(event)

    def subscriber_count(self, task_id: str | None = None) -> int:
        with self._lock:
            if task_id is not None:
                return len(self._subscribers.get(task_id, ()))
            return sum(len(items) for items in self._subscribers.values())
