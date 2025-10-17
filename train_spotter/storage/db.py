"""SQLite persistence layer for train and vehicle events."""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Iterable, Optional

from pydantic import BaseModel


class TrainEvent(BaseModel):
    """Structured representation of a train pass-by."""

    train_id: str
    started_at: float
    ended_at: float
    duration: float
    coverage_ratio: Optional[float] = None


class VehicleEvent(BaseModel):
    """Structured representation of a single vehicle track across a lane."""

    track_id: str
    class_label: str
    lane_id: str
    entered_at: float
    exited_at: float
    duration: float


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS train_passes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        train_id TEXT NOT NULL,
        started_at REAL NOT NULL,
        ended_at REAL NOT NULL,
        duration REAL NOT NULL,
        coverage_ratio REAL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS vehicle_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        track_id TEXT NOT NULL,
        class_label TEXT NOT NULL,
        lane_id TEXT NOT NULL,
        entered_at REAL NOT NULL,
        exited_at REAL NOT NULL,
        duration REAL NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS system_status (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        last_stream_heartbeat REAL,
        last_train_event REAL,
        last_vehicle_event REAL
    );
    """,
)


class DatabaseManager:
    """Thin wrapper around SQLite for thread-safe access and schema management."""

    def __init__(self, db_path: str, ensure_fsync: bool = True) -> None:
        self._path = Path(db_path)
        self._ensure_fsync = ensure_fsync
        self._lock = threading.RLock()
        self._conn = self._create_connection()
        self._init_schema()

    def _create_connection(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        if self._ensure_fsync:
            conn.execute("PRAGMA synchronous = FULL;")
        else:
            conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA journal_mode = WAL;")
        return conn

    def _init_schema(self) -> None:
        with self._transaction() as cur:
            for stmt in SCHEMA_STATEMENTS:
                cur.execute(stmt)
            cur.execute(
                """
                INSERT OR IGNORE INTO system_status (id, last_stream_heartbeat)
                VALUES (1, ?)
                """,
                (time.time(),),
            )

    @contextmanager
    def _transaction(self) -> Generator[sqlite3.Cursor, None, None]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    def record_train_event(self, event: TrainEvent) -> None:
        with self._transaction() as cur:
            cur.execute(
                """
                INSERT INTO train_passes (train_id, started_at, ended_at, duration, coverage_ratio)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event.train_id,
                    event.started_at,
                    event.ended_at,
                    event.duration,
                    event.coverage_ratio,
                ),
            )
            cur.execute(
                """
                UPDATE system_status SET last_train_event = ? WHERE id = 1
                """,
                (event.ended_at,),
            )

    def record_vehicle_event(self, event: VehicleEvent) -> None:
        with self._transaction() as cur:
            cur.execute(
                """
                INSERT INTO vehicle_events (track_id, class_label, lane_id,
                                            entered_at, exited_at, duration)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event.track_id,
                    event.class_label,
                    event.lane_id,
                    event.entered_at,
                    event.exited_at,
                    event.duration,
                ),
            )
            cur.execute(
                """
                UPDATE system_status SET last_vehicle_event = ? WHERE id = 1
                """,
                (event.exited_at,),
            )

    def update_stream_heartbeat(self) -> None:
        with self._transaction() as cur:
            cur.execute(
                """
                UPDATE system_status SET last_stream_heartbeat = ? WHERE id = 1
                """,
                (time.time(),),
            )

    def fetch_train_events(self, limit: int = 100) -> Iterable[sqlite3.Row]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                SELECT train_id, started_at, ended_at, duration, coverage_ratio
                FROM train_passes
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = cur.fetchall()
            cur.close()
        return rows

    def fetch_vehicle_events(self, limit: int = 200) -> Iterable[sqlite3.Row]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                SELECT track_id, class_label, lane_id, entered_at, exited_at, duration
                FROM vehicle_events
                ORDER BY entered_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = cur.fetchall()
            cur.close()
        return rows

    def close(self) -> None:
        with self._lock:
            self._conn.close()


__all__ = ["DatabaseManager", "TrainEvent", "VehicleEvent"]
