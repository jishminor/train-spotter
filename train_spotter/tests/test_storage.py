"""Persistence layer smoke tests."""

from __future__ import annotations

from pathlib import Path
from time import time

from train_spotter.storage import DatabaseManager, TrainEvent, VehicleEvent


def test_database_manager_records_events(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    db = DatabaseManager(str(db_path), ensure_fsync=False)

    start = time()
    end = start + 5
    db.record_train_event(
        TrainEvent(
            train_id="train-test",
            started_at=start,
            ended_at=end,
            duration=end - start,
            coverage_ratio=0.75,
        )
    )

    db.record_vehicle_event(
        VehicleEvent(
            track_id="42",
            class_label="vehicle",
            lane_id="lane_a",
            entered_at=start,
            exited_at=end,
            duration=end - start,
        )
    )

    trains = list(db.fetch_train_events())
    vehicles = list(db.fetch_vehicle_events())

    assert trains and trains[0]["train_id"] == "train-test"
    assert vehicles and vehicles[0]["track_id"] == "42"

    db.close()
