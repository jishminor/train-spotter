"""Utilities for loading and saving region of interest configuration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator

Coordinate = Tuple[float, float]


class ZonePolygon(BaseModel):
    """A named polygon zone expressed in relative coordinates (0-1 range)."""

    model_config = ConfigDict(frozen=True)

    label: str
    points: List[Coordinate]

    @field_validator("points")
    @classmethod
    def _validate_points(cls, value: List[Coordinate]) -> List[Coordinate]:
        if len(value) < 3:
            raise ValueError("polygon requires at least three vertices")
        for x, y in value:
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                raise ValueError("coordinate values must be in normalised 0-1 range")
        return value


class RoadLane(BaseModel):
    """Definition for a roadway lane used in vehicle counting."""

    model_config = ConfigDict(frozen=True)

    lane_id: str
    polygon: ZonePolygon
    exit_line: Optional[List[Coordinate]] = Field(
        default=None,
        description="Optional 2-point line segment used for crossing detection",
    )

    @field_validator("exit_line")
    @classmethod
    def _validate_exit_line(
        cls, value: Optional[List[Coordinate]]
    ) -> Optional[List[Coordinate]]:
        if value is None:
            return value
        if len(value) != 2:
            raise ValueError("exit_line must contain exactly two points when provided")
        return value


class ROIConfig(BaseModel):
    """Top-level ROI configuration file structure."""

    model_config = ConfigDict(frozen=True)

    camera_id: str = Field(default="camera0")
    train_roi: ZonePolygon
    road_lanes: List[RoadLane] = Field(default_factory=list)
    exclusion_zones: List[ZonePolygon] = Field(
        default_factory=list,
        description="Zones to ignore for spurious detections",
    )
    metadata: Dict[str, Any] = Field(default_factory=dict)


def load_roi_config(path: str | Path) -> ROIConfig:
    """Load ROI configuration from disk."""

    roi_path = Path(path)
    if not roi_path.exists():
        raise FileNotFoundError(f"ROI configuration not found: {roi_path}")
    data = json.loads(roi_path.read_text(encoding="utf-8"))
    return ROIConfig(**data)


def save_roi_config(config: ROIConfig, path: str | Path) -> None:
    """Persist an ROI configuration to disk."""

    roi_path = Path(path)
    roi_path.write_text(
        json.dumps(config.model_dump(mode="json"), indent=2, sort_keys=True),
        encoding="utf-8",
    )


__all__ = ["ROIConfig", "ZonePolygon", "RoadLane", "load_roi_config", "save_roi_config"]
