"""DeepStream metadata processing and event generation."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, TYPE_CHECKING

from train_spotter.service.config import AppConfig, LaneSpec, TrainDetectionSettings
from train_spotter.storage import EventBus, EventMessage, EventType
from train_spotter.storage.db import TrainEvent, VehicleEvent

if TYPE_CHECKING:
    from train_spotter.ui.display import OverlayController

try:
    import pyds  # type: ignore
except ImportError:  # pragma: no cover - DeepStream runtime not available for tests
    pyds = None  # type: ignore

LOGGER = logging.getLogger(__name__)


@dataclass
class DetectedObject:
    """Lightweight representation of a DeepStream object meta."""

    track_id: int
    class_id: int
    class_label: str
    confidence: float
    bbox: List[float]
    lane_id: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


class TrainStateMachine:
    """Track train presence over time and emit events when state changes."""

    def __init__(self, settings: TrainDetectionSettings, event_bus: EventBus) -> None:
        self._settings = settings
        self._event_bus = event_bus
        self._active = False
        self._hit_count = 0
        self._miss_count = 0
        self._current_train_start: Optional[float] = None
        self._last_coverage: float = 0.0

    def update(self, coverage: float, timestamp: float) -> None:
        if coverage >= self._settings.zone.coverage_threshold:
            self._hit_count += 1
            self._miss_count = 0
            self._last_coverage = coverage
            if not self._active and self._hit_count >= self._settings.consecutive_hit_threshold:
                self._active = True
                self._current_train_start = timestamp
                self._emit(EventType.TRAIN_STARTED, timestamp)
        else:
            if self._active:
                self._miss_count += 1
                if self._miss_count >= self._settings.consecutive_miss_threshold:
                    self._active = False
                    self._emit(EventType.TRAIN_ENDED, timestamp)
                    self._reset_counters()
            else:
                self._reset_counters()

    def _reset_counters(self) -> None:
        self._hit_count = 0
        self._miss_count = 0
        self._last_coverage = 0.0
        self._current_train_start = None

    def _emit(self, event_type: EventType, timestamp: float) -> None:
        if event_type == EventType.TRAIN_STARTED:
            payload = {
                "train_id": f"train-{int(timestamp)}",
                "started_at": timestamp,
            }
        else:
            started_at = self._current_train_start or (timestamp - self._settings.zone.min_duration_seconds)
            duration = max(0.0, timestamp - started_at)
            payload = TrainEvent(
                train_id=f"train-{int(started_at)}",
                started_at=started_at,
                ended_at=timestamp,
                duration=duration,
                coverage_ratio=self._last_coverage,
            )
        message = EventMessage(event_type, payload, timestamp=timestamp)
        self._event_bus.publish(message)


class VehicleTrackerHooks:
    """Aggregate vehicle events per lane and broadcast through the event bus."""

    def __init__(self, lanes: List[LaneSpec], event_bus: EventBus) -> None:
        self._lanes = {lane.lane_id: lane for lane in lanes}
        self._event_bus = event_bus
        self._active_tracks: Dict[str, Dict[int, float]] = {
            lane_id: {} for lane_id in self._lanes
        }

    def handle_detection(self, obj: DetectedObject, timestamp: float) -> None:
        if obj.lane_id is None or obj.lane_id not in self._lanes:
            return
        lane_tracks = self._active_tracks.setdefault(obj.lane_id, {})
        lane_tracks[obj.track_id] = lane_tracks.get(obj.track_id, timestamp)

    def finalise_tracks(self, timestamp: float) -> None:
        for lane_id, tracks in list(self._active_tracks.items()):
            for track_id, entered_at in list(tracks.items()):
                duration = max(0.0, timestamp - entered_at)
                event = VehicleEvent(
                    track_id=str(track_id),
                    class_label="vehicle",
                    lane_id=lane_id,
                    entered_at=entered_at,
                    exited_at=timestamp,
                    duration=duration,
                )
                message = EventMessage(EventType.VEHICLE_EVENT, event, timestamp)
                self._event_bus.publish(message)
                del tracks[track_id]


class StreamAnalytics:
    """Glue layer converting DeepStream metadata into domain events."""

    def __init__(
        self,
        app_config: AppConfig,
        event_bus: EventBus,
        overlay_controller: "OverlayController" | None = None,
    ) -> None:
        if pyds is None:
            LOGGER.warning("pyds is not available; analytics will be inert")
        self._config = app_config
        self._event_bus = event_bus
        self._train_sm = TrainStateMachine(app_config.train_detection, event_bus)
        self._vehicle_hooks = VehicleTrackerHooks(
            app_config.vehicle_tracking.lanes,
            event_bus,
        )
        self._overlay = overlay_controller

    def process_frame(self, batch_meta) -> None:
        """Entry point for pad probe to process NvDsBatchMeta."""

        if pyds is None or batch_meta is None:
            return
        l_frame = batch_meta.frame_meta_list
        while l_frame is not None:
            frame_meta = self._cast_frame_meta(l_frame.data)
            timestamp = time.time()
            detections = self._extract_objects(frame_meta)
            coverage = self._estimate_train_coverage(detections)
            self._train_sm.update(coverage, timestamp)
            for det in detections:
                self._vehicle_hooks.handle_detection(det, timestamp)
            # At end of frame, finalise stale tracks
            self._vehicle_hooks.finalise_tracks(timestamp)
            if self._overlay is not None:
                self._overlay.apply_to_frame(frame_meta)
            l_frame = l_frame.next

    def _cast_frame_meta(self, data):
        try:
            return pyds.NvDsFrameMeta.cast(data)  # type: ignore[attr-defined]
        except AttributeError:
            return None

    def _extract_objects(self, frame_meta) -> List[DetectedObject]:
        detections: List[DetectedObject] = []
        if frame_meta is None:
            return detections
        l_obj = frame_meta.obj_meta_list
        while l_obj is not None:
            obj_meta = self._cast_object_meta(l_obj.data)
            if obj_meta is None:
                l_obj = l_obj.next
                continue
            det = DetectedObject(
                track_id=int(obj_meta.object_id),
                class_id=int(obj_meta.class_id),
                class_label=self._label_for_class(obj_meta.class_id),
                confidence=float(obj_meta.confidence),
                bbox=[
                    float(obj_meta.rect_params.left),
                    float(obj_meta.rect_params.top),
                    float(obj_meta.rect_params.width),
                    float(obj_meta.rect_params.height),
                ],
            )
            detections.append(det)
            l_obj = l_obj.next
        return detections

    def _cast_object_meta(self, data):
        try:
            return pyds.NvDsObjectMeta.cast(data)  # type: ignore[attr-defined]
        except AttributeError:
            return None

    def _label_for_class(self, class_id: int) -> str:
        labels = {
            0: "vehicle",
            1: "person",
            2: "bicycle",
            3: "road_sign",
        }
        return labels.get(class_id, f"class_{class_id}")

    def _estimate_train_coverage(self, detections: List[DetectedObject]) -> float:
        if not detections or not self._config.train_detection.zone:
            return 0.0
        # Placeholder ratio based on largest bbox area.
        max_area = 0.0
        for det in detections:
            if det.class_label not in {"vehicle", "train"}:
                continue
            _, _, width, height = det.bbox
            area = width * height
            if area > max_area:
                max_area = area
        frame_area = 1920 * 1080  # Fallback assumption; replace with runtime frame size.
        return min(1.0, max_area / frame_area)


def analytics_pad_probe(pad, info, analytics: StreamAnalytics):
    """Pad probe wrapper to integrate with GStreamer."""

    if pyds is None:
        return 0
    buffer = info.get_buffer()
    if not buffer:

        LOGGER.debug("Empty buffer encountered in analytics_pad_probe")
        return 0
    batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(buffer))  # type: ignore[attr-defined]
    analytics.process_frame(batch_meta)
    return 0


__all__ = ["StreamAnalytics", "analytics_pad_probe"]
