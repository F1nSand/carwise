"""Thread-safe, process-local wakeups for durable run event streams."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class _Subscriber:
    loop: asyncio.AbstractEventLoop
    event: asyncio.Event


class RunEventNotifier:
    """Wake SSE consumers after writes without making notifications authoritative.

    Run events remain durable in the configured store. Notifications only avoid
    needless polling; subscribers always read from the store after waking, and
    callers should retain a low-frequency timeout as a cross-process fallback.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[str, dict[int, _Subscriber]] = {}

    def subscribe(self, run_id: str) -> asyncio.Event:
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        subscriber = _Subscriber(loop=loop, event=event)
        with self._lock:
            self._subscribers.setdefault(str(run_id), {})[id(event)] = subscriber
        return event

    def unsubscribe(self, run_id: str, event: asyncio.Event) -> None:
        key = str(run_id)
        with self._lock:
            subscribers = self._subscribers.get(key)
            if subscribers is None:
                return
            subscribers.pop(id(event), None)
            if not subscribers:
                self._subscribers.pop(key, None)

    def notify(self, run_id: str) -> None:
        key = str(run_id)
        with self._lock:
            subscribers = list(self._subscribers.get(key, {}).items())
        stale: list[int] = []
        for token, subscriber in subscribers:
            try:
                subscriber.loop.call_soon_threadsafe(subscriber.event.set)
            except RuntimeError:
                stale.append(token)
        if stale:
            with self._lock:
                current = self._subscribers.get(key)
                if current is not None:
                    for token in stale:
                        current.pop(token, None)
                    if not current:
                        self._subscribers.pop(key, None)
