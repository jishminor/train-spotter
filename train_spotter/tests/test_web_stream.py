"""Tests for WebRTC signaling helpers."""

from __future__ import annotations

from train_spotter.web.webrtc import WebRTCManager, WebRTCSession


def test_webrtc_session_roundtrip_queue() -> None:
    session = WebRTCSession("test-session")
    offer = {"type": "offer", "sdp": "fake"}
    session.enqueue_from_browser(offer)
    drained = list(session.drain_browser_messages())
    assert drained == [offer]

    payload = {"type": "answer", "sdp": "ok"}
    session.send_to_browser(payload)
    message = session.next_outgoing(timeout=0.1)
    assert message == payload

    session.close("done")
    assert session.is_closed()
    closed_message = session.next_outgoing(timeout=0.1)
    assert closed_message["type"] == "session-closed"


def test_manager_invokes_session_handler() -> None:
    manager = WebRTCManager()
    invoked: list[str] = []

    manager.register_session_handler(lambda session: invoked.append(session.id))

    session = manager.create_session()
    assert invoked == [session.id]

    session.close("cleanup")
    manager.remove_session(session.id)
