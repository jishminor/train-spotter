"""Main entry point wiring together pipeline, storage, and web dashboard."""

from __future__ import annotations

import argparse
import logging
import os
import threading
import time
from typing import Optional, TYPE_CHECKING

from train_spotter.service.config import AppConfig, resolve_config
from train_spotter.storage import (
    DatabaseManager,
    EventBus,
    EventMessage,
    EventSubscription,
    EventType,
    TrainEvent,
    VehicleEvent,
)
from train_spotter.web import create_app
from train_spotter.web.mjpeg import MJPEGStreamServer
from train_spotter.service.roi import ROIConfig, load_roi_config

if TYPE_CHECKING:
    from train_spotter.pipeline import DeepStreamPipeline

LOGGER = logging.getLogger(__name__)


class EventProcessor:
    """Persists events emitted by the analytics pipeline."""

    def __init__(self, event_bus: EventBus, db: DatabaseManager) -> None:
        self._bus = event_bus
        self._db = db
        self._subscription: EventSubscription = event_bus.subscribe()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._subscription.close()
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            message = self._subscription.get(timeout=0.5)
            if not message:
                continue
            self._handle_event(message)

    def _handle_event(self, message: EventMessage) -> None:
        if message.type == EventType.TRAIN_ENDED and isinstance(message.payload, TrainEvent):
            LOGGER.debug("Persisting train event: %s", message.payload)
            self._db.record_train_event(message.payload)
        elif message.type == EventType.VEHICLE_EVENT and isinstance(message.payload, VehicleEvent):
            LOGGER.debug("Persisting vehicle event: %s", message.payload)
            self._db.record_vehicle_event(message.payload)
        elif message.type == EventType.HEARTBEAT:
            self._db.update_stream_heartbeat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train Spotter service")
    parser.add_argument("--config", help="Path to application configuration JSON")
    parser.add_argument(
        "--web-only",
        action="store_true",
        help="Run only the web dashboard (expects external pipeline)",
    )
    parser.add_argument(
        "--passthrough",
        action="store_true",
        help="Skip inference and stream raw camera feed to the dashboard",
    )
    parser.add_argument(
        "--gst-debug",
        help="Set GST_DEBUG for detailed GStreamer logging (e.g. '3' or 'nvarguscamerasrc:5')",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log verbosity",
    )
    return parser.parse_args()


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def run_web_server(app_config: AppConfig, db: DatabaseManager, event_bus: EventBus) -> threading.Thread:
    app = create_app(app_config, db, event_bus)

    def _serve() -> None:
        LOGGER.info(
            "Starting web dashboard on %s:%s",
            app_config.web.host,
            app_config.web.port,
        )
        app.run(
            host=app_config.web.host,
            port=app_config.web.port,
            debug=False,
            use_reloader=False,
            threaded=True,
        )

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    return thread


def main() -> None:
    args = parse_args()
    if args.gst_debug:
        os.environ["GST_DEBUG"] = args.gst_debug
    configure_logging(args.log_level)
    if args.gst_debug:
        LOGGER.info("GST_DEBUG set to %s", args.gst_debug)
    app_config = resolve_config(args.config)
    LOGGER.info("Loaded configuration for camera %s", app_config.camera_id)
    roi_config: ROIConfig | None = None
    try:
        roi_config = load_roi_config(app_config.roi_config_path)
        LOGGER.info("Loaded ROI configuration from %s", app_config.roi_config_path)
    except FileNotFoundError:
        LOGGER.warning("ROI configuration not found at %s; using defaults", app_config.roi_config_path)
    except ValueError as exc:
        LOGGER.error("Failed to load ROI configuration: %s", exc)

    event_bus = EventBus()
    database = DatabaseManager(
        app_config.storage.database_path,
        ensure_fsync=app_config.storage.ensure_fsync,
    )
    mjpeg_server = MJPEGStreamServer(
        app_config.web.signaling_listen_host,
        app_config.web.mjpeg_port,
        app_config.web.max_clients,
        app_config.web.mjpeg_framerate,
    )
    event_processor = EventProcessor(event_bus, database)

    mjpeg_server.start()
    web_thread = run_web_server(app_config, database, event_bus)

    try:
        from train_spotter.pipeline import DeepStreamPipeline
    except ImportError as exc:
        LOGGER.critical("Failed to initialise DeepStream pipeline: %s", exc)
        raise SystemExit("DeepStream Python bindings (pyds) are required. Install them before running Train Spotter.") from exc

    pipeline: Optional["DeepStreamPipeline"] = None
    if not args.web_only:
        if args.passthrough:
            LOGGER.info("Passthrough mode enabled; inference and tracking will be skipped")
        pipeline = DeepStreamPipeline(
            app_config,
            event_bus,
            roi_config=roi_config,
            mjpeg_server=mjpeg_server,
            enable_inference=not args.passthrough,
        )
        pipeline.build()
        pipeline.start()
    else:
        LOGGER.info("Web-only mode enabled; DeepStream pipeline not started")

    try:
        while True:
            if pipeline is not None:
                if pipeline.wait_for_stop(timeout=1.0):
                    LOGGER.info("Pipeline stopped; exiting main loop")
                    break
            else:
                time.sleep(1)
    except KeyboardInterrupt:
        LOGGER.info("Shutdown requested")
    finally:
        event_processor.stop()
        mjpeg_server.stop()
        if pipeline is not None:
            pipeline.stop()
        event_bus.stop()
        web_thread.join(timeout=2.0)
        LOGGER.info("Shutdown complete")


if __name__ == "__main__":  # pragma: no cover - entry point guard
    main()
