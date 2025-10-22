"""Flask application exposing live view, WebRTC signaling metadata, and history."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from flask import Flask, jsonify, render_template, request, Response

from train_spotter.service.config import AppConfig
from train_spotter.storage import DatabaseManager, EventBus, EventType

LOGGER = logging.getLogger(__name__)


def _row_to_dict(row) -> dict:
    return {key: row[key] for key in row.keys()}


def _resolve_signaling_host(configured_host: str, request_host: str) -> Optional[str]:
    """Return host to use for signaling; None implies client should auto-detect."""
    if configured_host in {"", "0.0.0.0", "::"}:
        host_only = request_host.split(":", 1)[0]
        return host_only or None
    return configured_host


def _calculate_activity_over_time(trains: list, vehicles: list) -> list:
    """Calculate activity over the last 24 hours, grouped by hour."""
    now = dt.datetime.utcnow()
    buckets = []

    # Create 24 hourly buckets
    for i in range(23, -1, -1):
        hour_start = now - dt.timedelta(hours=i+1)
        hour_end = now - dt.timedelta(hours=i)

        train_count = sum(
            1 for t in trains
            if hour_start.timestamp() <= t.get("started_at", 0) < hour_end.timestamp()
        )

        vehicle_count = sum(
            1 for v in vehicles
            if hour_start.timestamp() <= v.get("entered_at", 0) < hour_end.timestamp()
        )

        buckets.append({
            "label": hour_start.strftime("%H:%M"),
            "trains": train_count,
            "vehicles": vehicle_count,
        })

    return buckets


def _calculate_hourly_traffic(vehicles: list) -> list:
    """Calculate vehicle count for each hour of the day (0-23)."""
    hourly = [0] * 24

    for v in vehicles:
        entered_at = v.get("entered_at", 0)
        if entered_at > 0:
            hour = dt.datetime.fromtimestamp(entered_at).hour
            hourly[hour] += 1

    return hourly


def create_app(
    app_config: AppConfig,
    db: DatabaseManager,
    event_bus: Optional[EventBus] = None,
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

    @app.route("/dashboard")
    def dashboard():
        return render_template("dashboard.html")

    @app.route("/api/status")
    def status():
        train_rows = list(db.fetch_train_events(limit=1))
        vehicle_rows = list(db.fetch_vehicle_events(limit=100))
        latest_train = _row_to_dict(train_rows[0]) if train_rows else None

        # Count today's events (simple approximation using recent data)
        train_count = len(list(db.fetch_train_events(limit=100)))
        vehicle_count = len(vehicle_rows)

        status_payload = {
            "timestamp": dt.datetime.utcnow().isoformat() + "Z",
            "latest_train": latest_train,
            "train_count": train_count,
            "vehicle_count": vehicle_count,
        }
        return jsonify(status_payload)

    @app.route("/api/stats")
    def stats():
        """Comprehensive statistics endpoint for dashboard."""
        train_rows = list(db.fetch_train_events(limit=1000))
        vehicle_rows = list(db.fetch_vehicle_events(limit=1000))

        trains = [_row_to_dict(row) for row in train_rows]
        vehicles = [_row_to_dict(row) for row in vehicle_rows]

        # Calculate statistics
        total_trains = len(trains)
        total_vehicles = len(vehicles)

        # Vehicle type breakdown
        vehicle_types = {"car": 0, "truck": 0, "other": 0}
        for v in vehicles:
            label = v.get("class_label", "").lower()
            if label == "car":
                vehicle_types["car"] += 1
            elif label == "truck":
                vehicle_types["truck"] += 1
            else:
                vehicle_types["other"] += 1

        # Lane distribution
        lane_distribution = {}
        for v in vehicles:
            lane = v.get("lane_id", "unknown")
            lane_distribution[lane] = lane_distribution.get(lane, 0) + 1

        # Average duration
        avg_duration = 0
        if vehicles:
            total_duration = sum(v.get("duration", 0) for v in vehicles)
            avg_duration = total_duration / len(vehicles)

        # Activity over time (last 24 hours, grouped by hour)
        activity_over_time = _calculate_activity_over_time(trains, vehicles)

        # Hourly traffic (24-hour buckets)
        hourly_traffic = _calculate_hourly_traffic(vehicles)

        stats_payload = {
            "total_trains": total_trains,
            "total_vehicles": total_vehicles,
            "total_trucks": vehicle_types["truck"],
            "avg_duration": round(avg_duration, 2),
            "vehicle_types": vehicle_types,
            "lane_distribution": lane_distribution,
            "activity_over_time": activity_over_time,
            "hourly_traffic": hourly_traffic,
        }
        return jsonify(stats_payload)

    @app.route("/api/events/stream")
    def event_stream():
        """Server-Sent Events endpoint for real-time notifications."""
        if not event_bus:
            return jsonify({"error": "Event bus not available"}), 503

        def generate():
            subscription = event_bus.subscribe(maxsize=50)
            try:
                # Send initial connection event
                yield f"data: {{'type': 'connected', 'message': 'Event stream connected'}}\n\n"

                while True:
                    try:
                        event = subscription.get(timeout=30.0)  # 30s timeout for keepalive

                        # Format event for SSE
                        event_data = {
                            "type": event.type.name.lower(),
                            "timestamp": event.timestamp,
                            "payload": event.payload if isinstance(event.payload, dict) else {},
                        }

                        # Only send train and vehicle events (not heartbeats)
                        if event.type in (EventType.TRAIN_STARTED, EventType.TRAIN_ENDED, EventType.VEHICLE_EVENT):
                            import json
                            yield f"data: {json.dumps(event_data)}\n\n"

                    except Exception:
                        # Send keepalive comment to prevent timeout
                        yield ": keepalive\n\n"

            except GeneratorExit:
                subscription.close()
            finally:
                subscription.close()

        return Response(generate(), mimetype="text/event-stream")

    return app


__all__ = ["create_app"]
