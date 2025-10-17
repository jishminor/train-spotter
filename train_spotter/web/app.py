"""Flask application exposing live view and historical data."""

from __future__ import annotations

import datetime as dt
from typing import Iterable, List

from flask import Flask, Response, jsonify, render_template

from train_spotter.service.config import AppConfig
from train_spotter.storage import DatabaseManager
from train_spotter.web.streaming import FrameBroadcaster


def _row_to_dict(row) -> dict:
    return {key: row[key] for key in row.keys()}


def create_app(
    app_config: AppConfig,
    db: DatabaseManager,
    frame_broadcaster: FrameBroadcaster,
) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")

    @app.route("/")
    def index():
        return render_template(
            "index.html",
            web_config=app_config.web,
        )

    @app.route("/history")
    def history():
        train_rows = list(db.fetch_train_events(limit=100))
        vehicle_rows = list(db.fetch_vehicle_events(limit=200))
        trains = [_row_to_dict(row) for row in train_rows]
        vehicles = [_row_to_dict(row) for row in vehicle_rows]
        return render_template(
            "history.html",
            trains=trains,
            vehicles=vehicles,
        )

    @app.route("/stream.mjpg")
    def video_feed():
        fps = app_config.web.mjpeg_framerate
        return Response(
            frame_broadcaster.mjpeg_stream(fps=fps),
            mimetype="multipart/x-mixed-replace; boundary=frame",
        )

    @app.route("/api/status")
    def status():
        train_rows = list(db.fetch_train_events(limit=1))
        latest_train = _row_to_dict(train_rows[0]) if train_rows else None
        status_payload = {
            "timestamp": dt.datetime.utcnow().isoformat() + "Z",
            "latest_train": latest_train,
        }
        return jsonify(status_payload)

    return app


__all__ = ["create_app"]
