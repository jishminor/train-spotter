"""Event bus basic behaviour tests."""

from __future__ import annotations

import time

from train_spotter.storage import EventBus, EventMessage, EventType


def test_event_bus_publish_and_consume() -> None:
    bus = EventBus()
    subscription = bus.subscribe()

    now = time.time()
    event = EventMessage(EventType.HEARTBEAT, payload=None, timestamp=now)
    bus.publish(event)

    received = subscription.get(timeout=0.1)
    assert received == event

    subscription.close()
    bus.stop()
