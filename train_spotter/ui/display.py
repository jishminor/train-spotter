"""On-device overlay helpers for DeepStream OSD output."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from train_spotter.storage import EventBus, EventMessage, EventType

try:
    import pyds  # type: ignore
except ImportError:  # pragma: no cover - DeepStream runtime missing in tests
    pyds = None  # type: ignore

LOGGER = logging.getLogger(__name__)


@dataclass
class OverlayState:
    """State exposed to the on-screen display."""

    train_active: bool = False
    train_started_at: Optional[float] = None
    last_train_duration: float = 0.0
    vehicle_counts: Dict[str, int] = field(default_factory=dict)

    @property
    def train_elapsed(self) -> float:
        if self.train_active and self.train_started_at is not None:
            return max(0.0, time.time() - self.train_started_at)
        return self.last_train_duration


class OverlayController:
    """Consumes events and projects them onto the DeepStream OSD."""

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._subscription = event_bus.subscribe()
        self._state = OverlayState()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @property
    def state(self) -> OverlayState:
        return self._state

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=2.0)
        self._subscription.close()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            message = self._subscription.get(timeout=0.25)
            if not message:
                continue
            self._handle_event(message)

    def _handle_event(self, message: EventMessage) -> None:
        if message.type == EventType.TRAIN_STARTED:
            self._state.train_active = True
            payload = message.payload
            started_at = getattr(payload, "started_at", None) or message.timestamp
            self._state.train_started_at = started_at
        elif message.type == EventType.TRAIN_ENDED:
            self._state.train_active = False
            if hasattr(message.payload, "duration"):
                self._state.last_train_duration = float(message.payload.duration)
            self._state.train_started_at = None
        elif message.type == EventType.VEHICLE_EVENT and hasattr(message.payload, "lane_id"):
            lane_id = message.payload.lane_id
            self._state.vehicle_counts[lane_id] = self._state.vehicle_counts.get(lane_id, 0) + 1

    def apply_to_frame(self, frame_meta, batch_meta=None) -> None:
        if pyds is None or frame_meta is None:
            return
        batch_meta_obj = batch_meta if batch_meta is not None else getattr(frame_meta, "batch_meta", None)
        if batch_meta_obj is None:
            LOGGER.debug("Batch metadata unavailable; skipping overlay draw")
            return
        display_meta = pyds.nvds_acquire_display_meta_from_pool(batch_meta_obj)  # type: ignore[attr-defined]
        if display_meta is None:
            LOGGER.debug("Failed to acquire display meta")
            return

        train_status = "TRAIN PASSING" if self._state.train_active else "TRACK CLEAR"
        duration = self._state.train_elapsed
        text_lines = [
            f"Status: {train_status}",
            f"Train duration: {duration:.1f}s",
        ]
        if self._state.vehicle_counts:
            lane_lines = ", ".join(f"{lane}:{count}" for lane, count in self._state.vehicle_counts.items())
            text_lines.append(f"Vehicles: {lane_lines}")

        display_meta.num_labels = 1
        display_meta.text_params[0].display_text = "\n".join(text_lines)
        display_meta.text_params[0].x_offset = 20
        display_meta.text_params[0].y_offset = 20
        display_meta.text_params[0].font_params.font_name = "Serif"
        display_meta.text_params[0].font_params.font_size = 18
        display_meta.text_params[0].font_params.font_color.set(1, 1, 1, 1)
        display_meta.text_params[0].set_bg_clr = 1
        display_meta.text_params[0].text_bg_clr.set(0.1, 0.1, 0.1, 0.8)
        pyds.nvds_add_display_meta_to_frame(frame_meta, display_meta)  # type: ignore[attr-defined]


__all__ = ["OverlayController", "OverlayState"]
