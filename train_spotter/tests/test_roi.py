"""ROI configuration tests."""

from __future__ import annotations

from pathlib import Path

from train_spotter.service.roi import load_roi_config


def test_load_example_roi(tmp_path: Path) -> None:
    example = Path("train_spotter/data/roi_config.example.json")
    target = tmp_path / "roi.json"
    target.write_text(example.read_text(), encoding="utf-8")

    roi = load_roi_config(target)

    assert roi.camera_id == "camera0"
    assert roi.train_roi.points
    assert len(roi.road_lanes) == 2
