"""In-process publish/subscribe bus for system events."""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Optional


class EventType(Enum):
    TRAIN_STARTED = auto()
    TRAIN_ENDED = auto()
    VEHICLE_EVENT = auto()
    HEARTBEAT = auto()


@dataclass(frozen=True)
class EventMessage:
    """Lightweight container for events flowing through the system."""

    type: EventType
    payload: Any
    timestamp: float


class EventBus:
    """Thread-safe pub-sub queue with graceful shutdown semantics."""

    def __init__(self, maxsize: int = 1024) -> None:
        self._maxsize = maxsize
        self._subscribers: list["queue.Queue[EventMessage]"] = []
        self._lock = threading.RLock()
        self._stopped = threading.Event()

    def publish(self, event: EventMessage) -> None:
        if self._stopped.is_set():
            return
        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(event)
                except queue.Full:
                    continue

    def subscribe(self, maxsize: Optional[int] = None) -> "EventSubscription":
        queue_size = maxsize or self._maxsize
        subscriber_queue: "queue.Queue[EventMessage]" = queue.Queue(maxsize=queue_size)
        with self._lock:
            self._subscribers.append(subscriber_queue)
        return EventSubscription(self, subscriber_queue)

    def _unsubscribe(self, subscriber_queue: "queue.Queue[EventMessage]") -> None:
        with self._lock:
            if subscriber_queue in self._subscribers:
                self._subscribers.remove(subscriber_queue)

    def stop(self) -> None:
        self._stopped.set()
        with self._lock:
            self._subscribers.clear()


class EventSubscription:
    """Handle returned to components consuming events from the bus."""

    def __init__(self, bus: EventBus, queue_ref: "queue.Queue[EventMessage]") -> None:
        self._bus = bus
        self._queue = queue_ref
        self._stopped = False

    def get(self, timeout: float | None = 1.0) -> Optional[EventMessage]:
        if self._stopped:
            return None
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        if not self._stopped:
            self._bus._unsubscribe(self._queue)
            self._stopped = True


__all__ = ["EventBus", "EventMessage", "EventType", "EventSubscription"]
