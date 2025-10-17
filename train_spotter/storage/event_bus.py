"""Simple in-process event bus for coordinating between pipeline and storage/UI."""

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
        self._queue: "queue.Queue[EventMessage]" = queue.Queue(maxsize=maxsize)
        self._stopped = threading.Event()

    def publish(self, event: EventMessage) -> None:
        if self._stopped.is_set():
            return
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            # Dropping events is acceptable; ensure the queue keeps moving.
            self._queue.get_nowait()
            self._queue.put_nowait(event)

    def consume(self, timeout: float | None = 1.0) -> Optional[EventMessage]:
        if self._stopped.is_set():
            return None
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self) -> None:
        self._stopped.set()
        # Drain queue to unblock consumers.
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break


__all__ = ["EventBus", "EventMessage", "EventType"]
