"""Helpers for in-process WebRTC signaling."""

from __future__ import annotations

import json
import logging
import queue
import threading
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, Optional

LOGGER = logging.getLogger(__name__)


class WebRTCSession:
    """Bidirectional message queues and lifecycle helpers for a single peer."""

    def __init__(self, session_id: Optional[str] = None) -> None:
        self.id = session_id or uuid.uuid4().hex
        self._from_browser: "queue.Queue[dict]" = queue.Queue()
        self._to_browser: "queue.Queue[dict]" = queue.Queue()
        self._closed = threading.Event()
        self._close_callbacks: list[Callable[[WebRTCSession], None]] = []
        self.close_reason: Optional[str] = None

    def enqueue_from_browser(self, payload: dict) -> None:
        if self._closed.is_set():
            LOGGER.debug("Dropping message for closed session %s: %s", self.id, payload)
            return
        self._from_browser.put(payload)

    def drain_browser_messages(self) -> Iterable[dict]:
        while True:
            try:
                message = self._from_browser.get_nowait()
            except queue.Empty:
                break
            else:
                yield message

    def next_outgoing(self, timeout: float = 0.5) -> Optional[dict]:
        """Block for up to timeout waiting for a message to send."""
        if self._closed.is_set() and self._to_browser.empty():
            return None
        try:
            return self._to_browser.get(timeout=timeout)
        except queue.Empty:
            return None

    def send_to_browser(self, payload: dict) -> None:
        if self._closed.is_set():
            LOGGER.debug("Attempt to send on closed session %s ignored", self.id)
            return
        self._to_browser.put(payload)

    def add_close_callback(self, callback: Callable[["WebRTCSession"], None]) -> None:
        self._close_callbacks.append(callback)

    def close(self, reason: str | None = None) -> None:
        if self._closed.is_set():
            return
        self.close_reason = reason
        self._closed.set()
        while not self._to_browser.empty():
            try:
                self._to_browser.get_nowait()
            except queue.Empty:
                break
        for callback in list(self._close_callbacks):
            try:
                callback(self)
            except Exception:  # pragma: no cover - defensive
                LOGGER.exception("WebRTC session close callback failed")
        self._to_browser.put({"type": "session-closed", "reason": reason})

    def is_closed(self) -> bool:
        return self._closed.is_set()


@dataclass
class WebRTCManager:
    """Tracks active sessions and notifies the pipeline when new peers join."""

    _sessions: Dict[str, WebRTCSession] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _session_handler: Optional[Callable[[WebRTCSession], None]] = None

    def register_session_handler(self, handler: Callable[[WebRTCSession], None]) -> None:
        """Hook invoked as soon as a new peer session is created."""
        self._session_handler = handler

    def create_session(self) -> WebRTCSession:
        session = WebRTCSession()
        with self._lock:
            self._sessions[session.id] = session
        LOGGER.debug("Created WebRTC session %s", session.id)
        if self._session_handler:
            try:
                self._session_handler(session)
            except Exception:
                LOGGER.exception("WebRTC session handler failed")
                session.close("handler-error")
        else:
            LOGGER.warning("No WebRTC session handler registered; closing session %s", session.id)
            session.close("pipeline-unavailable")
        return session

    def remove_session(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)
        LOGGER.debug("Removed WebRTC session %s", session_id)

    def close_all(self, reason: str = "shutdown") -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close(reason)


def parse_browser_payload(raw: str) -> Optional[dict]:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        LOGGER.warning("Ignoring malformed signaling payload: %s", raw)
        return None


__all__ = ["WebRTCManager", "WebRTCSession", "parse_browser_payload"]
