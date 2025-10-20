"""Flask application exposing live view, WebRTC signaling metadata, and history."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from flask import Flask, jsonify, render_template, request

from train_spotter.service.config import AppConfig
from train_spotter.storage import DatabaseManager

LOGGER = logging.getLogger(__name__)


def _row_to_dict(row) -> dict:
    return {key: row[key] for key in row.keys()}


def _resolve_signaling_host(configured_host: str, request_host: str) -> Optional[str]:
    """Return host to use for signaling; None implies client should auto-detect."""
    if configured_host in {"", "0.0.0.0", "::"}:
        host_only = request_host.split(":", 1)[0]
        return host_only or None
    return configured_host


def create_app(
    app_config: AppConfig,
    db: DatabaseManager,
) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")

    @app.route("/")
    def index():
        signaling_host = _resolve_signaling_host(app_config.web.signaling_listen_host, request.host)
        ws_scheme = "wss" if request.scheme == "https" else "ws"
        signaling_url = (
            f"{ws_scheme}://{signaling_host}:{app_config.web.signaling_port}"
            if signaling_host
            else None
        )
        mjpeg_host = _resolve_signaling_host(app_config.web.signaling_listen_host, request.host)
        mjpeg_url = (
            f"{ws_scheme}://{mjpeg_host}:{app_config.web.mjpeg_port}/mjpeg"
            if mjpeg_host
            else None
        )
        return render_template(
            "index.html",
            web_config=app_config.web,
            signaling_url=signaling_url,
            signaling_host=signaling_host,
            signaling_port=app_config.web.signaling_port,
            mjpeg_url=mjpeg_url,
            mjpeg_host=mjpeg_host,
            mjpeg_port=app_config.web.mjpeg_port,
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
