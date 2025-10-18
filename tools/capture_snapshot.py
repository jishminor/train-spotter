#!/usr/bin/env python3
"""Capture a single frame from the Jetson camera for ROI calibration."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

import cv2
import numpy as np

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from train_spotter.service.config import AppConfig

DEFAULT_CAMERA_PIPELINE = (
    "v4l2src device=/dev/video0 ! video/x-raw,format=YUY2,width={width},height={height},framerate={fps}/1 ! nvvidconv ! "
    "video/x-raw(memory:NVMM),format=NV12 ! "
    "nvvidconv flip-method=0 ! video/x-raw,format=BGRx ! videoconvert ! video/x-raw,format=BGR ! "
    "appsink name=capture_sink emit-signals=false sync=false max-buffers=1 drop=true"
)


def _is_file_source(source: str) -> bool:
    lowered = source.lower()
    return any(keyword in lowered for keyword in ("filesrc", "multifilesrc", "uridecodebin", "nvurisrcbin", "file://"))


def _ensure_appsink(pipeline: str, width: int, height: int) -> str:
    if "appsink" in pipeline:
        return pipeline
    segments = pipeline.strip().rstrip("! ").split("!")
    segments = [segment.strip() for segment in segments if segment.strip()]
    segments.extend(
        [
            "nvvidconv",
            f"video/x-raw,width={width},height={height},format=BGRx",
            "videoconvert",
            "video/x-raw,format=BGR",
            "appsink name=capture_sink emit-signals=false sync=false max-buffers=1 drop=true",
        ]
    )
    return " ! ".join(segments)


def _resolve_file_location(source: str) -> Path:
    parsed = urlparse(source)
    if parsed.scheme and parsed.scheme != "file":
        raise ValueError(f"Unsupported URI scheme in camera source: {source}")
    return Path(unquote(parsed.path)).expanduser().resolve()


def _build_pipeline_from_config(config_path: Path, width: int, height: int, fps: int) -> tuple[str, bool]:
    app_config = AppConfig.from_file(config_path)
    camera_source = app_config.camera_source.strip()
    if camera_source.startswith("file://"):
        file_path = _resolve_file_location(camera_source)
        pipeline = (
            "filesrc location={path} ! qtdemux name=demux "
            "demux.video_0 ! queue ! h264parse ! nvv4l2decoder "
            "! nvvidconv ! video/x-raw,width={width},height={height},format=BGRx "
            "! videoconvert ! video/x-raw,format=BGR "
            "! appsink name=capture_sink emit-signals=false sync=false max-buffers=1 drop=true"
        ).format(path=file_path, width=width, height=height)
        return pipeline, False
    elif "!" in camera_source:
        pipeline = camera_source.format(width=width, height=height, fps=fps)
        pipeline = _ensure_appsink(pipeline, width, height)
        return pipeline, not _is_file_source(camera_source)
    else:
        pipeline = _ensure_appsink(camera_source, width, height)
        return pipeline, not _is_file_source(camera_source)


def build_pipeline(camera: str, width: int, height: int, fps: int) -> tuple[str, bool]:
    if "!" in camera or "file://" in camera:
        pipeline = camera.format(width=width, height=height, fps=fps)
    else:
        pipeline = DEFAULT_CAMERA_PIPELINE.format(width=width, height=height, fps=fps)
    is_live = not _is_file_source(camera)
    return _ensure_appsink(pipeline, width, height), is_live


def capture_frame(pipeline: str, timeout_seconds: int = 5, warmup_frames: int = 0) -> np.ndarray:
    Gst.init(None)
    gst_pipeline = Gst.parse_launch(pipeline)
    appsink = gst_pipeline.get_by_name("capture_sink")
    if appsink is None:
        gst_pipeline.set_state(Gst.State.NULL)
        raise RuntimeError("Pipeline must terminate with an appsink named 'capture_sink'")
    appsink.set_property("emit-signals", False)
    gst_pipeline.set_state(Gst.State.PLAYING)
    bus = gst_pipeline.get_bus()
    try:
        sample = None
        total_attempts = max(0, warmup_frames) + 1
        for idx in range(total_attempts):
            sample = appsink.emit("try-pull-sample", timeout_seconds * Gst.SECOND)
            if sample is None:
                msg = bus.timed_pop_filtered(timeout_seconds * Gst.SECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS)
                if msg and msg.type == Gst.MessageType.ERROR:
                    err, debug = msg.parse_error()
                    raise RuntimeError(f"GStreamer error: {err} ({debug})")
                raise RuntimeError("Timed out while waiting for frame from pipeline")
            if idx < warmup_frames:
                sample = None
                continue
            break
        if sample is None:
            raise RuntimeError("Failed to capture frame from pipeline")
        buffer = sample.get_buffer()
        caps = sample.get_caps()
        structure = caps.get_structure(0)
        width = structure.get_value("width")
        height = structure.get_value("height")
        success, mapinfo = buffer.map(Gst.MapFlags.READ)
        if not success:
            raise RuntimeError("Failed to map buffer from appsink")
        try:
            frame = np.frombuffer(mapinfo.data, dtype=np.uint8).reshape((height, width, 3)).copy()
        finally:
            buffer.unmap(mapinfo)
        sample = None
    finally:
        gst_pipeline.set_state(Gst.State.NULL)
    return frame


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture a snapshot for ROI calibration")
    parser.add_argument("output", type=Path, help="Path to write the captured image (PNG)")
    parser.add_argument(
        "--camera",
        default=DEFAULT_CAMERA_PIPELINE,
        help="GStreamer pipeline for the camera source (format placeholders: width, height, fps)",
    )
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Display the captured frame in a preview window (requires GUI)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Application configuration file to reuse camera settings",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=None,
        help="Frames to discard before saving the snapshot (default: auto – 30 for live sources, 0 for file sources)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.config:
            pipeline, is_live = _build_pipeline_from_config(args.config, args.width, args.height, args.fps)
        else:
            pipeline, is_live = build_pipeline(args.camera, args.width, args.height, args.fps)
    except Exception as exc:  # pragma: no cover - runtime error path
        print(f"Failed to build camera pipeline: {exc}", file=sys.stderr)
        return 1

    if args.warmup is None:
        warmup_frames = 30 if is_live else 0
    else:
        warmup_frames = max(0, args.warmup)

    try:
        frame = capture_frame(pipeline, warmup_frames=warmup_frames)
    except Exception as exc:
        print(f"Failed to open camera pipeline: {exc}", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(args.output), frame):
        print("Failed to write snapshot", file=sys.stderr)
        return 1

    print(f"Snapshot saved to {args.output}")
    if args.preview:
        cv2.imshow("ROI Snapshot", frame)
        print("Press any key in the preview window to exit...")
        cv2.waitKey(0)
        cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
