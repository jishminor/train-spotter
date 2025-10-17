#!/usr/bin/env python3
"""Capture a single frame from the Jetson camera for ROI calibration."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2

DEFAULT_CAMERA_PIPELINE = (
    "nvarguscamerasrc sensor-id=0 ! "
    "video/x-raw(memory:NVMM),width={width},height={height},format=NV12,framerate={fps}/1 ! "
    "nvvidconv flip-method=0 ! video/x-raw,format=BGRx ! videoconvert ! video/x-raw,format=BGR ! appsink"
)


def build_pipeline(camera: str, width: int, height: int, fps: int) -> str:
    if "!" in camera:
        return camera.format(width=width, height=height, fps=fps)
    return DEFAULT_CAMERA_PIPELINE.format(width=width, height=height, fps=fps)


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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    pipeline = build_pipeline(args.camera, args.width, args.height, args.fps)
    cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
    if not cap.isOpened():
        print("Failed to open camera pipeline", file=sys.stderr)
        return 1

    # Warm up sensor
    for _ in range(15):
        cap.read()
        time.sleep(0.01)

    ret, frame = cap.read()
    cap.release()
    if not ret or frame is None:
        print("Unable to read frame from camera", file=sys.stderr)
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
