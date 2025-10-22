"""Stream a video file into a v4l2loopback device for testing cameras."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List


def build_ffmpeg_command(
    ffmpeg: str,
    video_path: Path,
    device: Path,
    width: int | None,
    height: int | None,
    framerate: int | None,
    pixel_format: str,
    extra_args: List[str],
) -> List[str]:
    command: List[str] = [
        ffmpeg,
        "-re",
        "-stream_loop",
        "-1",
        "-i",
        str(video_path),
    ]

    if width and height:
        command.extend(["-vf", f"scale={width}:{height}"])
    if framerate:
        command.extend(["-r", str(framerate)])

    command.extend(
        [
            "-vcodec",
            "rawvideo",
            "-pix_fmt",
            pixel_format,
            *extra_args,
            "-f",
            "v4l2",
            str(device),
        ]
    )
    return command


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Stream an input video into a v4l2loopback device so the DeepStream "
            "pipeline can consume it as if it were a live camera."
        )
    )
    parser.add_argument(
        "video",
        type=Path,
        help="Path to the video file (e.g. tests/traffic.mp4)",
    )
    parser.add_argument(
        "--device",
        type=Path,
        default=Path("/dev/video10"),
        help="v4l2loopback device path to write frames to (default: /dev/video10)",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=640,
        help="Output width to advertise on the loopback device (default: 640)",
    )
    parser.add_argument(
        "--height",
        type=int,
        default=480,
        help="Output height to advertise on the loopback device (default: 480)",
    )
    parser.add_argument(
        "--framerate",
        type=int,
        default=30,
        help="Output framerate in FPS (default: 30)",
    )
    parser.add_argument(
        "--pixel-format",
        default="yuv420p",
        help="Pixel format advertised on the loopback device (default: yuv420p)",
    )
    parser.add_argument(
        "--ffmpeg",
        default="ffmpeg",
        help="ffmpeg binary to invoke (default: ffmpeg)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the command instead of executing it",
    )
    parser.add_argument(
        "--extra-ffmpeg-args",
        nargs=argparse.REMAINDER,
        default=[],
        help="Additional arguments appended to the ffmpeg command before the sink",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    ffmpeg = shutil.which(args.ffmpeg)
    if ffmpeg is None:
        print("ffmpeg not found on PATH; install it or pass --ffmpeg", file=sys.stderr)
        return 1

    video_path = args.video.expanduser().resolve()
    if not video_path.exists():
        print(f"Video file not found: {video_path}", file=sys.stderr)
        return 1

    device_path = args.device
    if not device_path.exists():
        print(
            f"Warning: {device_path} does not exist. Load v4l2loopback first:"
            f" sudo modprobe v4l2loopback video_nr={device_path.name.lstrip('video')}"
            " card_label=TrafficLoopback exclusive_caps=1",
            file=sys.stderr,
        )

    command = build_ffmpeg_command(
        ffmpeg,
        video_path,
        device_path,
        width=args.width,
        height=args.height,
        framerate=args.framerate,
        pixel_format=args.pixel_format,
        extra_args=args.extra_ffmpeg_args,
    )

    if args.dry_run:
        print(" \\")
        for idx, token in enumerate(command):
            sep = " \\\n" if idx < len(command) - 1 else "\n"
            print(token, end=sep)
        return 0

    print(
        "Streaming",
        video_path,
        "->",
        device_path,
        f"({args.width}x{args.height} @ {args.framerate}fps, {args.pixel_format})",
    )
    print("Press Ctrl+C to stop streaming.")

    try:
        process = subprocess.run(command, check=False)
        if process.returncode not in (0, 255):
            print(f"ffmpeg exited with code {process.returncode}", file=sys.stderr)
            return process.returncode
    except KeyboardInterrupt:
        print("\nStopping stream...")
    return 0


if __name__ == "__main__":  # pragma: no cover - manual utility
    sys.exit(main())
