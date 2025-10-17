"""DeepStream metadata processing and event generation."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from train_spotter.service.config import AppConfig, TrainDetectionSettings
from train_spotter.service.roi import ROIConfig
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
    left: float
    top: float
    width: float
    height: float
    frame_width: float
    frame_height: float
    lane_id: Optional[str] = None
    timestamp: float = field(default_factory=time.time)

    @property
    def pixel_bounds(self) -> Tuple[float, float, float, float]:
        return (
            self.left,
            self.top,
            self.left + self.width,
            self.top + self.height,
        )

    @property
    def norm_bounds(self) -> Tuple[float, float, float, float]:
        fw = max(self.frame_width, 1.0)
        fh = max(self.frame_height, 1.0)
        x1 = max(0.0, self.left / fw)
        y1 = max(0.0, self.top / fh)
        x2 = min(1.0, (self.left + self.width) / fw)
        y2 = min(1.0, (self.top + self.height) / fh)
        return x1, y1, x2, y2

    @property
    def norm_center(self) -> Tuple[float, float]:
        x1, y1, x2, y2 = self.norm_bounds
        return (x1 + x2) / 2.0, (y1 + y2) / 2.0


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

    @dataclass
    class _TrackState:
        track_id: int
        lane_id: str
        class_label: str
        entered_at: float
        last_seen: float

    def __init__(
        self,
        lane_polygons: Dict[str, List[Tuple[float, float]]],
        event_bus: EventBus,
        stale_seconds: float = 1.5,
    ) -> None:
        self._lane_polygons = lane_polygons
        self._event_bus = event_bus
        self._tracks: Dict[int, VehicleTrackerHooks._TrackState] = {}
        self._stale_seconds = stale_seconds

    def handle_detection(self, obj: DetectedObject, timestamp: float) -> None:
        lane_id = obj.lane_id or self._resolve_lane(obj.norm_center)
        if lane_id is None:
            return
        obj.lane_id = lane_id
        current = self._tracks.get(obj.track_id)
        if current is None:
            self._tracks[obj.track_id] = VehicleTrackerHooks._TrackState(
                track_id=obj.track_id,
                lane_id=lane_id,
                class_label=obj.class_label,
                entered_at=timestamp,
                last_seen=timestamp,
            )
        else:
            current.last_seen = timestamp
            current.lane_id = lane_id
            current.class_label = obj.class_label

    def finalise_tracks(self, timestamp: float) -> None:
        to_remove: List[int] = []
        for track_id, state in self._tracks.items():
            if timestamp - state.last_seen >= self._stale_seconds:
                duration = max(0.0, state.last_seen - state.entered_at)
                event = VehicleEvent(
                    track_id=str(track_id),
                    class_label=state.class_label,
                    lane_id=state.lane_id,
                    entered_at=state.entered_at,
                    exited_at=state.last_seen,
                    duration=duration,
                )
                message = EventMessage(EventType.VEHICLE_EVENT, event, state.last_seen)
                self._event_bus.publish(message)
                to_remove.append(track_id)
        for track_id in to_remove:
            self._tracks.pop(track_id, None)

    def _resolve_lane(self, point: Tuple[float, float]) -> Optional[str]:
        for lane_id, polygon in self._lane_polygons.items():
            if point_in_polygon(point, polygon):
                return lane_id
        return None


class StreamAnalytics:
    """Glue layer converting DeepStream metadata into domain events."""

    TRAIN_CLASS_LABELS = {"train", "vehicle", "car", "truck", "bus"}
    VEHICLE_CLASS_LABELS = {"vehicle", "car", "truck", "bus"}

    def __init__(
        self,
        app_config: AppConfig,
        event_bus: EventBus,
        overlay_controller: "OverlayController" | None = None,
        roi_config: ROIConfig | None = None,
    ) -> None:
        if pyds is None:
            LOGGER.warning("pyds is not available; analytics will be inert")
        self._config = app_config
        self._event_bus = event_bus
        self._overlay = overlay_controller
        self._roi_config = roi_config
        self._train_polygon = self._determine_train_polygon(roi_config)
        self._train_polygon_area = polygon_area(self._train_polygon) if self._train_polygon else 0.0
        self._train_bounds = polygon_bounds(self._train_polygon) if self._train_polygon else (0.0, 0.0, 0.0, 0.0)
        lane_polygons = self._prepare_lane_polygons(roi_config)
        self._vehicle_hooks = VehicleTrackerHooks(lane_polygons, event_bus)
        self._train_sm = TrainStateMachine(app_config.train_detection, event_bus)

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
                if det.class_label.lower() in self.VEHICLE_CLASS_LABELS:
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
        frame_width = float(getattr(frame_meta, "source_frame_width", 1920))
        frame_height = float(getattr(frame_meta, "source_frame_height", 1080))
        l_obj = frame_meta.obj_meta_list
        while l_obj is not None:
            obj_meta = self._cast_object_meta(l_obj.data)
            if obj_meta is None:
                l_obj = l_obj.next
                continue
            label = self._label_for_class(obj_meta.class_id, getattr(obj_meta, "obj_label", None))
            det = DetectedObject(
                track_id=int(obj_meta.object_id),
                class_id=int(obj_meta.class_id),
                class_label=label.lower(),
                confidence=float(obj_meta.confidence),
                left=float(obj_meta.rect_params.left),
                top=float(obj_meta.rect_params.top),
                width=float(obj_meta.rect_params.width),
                height=float(obj_meta.rect_params.height),
                frame_width=frame_width,
                frame_height=frame_height,
            )
            detections.append(det)
            l_obj = l_obj.next
        return detections

    def _cast_object_meta(self, data):
        try:
            return pyds.NvDsObjectMeta.cast(data)  # type: ignore[attr-defined]
        except AttributeError:
            return None

    def _label_for_class(self, class_id: int, meta_label: Optional[str]) -> str:
        if meta_label:
            return meta_label
        labels = {
            0: "vehicle",
            1: "person",
            2: "bicycle",
            3: "road_sign",
        }
        return labels.get(class_id, f"class_{class_id}")

    def _estimate_train_coverage(self, detections: List[DetectedObject]) -> float:
        if not detections or not self._train_polygon or self._train_polygon_area <= 0:
            return 0.0
        total_overlap = 0.0
        for det in detections:
            if det.class_label.lower() not in self.TRAIN_CLASS_LABELS:
                continue
            overlap = intersection_area(det.norm_bounds, self._train_bounds)
            total_overlap += overlap
        return min(1.0, total_overlap / self._train_polygon_area)

    def _determine_train_polygon(self, roi_config: ROIConfig | None) -> List[Tuple[float, float]]:
        if roi_config is not None:
            return [tuple(point) for point in roi_config.train_roi.points]
        polygon = self._config.train_detection.zone.polygon.points
        return [tuple(point) for point in polygon]

    def _prepare_lane_polygons(self, roi_config: ROIConfig | None) -> Dict[str, List[Tuple[float, float]]]:
        lane_polygons: Dict[str, List[Tuple[float, float]]] = {}
        if roi_config:
            for lane in roi_config.road_lanes:
                lane_polygons[lane.lane_id] = [tuple(point) for point in lane.polygon.points]
        else:
            for lane in self._config.vehicle_tracking.lanes:
                lane_polygons[lane.lane_id] = [tuple(point) for point in lane.polygon.points]
        return lane_polygons


def polygon_area(points: List[Tuple[float, float]]) -> float:
    if len(points) < 3:
        return 0.0
    area = 0.0
    for i in range(len(points)):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % len(points)]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def polygon_bounds(points: List[Tuple[float, float]]) -> Tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def intersection_area(
    box_a: Tuple[float, float, float, float],
    box_b: Tuple[float, float, float, float],
) -> float:
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b
    x_left = max(ax1, bx1)
    y_top = max(ay1, by1)
    x_right = min(ax2, bx2)
    y_bottom = min(ay2, by2)
    if x_right <= x_left or y_bottom <= y_top:
        return 0.0
    return max(0.0, (x_right - x_left) * (y_bottom - y_top))


def point_in_polygon(point: Tuple[float, float], polygon: List[Tuple[float, float]]) -> bool:
    x, y = point
    inside = False
    n = len(polygon)
    if n < 3:
        return False
    p1x, p1y = polygon[0]
    for i in range(n + 1):
        p2x, p2y = polygon[i % n]
        if min(p1y, p2y) < y <= max(p1y, p2y) and x <= max(p1x, p2x):
            if p1y != p2y:
                xints = (y - p1y) * (p2x - p1x) / (p2y - p1y) + p1x
            else:
                xints = p1x
            if p1x == p2x or x <= xints:
                inside = not inside
        p1x, p1y = p2x, p2y
    return inside


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
