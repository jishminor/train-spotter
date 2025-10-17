"""Basic configuration loading tests."""

from __future__ import annotations

from train_spotter.service.config import AppConfig, resolve_config


def test_appconfig_default_contains_core_sections() -> None:
    cfg = AppConfig.default()
    assert cfg.camera_id == "camera0"
    assert cfg.train_detection.zone.polygon.points
    assert cfg.vehicle_tracking.infer_primary_config_path.endswith(".txt")


def test_resolve_config_without_path_uses_defaults() -> None:
    cfg = resolve_config(None)
    assert isinstance(cfg, AppConfig)
    assert cfg.display.enable_overlay
