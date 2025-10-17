"""Application configuration models and loaders."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

Coordinate = Tuple[float, float]


class PolygonSpec(BaseModel):
    """Polygon definition expressed as a sequence of coordinates."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(..., description="Human readable name for this polygon")
    points: List[Coordinate]

    @field_validator("points")
    @classmethod
    def _validate_points(cls, value: List[Coordinate]) -> List[Coordinate]:
        if len(value) < 3:
            raise ValueError("polygon requires at least three points")
        return value


class LaneSpec(BaseModel):
    """Lane configuration for vehicle tracking."""

    model_config = ConfigDict(frozen=True)

    lane_id: str = Field(..., description="Unique identifier for the lane")
    polygon: PolygonSpec
    direction_hint: Optional[str] = Field(
        default=None, description="Optional text hint (e.g. northbound)"
    )


class TrainZoneSpec(BaseModel):
    """Detection zone focusing on the railway."""

    model_config = ConfigDict(frozen=True)

    polygon: PolygonSpec
    coverage_threshold: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description="Fraction of ROI that must be occupied to count as a train",
    )
    min_duration_seconds: float = Field(
        default=2.0,
        ge=0.0,
        description="Minimum sustained detection before marking train present",
    )
    clear_down_seconds: float = Field(
        default=1.0,
        ge=0.0,
        description="Grace period after last detection before considering track clear",
    )


class TrainDetectionSettings(BaseModel):
    """Parameters controlling train detection heuristics."""

    model_config = ConfigDict(frozen=True)

    zone: TrainZoneSpec
    min_bbox_area: int = Field(
        default=10_000,
        ge=0,
        description="Ignore detections smaller than this pixel area",
    )
    consecutive_hit_threshold: int = Field(
        default=5,
        ge=1,
        description="Frames required to promote candidate to active train",
    )
    consecutive_miss_threshold: int = Field(
        default=10,
        ge=1,
        description="Frames required to mark train as cleared",
    )


class VehicleTrackingSettings(BaseModel):
    """Parameters for DeepStream tracker and per-lane logic."""

    model_config = ConfigDict(frozen=True)

    lanes: List[LaneSpec] = Field(default_factory=list)
    tracker_config_path: Optional[str] = Field(
        default=None, description="Path to DeepStream tracker config"
    )
    infer_primary_config_path: str = Field(
        ...,
        description="Path to the primary detector config (e.g. TrafficCamNet)",
    )
    infer_secondary_config_path: Optional[str] = Field(
        default=None,
        description="Optional secondary classifier config for finer labels",
    )


class DisplaySettings(BaseModel):
    """Control the on-device visual output."""

    model_config = ConfigDict(frozen=True)

    enable_overlay: bool = True
    sink_type: str = Field(
        default="nveglglessink",
        description="DeepStream sink element for HDMI output",
    )
    show_debug_info: bool = False


class WebSettings(BaseModel):
    """Lightweight dashboard configuration."""

    model_config = ConfigDict(frozen=True)

    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8080)
    enable_auth: bool = False
    mjpeg_framerate: int = Field(default=10, ge=1, le=60)
    max_clients: int = Field(default=5, ge=1)


class StorageSettings(BaseModel):
    """Persistence configuration."""

    model_config = ConfigDict(frozen=True)

    database_path: str = Field(default="/data/train-spotter/events.db")
    ensure_fsync: bool = Field(
        default=True, description="Force fsync after critical writes"
    )


class DeepStreamPaths(BaseModel):
    """Config paths for DeepStream graph assembly."""

    model_config = ConfigDict(frozen=True)

    base_config_dir: str = Field(
        default="/opt/nvidia/deepstream/deepstream/samples/configs/"
    )
    pipeline_config: Optional[str] = Field(
        default=None,
        description="Optional path to a fully-specified DeepStream pipeline config",
    )


class AppConfig(BaseModel):
    """Top-level configuration for the application."""

    model_config = ConfigDict(frozen=True)

    camera_id: str = Field(default="camera0")
    camera_source: str = Field(
        default="nvarguscamerasrc sensor-id=0", description="GStreamer source"
    )
    roi_config_path: str = Field(
        default="train_spotter/data/roi_config.json",
        description="Path to ROI configuration JSON",
    )
    train_detection: TrainDetectionSettings
    vehicle_tracking: VehicleTrackingSettings
    display: DisplaySettings = Field(default_factory=DisplaySettings)
    web: WebSettings = Field(default_factory=WebSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    deepstream: DeepStreamPaths = Field(default_factory=DeepStreamPaths)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AppConfig":
        try:
            return cls(**data)
        except ValidationError as exc:  # pragma: no cover - Pydantic already tested
            raise ValueError(f"Invalid configuration: {exc}") from exc

    @classmethod
    def from_file(cls, path: str | Path) -> "AppConfig":
        cfg_path = Path(path)
        if not cfg_path.exists():
            raise FileNotFoundError(f"Configuration file not found: {cfg_path}")
        suffix = cfg_path.suffix.lower()
        if suffix not in {".json"}:
            raise ValueError("Unsupported configuration file format; use JSON")
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        return cls.from_dict(data)

    @classmethod
    def default(cls) -> "AppConfig":
        """Return a conservative default configuration."""

        default_data: Dict[str, Any] = {
            "train_detection": {
                "zone": {
                    "polygon": {
                        "name": "main_track",
                        "points": [
                            [0.1, 0.6],
                            [0.9, 0.6],
                            [0.9, 0.8],
                            [0.1, 0.8],
                        ],
                    }
                }
            },
            "vehicle_tracking": {
                "lanes": [],
                "infer_primary_config_path": "configs/trafficcamnet_primary.txt",
            },
        }
        return cls.from_dict(default_data)


def resolve_config(path: Optional[str]) -> AppConfig:
    """Load configuration from disk, falling back to defaults when missing."""

    if path:
        return AppConfig.from_file(path)
    return AppConfig.default()


__all__ = [
    "AppConfig",
    "PolygonSpec",
    "LaneSpec",
    "TrainZoneSpec",
    "TrainDetectionSettings",
    "VehicleTrackingSettings",
    "DisplaySettings",
    "WebSettings",
    "StorageSettings",
    "DeepStreamPaths",
    "resolve_config",
]
